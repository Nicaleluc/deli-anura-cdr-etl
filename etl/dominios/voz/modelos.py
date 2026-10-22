"""
Modelos y primitivas de voz (carga diferida: pesan y tardan).

- faster-whisper `small` (CPU int8): segmentos con timestamps (VAD incluido).
- speechbrain ECAPA (voxceleb): embeddings 192-dims L2-normalizados.
- Diarizacion por llamada: aglomerativo coseno, k adaptativo 1..4.
- Match: coseno contra plantillas con umbral por persona.

Modelos se descargan de HuggingFace en la 1ra corrida (~500 MB whisper
small + ~100 MB ecapa). En Windows sin Developer Mode se usa COPY en vez
de symlinks (si no, speechbrain falla con WinError 1314).
"""

import os
import re

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SR = 16000

# Turnos antes de este segundo son IVR/jingle: se GUARDAN (Rol OTRO) pero
# no votan en diarizacion ni matching.
IVR_SEG = 15.0
MIN_SEG = 1.6
MAX_SEG = 11.0

_whisper = None
_speaker = None


def get_whisper(model_name="small"):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _whisper


def get_speaker(savedir=None):
    global _speaker
    if _speaker is None:
        import torch  # noqa: F401 - lo exige speechbrain en runtime
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy

        _speaker = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir or os.path.join(os.path.expanduser("~"), ".cache",
                                            "deli-voz", "ecapa"),
            run_opts={"device": "cpu"},
            local_strategy=LocalStrategy.COPY,
        )
    return _speaker


def decode_audio(path, max_seg=None):
    """Ogg -> float32 mono 16k (PyAV, sin ffmpeg).

    RESAMPLEA a 16k si el archivo no lo está: hay al menos un CDR (el
    un CDR medido) en 8kHz, y `to_ndarray` devuelve las muestras tal cual, sin
    convertir la tasa. Sin resample, un audio de 8k se leía como 16k =>
    reproducción al doble de velocidad (voz de ratón), y los segmentos y
    embeddings salían mal alineados. Verificado: 832.12s x 8k = 6.656.960
    muestras; antes se veían 416.1s, exactamente la mitad.
    """
    import av

    with av.open(path) as cont:
        stream = cont.streams.audio[0]
        stream.thread_type = "AUTO"
        resampler = None
        if stream.sample_rate != SR:
            resampler = av.AudioResampler(format="flt", layout="mono",
                                          rate=SR)
        partes = []
        for frame in cont.decode(stream):
            if resampler is not None:
                for out in resampler.resample(frame):
                    # el resampler devuelve (canales, muestras); hay que
                    # aplanar a 1D para que concatenar con los frames
                    # crudos (que llegan 1D) no rompa
                    partes.append(out.to_ndarray().astype(np.float32).ravel())
                continue
            a = frame.to_ndarray().astype(np.float32)
            if a.ndim == 2:
                # Planar (vorbis: canales x muestras) vs packed (wav: muestras
                # x canales). El eje de tamaño <=2 es canales.
                if a.shape[0] not in (1, 2) and a.shape[1] in (1, 2):
                    a = a.T
                a = a.mean(axis=0)
            partes.append(a)
        if resampler is not None:
            for out in resampler.resample(None):  # flush final
                partes.append(out.to_ndarray().astype(np.float32).ravel())
    x = np.concatenate(partes) if partes else np.zeros(0, dtype=np.float32)
    if max_seg is not None:
        x = x[: int(max_seg * SR)]
    peak = np.abs(x).max(initial=0)
    if peak > 0:
        x = x / peak
    return x


def es_basura(texto):
    """Gate textual: descarta ruido/musica/jingle (validado en 772 turnos)."""
    t = (texto or "").strip()
    if len(t) < 4:
        return True
    letras = sum(c.isalpha() for c in t)
    if letras < 4 or letras / len(t) < 0.5:
        return True
    toks = [w.lower() for w in re.findall(r"[^\W\d_]+", t)]
    if len(toks) >= 5 and len(set(toks)) <= max(2, len(toks) // 4):
        return True
    if re.search(r"(suscr[ií]bete|musiquita|eleva tu voz)", t, re.I):
        return True
    return False


def transcribir(wm, audio, beam_size=5, language="es"):
    """Segmentos limpios: [(ini, fin, texto)]."""
    segs, _met = transcribir_con_metricas(wm, audio, beam_size, language)
    return segs


def transcribir_con_metricas(wm, audio, beam_size=5, language="es"):
    """Como transcribir + conteos para calidad: (segs, metricas).

    metricas = {raw: segmentos que dio whisper, corta: fuera de
    [MIN_SEG, MAX_SEG], basura: gate es_basura, ok: los que quedan}.
    """
    segs, _info = wm.transcribe(audio.astype(np.float32), language=language,
                                 vad_filter=True, beam_size=beam_size)
    met = {"raw": 0, "corta": 0, "basura": 0, "ok": 0}
    out = []
    for s in segs:
        met["raw"] += 1
        dur = s.end - s.start
        if not (MIN_SEG <= dur <= MAX_SEG):
            met["corta"] += 1
            continue
        txt = (s.text or "").strip()
        if es_basura(txt):
            met["basura"] += 1
            continue
        met["ok"] += 1
        out.append((float(s.start), float(s.end), txt))
    return out, met


def calificar(cps, tpm, pct_basura=None):
    """Calidad del transcripto: OK / DUDOSA / MALA.

    Umbrales medidos 2026-10-02 sobre 810 conversaciones (cps med 8.8 p5 3.1;
    tpm med 9.0 p5 3.3). pct_basura se GUARDA pero no decide todavia (sin
    calibrar contra casos reales).
    """
    if cps < 2.0:
        return "MALA"
    if cps < 5.0 or tpm < 3.0:
        return "DUDOSA"
    return "OK"


def embeber(spk, audio, segmentos):
    """Embedding por segmento (solo >=1.6s). Devuelve lista de vectores
    normalizados alineada con `segmentos` (None si no embebible)."""
    import torch

    out = []
    for ini, fin, _txt in segmentos:
        seg = audio[int(ini * SR): int(fin * SR)]
        if seg.size < MIN_SEG * SR:
            out.append(None)
            continue
        seg = seg / (np.abs(seg).max() + 1e-9)
        with torch.no_grad():
            v = spk.encode_batch(torch.from_numpy(seg).unsqueeze(0))
        v = torch.nn.functional.normalize(v.squeeze(), dim=0).numpy()
        out.append(v.astype(np.float32))
    return out


def canal_de_segmento(L, R, ini, fin):
    """Canal (L/R) donde el segmento tiene mas energia (pierna dominante)."""
    a, b = int(ini * SR), int(fin * SR)
    if b <= a:
        return "L"
    el = float((L[a:b].astype(np.float64) ** 2).mean())
    er = float((R[a:b].astype(np.float64) ** 2).mean())
    return "L" if el >= er else "R"


def embeber_estereo(spk, L, R, segmentos):
    """Embedding por segmento en su CANAL DOMINANTE (piernas Anura).

    El downmix mono lava la voz mas debil (medido: 0,75 en mono
    vs 0,37 en el canal correcto). Aca cada segmento se embebe del canal donde suena,
    que es lo que separa tecnico/cliente. Devuelve (vectores, canales).
    """
    import torch

    out, chs = [], []
    for ini, fin, _txt in segmentos:
        a, b = int(ini * SR), int(fin * SR)
        if b - a < MIN_SEG * SR:
            out.append(None)
            chs.append(None)
            continue
        ch = canal_de_segmento(L, R, ini, fin)
        seg = (L if ch == "L" else R)[a:b].copy()
        seg = seg / (np.abs(seg).max() + 1e-9)
        with torch.no_grad():
            v = spk.encode_batch(torch.from_numpy(seg).unsqueeze(0))
        v = torch.nn.functional.normalize(v.squeeze(), dim=0).numpy()
        out.append(v.astype(np.float32))
        chs.append(ch)
    return out, chs


def transcribir_ventana(wm, audio, language="es"):
    """Texto de una ventana puntual, SIN filtro de duracion del pipeline.

    Sirve para recuperar turnos del canal del tecnico que el VAD/mono
    descarta (medido: en un caso real whisper no capturo a un tecnico). Normaliza
    el pico: el canal del tecnico suele venir mas bajo que el del cliente.
    """
    if audio.size < int(0.4 * SR):
        return ""
    a = audio.astype(np.float32)
    a = a / (np.abs(a).max() + 1e-9)
    segs, _ = wm.transcribe(a, language=language, vad_filter=True)
    return " ".join((s.text or "").strip() for s in segs).strip()


def diarizar(embeddings):
    """Agrupa turnos en hablantes. k adaptativo 1..4 (complete-linkage,
    corte 0.90; si queda 1 grupo con >=6 turnos y los centroides de una
    biparticion separan >=0.45, fuerza k=2). Devuelve labels por indice."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    X = np.array(embeddings, dtype=np.float32)
    n = len(X)
    if n <= 1:
        return [1] * n
    Z = linkage(pdist(X, metric="cosine"), method="complete")
    lab = fcluster(Z, 0.90, criterion="distance")
    if len(set(lab)) > 4:
        lab = fcluster(Z, 4, criterion="maxclust")
    if len(set(lab)) == 1 and n >= 6:
        lab2 = fcluster(Z, 2, criterion="maxclust")
        a = X[lab2 == 1].mean(0)
        b = X[lab2 == 2].mean(0)
        a /= np.linalg.norm(a)
        b /= np.linalg.norm(b)
        if 1 - float(a @ b) >= 0.45:
            return [int(x) for x in lab2]
    return [int(x) for x in lab]


def parse_vector(texto):
    """VECTOR(192) de SQL ('[0.1,...]') -> numpy."""
    return np.fromstring(texto.strip().strip("[]"), dtype=np.float32, sep=",")


def vec_str(vec):
    v = np.asarray(vec, dtype=np.float32)
    v = v / (np.linalg.norm(v) or 1.0)
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def decode_estereo(path):
    """Estereo REAL a 16k: (L, R) float32 mono-normalizados por pico global.

    A diferencia de decode_audio (que mezcla a mono), preserva los canales:
    las grabaciones Anura traen una pierna por canal (corr L/R ~= 0, medido
    en 10 archivos; ver docs/canales-estereo.md). Resamplea por canal si el
    archivo no esta en 16k. Mono -> canal duplicado.
    """
    import av

    with av.open(path) as cont:
        stream = cont.streams.audio[0]
        stream.thread_type = "AUTO"
        partes = []
        for frame in cont.decode(stream):
            a = frame.to_ndarray().astype(np.float32)
            partes.append(a if a.ndim == 2 else np.stack([a, a]))
    raw = np.concatenate(partes, axis=1).astype(np.float64)
    sr = int(stream.sample_rate)
    if sr != SR:
        from scipy.signal import resample_poly

        raw = np.stack([resample_poly(raw[0], SR, sr),
                        resample_poly(raw[1], SR, sr)])
    if raw.shape[0] not in (1, 2) and raw.shape[1] in (1, 2):
        raw = raw.T
    if raw.shape[0] == 1:
        raw = np.stack([raw[0], raw[0]])
    peak = np.abs(raw).max(initial=0)
    if peak > 0:
        raw = raw / peak
    return raw[0].astype(np.float32), raw[1].astype(np.float32)


def elegir_lado(dist_l, dist_r, margen=0.10):
    """Lado del tecnico por distancia a su plantilla.

    None si falta un lado o la diferencia no supera el margen (llamada
    mezclada / musica / solapes): mejor no decir que decir mal.
    """
    if dist_l is None or dist_r is None:
        return None
    if abs(dist_l - dist_r) < margen:
        return None
    return "L" if dist_l < dist_r else "R"


def canal_del_tecnico(path, template):
    """Determina de que lado esta el tecnico (reporta, no decide matching).

    Junta frames puros por canal (energia 2x sobre el otro, piso 0.15),
    centroide por lado, distancia a la plantilla. Devuelve
    {"lado", "dist_L", "dist_R", "seg_L", "seg_R"}.
    """
    import torch

    L, R = decode_estereo(path)
    n = min(len(L), len(R))
    fl = SR  # frames de 1 s
    nfr = n // fl
    if nfr == 0:
        return {"lado": None, "dist_L": None, "dist_R": None,
                "seg_L": 0, "seg_R": 0}
    eL = np.array([np.sqrt((L[k * fl:(k + 1) * fl] ** 2).mean())
                   for k in range(nfr)])
    eR = np.array([np.sqrt((R[k * fl:(k + 1) * fl] ** 2).mean())
                   for k in range(nfr)])
    # Normalizacion POR CANAL (cada pierna tiene su propio nivel): lo que
    # importa es la dinamica relativa dentro del canal, no el volumen
    # absoluto. OJO: ndarray.max(initial=1.0) pone piso 1.0 y NO normaliza.
    eL /= float(eL.max()) or 1.0
    eR /= float(eR.max()) or 1.0
    spk = get_speaker()
    dist = {}
    segs = {}
    for lado, mask, ch, y in (("L", (eL > 0.15) & (eL > 2 * eR), 0, L),
                              ("R", (eR > 0.15) & (eR > 2 * eL), 1, R)):
        segs[lado] = float(mask.sum())
        puro = np.concatenate([y[k * fl:(k + 1) * fl]
                               for k in np.where(mask)[0]]) \
            if mask.sum() else np.zeros(0, dtype=np.float32)
        vecs = []
        for k in range(0, len(puro), 11 * SR):
            seg = puro[k:k + 11 * SR]
            if seg.size < MIN_SEG * SR:
                continue
            seg = seg / (np.abs(seg).max() + 1e-9)
            with torch.no_grad():
                v = spk.encode_batch(torch.from_numpy(seg).unsqueeze(0))
            vecs.append(torch.nn.functional.normalize(
                v.squeeze(), dim=0).numpy().astype(np.float32))
        if not vecs:
            dist[lado] = None
            continue
        cen = np.array(vecs).mean(0)
        cen /= np.linalg.norm(cen) or 1.0
        tpl = np.asarray(template, dtype=np.float32)
        tpl /= np.linalg.norm(tpl) or 1.0
        dist[lado] = 1 - float(cen @ tpl)
    return {"lado": elegir_lado(dist["L"], dist["R"]),
            "dist_L": dist["L"], "dist_R": dist["R"],
            "seg_L": segs["L"], "seg_R": segs["R"]}


def match_plantilla(vec, plantillas):
    """Mejor (nombre, distancia) contra [(nombre, centroide, umbral)]."""
    mejor, dist = None, 9.0
    for nombre, centroide, _umbral in plantillas:
        d = 1 - float(np.dot(vec, centroide))
        if d < dist:
            mejor, dist = nombre, d
    return mejor, dist
