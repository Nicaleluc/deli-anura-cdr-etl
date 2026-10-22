"""
Atribucion de tecnico por llamada: audio -> diarizacion -> match -> SQL.

Pipeline por conversacion (solo IN + MULTIDIAL + grabada):
  1. Descarga el .ogg (si no existe) a VOZ_AUDIO_ROOT/YYYY-MM-DD/.
  2. Transcribe (faster-whisper, VAD) y embebe turnos (ECAPA).
  3. Diariza en hablantes (k adaptativo 1..4) con turnos >= 15s (zona IVR
     excluida del voto; igual se guardan como Rol OTRO / Sistema:).
  4. Matchea cada hablante contra A_Voz (misma ModeloVersion). Con umbral
     por persona: TECNICO nombrado; sin match: Hablante A/B, Rol OTRO.
  5. Inserta A_Conversacion + Turnos + R_Conversacion_Voz (idempotente
     por CdrId UNIQUE). A_Llamada_Id es best-effort (puede quedar NULL).

Uso:
    python -m cli.atribuir_voz [--desde YYYY-MM-DD] [--hasta YYYY-MM-DD]
                               [--limite N] [--semilla]
"""

import os
from collections import Counter
from datetime import date, datetime, timedelta

import numpy as np

from config.settings import (
    VOZ_AUDIO_ROOT,
    VOZ_DIAS_ATRAS,
    VOZ_LIMITE,
    VOZ_MODELO_VERSION,
    VOZ_UMBRAL_CANDIDATO,
    VOZ_UMBRAL_RECHAZO,
    VOZ_VOICEPRINTS_JSON,
    VOZ_WHISPER_BEAM,
    VOZ_WHISPER_MODEL,
)
from etl.conn.anura_client import AnuraClient
from etl.conn.db import get_engine
from etl.dominios.voz import audio as A
from etl.dominios.voz import modelos as M
from etl.dominios.voz.roster import cargar_roster
from sqlalchemy import text

# Umbrales iniciales por persona (calibrados en analisis: intra vs inter).
# Se guardan en A_Voz.Umbral; esto solo rige el seed (despues manda la DB).
# La lista de claves ES el roster canonico de tecnicos a identificar.
# Dato personal: vive en config/roster.json (ignorado por git), NO en el codigo.
# Los no calibrados van en 0.55 = default del seed (sin cambio de conducta);
# al calibrar uno, se baja a su valor medido (rango 0.50-0.60).
_ROSTER = cargar_roster()
UMBRALES_SEED = _ROSTER["umbrales"]
EXCLUIDOS_ROSTER = set(_ROSTER["excluidos"])


def es_tecnico_roster(nombre, roster=None, excluidos=None):
    """True si el nombre es (o empieza como) un tecnico del roster.

    Compara normalizado contra nombre completo Y nombre de pila: un cliente
    que dice "soy <nombre>" no debe bautizar una voz cliente aunque la voz de
    ese tecnico no exista todavia. Un cliente realmente con ese nombre queda
    para revision humana (un nombre equivocado es peor que un hueco).
    Los nombres en `excluidos` no cuentan (atienden pero no son del roster).
    """
    roster = UMBRALES_SEED if roster is None else roster
    excluidos = EXCLUIDOS_ROSTER if excluidos is None else set(excluidos)
    clave = _norm_nombre(nombre)
    if not clave:
        return False
    for r in roster:
        if r in excluidos:
            continue
        nr = _norm_nombre(r)
        if clave == nr or clave == nr.split()[0]:
            return True
    return False


def vec_para_db(vec):
    return M.vec_str(vec)


def cargar_plantillas(conn, modelo_version):
    """[(nombre, centroide, umbral, voz_id)] plantillas REALES de esa version.

    Solo voces de TECNICO con clips (plantillas seed verificadas). Sin el
    filtro EsTecnico se usaban como plantilla voces de cliente/basura
    (p.ej. una llamada 'Cliente'): cualquier hablante que matcheaba esa
    basura quedaba como Rol='TECNICO' con el VozId de un cliente.
    """
    rows = conn.execute(
        text("SELECT Id, Nombre, Centroide, Umbral FROM dbo.A_Voz "
             "WHERE Activo = :a AND EsTecnico = :et AND Clips > 0 "
             "AND ModeloVersion = :v"),
        {"a": 1, "et": 1, "v": modelo_version},
    ).fetchall()
    out = []
    for voz_id, nombre, centroide, umbral in rows:
        v = M.parse_vector(centroide)
        v = v / (np.linalg.norm(v) or 1.0)
        out.append((nombre, v.astype(np.float32), float(umbral), int(voz_id)))
    return out


def cargar_operadora(conn, modelo_version):
    """[(nombre, centroide, voz_id)] voces de SISTEMA (contestador/operadora).

    EsTecnico=0: NUNCA compiten como tecnico (cargar_plantillas las excluye).
    Se usan solo para detectar buzon: si toda la voz votante es operadora,
    la llamada se marca EsBuzon automaticamente (pedido 2026-10-08).
    """
    rows = conn.execute(
        text("SELECT Id, Nombre, Centroide FROM dbo.A_Voz "
             "WHERE Activo = :a AND EsTecnico = 0 AND Clips > 0 "
             "AND ModeloVersion = :v AND Nombre IN (:n1, :n2)"),
        {"a": 1, "v": modelo_version, "n1": "Operadora", "n2": "Buzon Claro"},
    ).fetchall()
    out = []
    for voz_id, nombre, centroide in rows:
        v = M.parse_vector(centroide)
        v = v / (np.linalg.norm(v) or 1.0)
        out.append((nombre, v.astype(np.float32), int(voz_id)))
    return out


# Frases que solo dice el contestador/operadora, nunca un humano en una
# conversacion real (criterio curado de las frases de buzon). "no esta
# disponible" NO vale: la dicen clientes reales.
FRASES_BUZON = (
    "casilla de mensajes",
    "te comunicaste con la casilla",
    "deja tu mensaje",
    "despues del tono",
    "bienvenido al buzon",
    "buzon de voz",
)


def detectar_buzon(textos, hay_match_tec, etiqs_votantes, etiqs_operadora):
    """True si contestador/operadora -> auto-EsBuzon (pedido 2026-10-08).

    Solo cuando NO hay match tecnico (si un tecnico matcheo, es conversacion
    real aunque se mencione el buzon). Condiciones (OR):
      - toda la voz votante matchea voz operadora, o
      - frase de sistema (normalizada sin acentos) en llamada corta
        (<=12 segmentos: las validadas tienen 1-3; una real que menciona la
        casilla es larga).
    Pura (sin DB): testeada en tests/test_voz.py.
    """
    if hay_match_tec:
        return False
    if etiqs_votantes and set(etiqs_votantes) <= set(etiqs_operadora or ()):
        return True
    txt = _norm_nombre(" ".join(textos or ()))
    if any(f in txt for f in FRASES_BUZON) and len(textos or ()) <= 12:
        return True
    return False


def _norm_nombre(s):
    """minusculas sin acentos ni espacios dobles, para comparar nombres.

    Jose Nunez == José Núñez. Sin esto, seed_voiceprints creaba un
    duplicado por cada tilde del pack.
    """
    import unicodedata
    t = unicodedata.normalize("NFKD", str(s or "")).encode(
        "ascii", "ignore").decode().lower()
    return " ".join(t.split())


def seed_voiceprints(engine, json_path, umbrales, modelo_version, forzar=False):
    """Upsert de plantillas desde voiceprints.json del pack.

    Dos guards, ambos por incidentes reales:

    1. El match es por nombre NORMALIZADO (sin acentos). Antes era `Nombre = :n`
       exacto, asi que el pack ("Nombre Apellido" con tilde) no encontraba la fila
       que el listening habia creado ("Nombre Apellido") y cada `--semilla`
       insertaba un duplicado. Con dos filas de la misma persona, el matcher
       elige la que mas se parezca y el nombre sale de una u otra.

    2. NO degrada una plantilla: si la fila ya tiene MAS clips que el pack, fue
       reconstruida escuchando audio verificado y se saltea (imprime aviso).
       Sin esto, un seed borro 4 clips de un tecnico y lo dejo con 1.
       `forzar=True` pisa igual.
    """
    import json as _json

    data = _json.load(open(json_path, encoding="utf-8"))
    n = nu = nsaltadas = 0
    with engine.begin() as conn:
        # {nombre normalizado: (id, nombre real, clips)}
        act = {}
        for vid, nombre, clips in conn.execute(
            text("SELECT Id, Nombre, Clips FROM dbo.A_Voz WHERE ModeloVersion = :v"),
            {"v": modelo_version},
        ).fetchall():
            act.setdefault(_norm_nombre(nombre), (int(vid), nombre, int(clips or 0)))
        # voiceprints.json: {nombre: {centroide: [...], ...}} ; ignora claves meta
        for nombre, info in data.items():
            if not isinstance(info, dict) or "centroide" not in info:
                continue
            centroide = vec_para_db(np.asarray(info["centroide"], dtype=np.float32))
            umbral = float(umbrales.get(nombre, 0.55))
            clips_raw = info.get("clips", info.get("turnos", 0)) or 0
            clips = len(clips_raw) if isinstance(clips_raw, list) else int(clips_raw)
            clave = _norm_nombre(nombre)
            hit = act.get(clave)
            if hit and not forzar and hit[2] > clips:
                print(f"  seed omite {nombre!r}: la BD tiene {hit[2]} clips "
                      f"y el pack solo {clips} (reconstruida escuchando)")
                nsaltadas += 1
                continue
            if hit:
                if hit[1] != nombre:
                    print(f"  seed match por nombre normalizado: pack {nombre!r} "
                          f"= BD {hit[1]!r} (voz {hit[0]})")
                conn.execute(
                    text("UPDATE dbo.A_Voz SET Centroide = :c, Umbral = :u, Clips = :k, "
                         "EsTecnico = :et, Activo = :a, FechaActualizacion = SYSUTCDATETIME() "
                         "WHERE Id = :i"),
                    {"c": centroide, "u": umbral, "k": clips, "et": 1, "a": 1, "i": hit[0]},
                )
                nu += 1
            else:
                nuevo = int(conn.execute(
                    text("INSERT INTO dbo.A_Voz (Nombre, EsTecnico, Centroide, "
                         "ModeloVersion, Umbral, Clips) "
                         "OUTPUT INSERTED.Id VALUES (:n, :et, :c, :v, :u, :k)"),
                    {"n": nombre, "et": 1, "c": centroide, "v": modelo_version,
                     "u": umbral, "k": clips},
                ).scalar())
                act[clave] = (nuevo, nombre, clips)
                print(f"  seed NUEVA voz {nuevo}: {nombre!r} ({clips} clips)")
                nu += 1
            n += 1
    extra = f", {nsaltadas} saltadas (no degradar)" if nsaltadas else ""
    print(f"seed A_Voz: {n} plantillas ({modelo_version}), {nu} upsert{extra}")
    return n


def resolver_llamada_id(conn, fecha, telefono):
    ini = fecha - timedelta(seconds=2)
    fin = fecha + timedelta(seconds=2)
    return conn.execute(
        text("SELECT TOP 1 Id FROM dbo.A_Llamada "
             "WHERE FechaDT >= :a AND FechaDT <= :b "
             "AND Nro_Origen = :t AND Direccion = :d ORDER BY FechaDT"),
        {"a": ini, "b": fin, "t": telefono, "d": "IN"},
    ).scalar()


def formatear_linea(rol, nombre, etiqueta, texto):
    if rol == "TECNICO" and nombre:
        return f"Tecnico [{nombre}]: {texto}"
    if rol == "CLIENTE":
        return f"Cliente: {texto}"
    if rol == "OTRO" and nombre:
        return f"Sistema [{nombre}]: {texto}"
    if etiqueta == "X":
        return f"Sistema: {texto}"
    return f"Hablante {etiqueta}: {texto}"


def procesar_llamada(client, engine, conv, plantillas, wm, spk,
                     beam, modelo_tr, modelo_ver, access_token=None, progreso=None):
    """Procesa UNA conversacion completa. Devuelve dict resumen o None si saltea.

    `progreso(etapa, detalle)`: callback opcional para mostrar avance en vivo.
    """
    _p = progreso or (lambda *a, **k: None)
    with engine.connect() as conn:
        ya = conn.execute(
            text("SELECT Id FROM dbo.A_Conversacion WHERE CdrId = :c"),
            {"c": conv["cdrId"]},
        ).scalar()
    if ya:
        return {"estado": "existe", "cdrId": conv["cdrId"]}
    # Guard hermana: la otra pierna (IN<->OUT) de la misma llamada ya pudo
    # entrar por el fallback. Misma llamada = misma fecha, duracion Y telefono
    # (sin el telefono, dos llamadas simultaneas de igual duracion dan falso
    # positivo: caso de dos simultáneas el 2026-10-02). Si el telefono
    # no se conoce de algun lado, se frena igual (conservador).
    with engine.connect() as conn:
        gemela = conn.execute(
            text("SELECT TOP 1 c2.CdrId FROM dbo.A_Conversacion c2 "
                 "LEFT JOIN dbo.A_Llamada l2 ON l2.Id=c2.A_Llamada_Id "
                 "WHERE c2.CdrId <> :c AND c2.DuracionSeg BETWEEN :d0 AND :d1 "
                 "AND c2.Fecha BETWEEN DATEADD(second, -300, :f) "
                 "AND DATEADD(second, 300, :f) "
                 "AND (l2.Nro_Origen = :t OR l2.Nro_Origen IS NULL)"),
            {"c": conv["cdrId"], "d0": conv["duration"] - 15,
             "d1": conv["duration"] + 15, "f": conv["fecha"],
             "t": conv.get("calling") or ""},
        ).scalar()
    if gemela:
        return {"estado": "existe-hermana", "cdrId": conv["cdrId"],
                "hermana": int(gemela)}

    _p("descargando")
    url = A.url_ogg(client, conv["cdrId"], access_token)
    if not url:
        return {"estado": "sin_url", "cdrId": conv["cdrId"]}
    dia = conv["fecha"].strftime("%Y-%m-%d")
    destino = A.ruta_audio(VOZ_AUDIO_ROOT, dia, conv["cdrId"], conv["calling"], conv["fecha"])
    try:
        A.descargar_ogg(url, destino)
    except Exception as exc:
        return {"estado": f"error_audio: {exc}", "cdrId": conv["cdrId"]}

    x = M.decode_audio(destino)
    if x.size < M.SR * 3:
        return {"estado": "audio_corto", "cdrId": conv["cdrId"]}
    mm = int(x.size // M.SR) // 60
    ss = int(x.size // M.SR) % 60
    _p("transcribiendo", f"{mm}:{ss:02d} de audio")
    segs, met = M.transcribir_con_metricas(wm, x, beam_size=beam)
    if not segs:
        return {"estado": "sin_turnos", "cdrId": conv["cdrId"]}
    dur_audio = x.size / M.SR
    cps = sum(len(t) for _a, _b, t in segs) / max(dur_audio, 1.0)
    tpm = len(segs) / max(dur_audio / 60.0, 1 / 60.0)
    pct_basura = (met["basura"] / met["raw"]) if met["raw"] else None
    calidad = M.calificar(cps, tpm)
    _p("identificando voces", f"{len(segs)} turnos ({calidad})")
    _p("identificando voces", f"{len(segs)} turnos")
    # Embeddings por CANAL DOMINANTE (piernas Anura). El downmix mono lava la
    # voz del tecnico cuando su pierna es mas debil (medido: 0,75 en mono
    # vs 0,37 en el canal correcto). Ver docs/canales-estereo.md.
    L, R = M.decode_estereo(destino)
    embs, canales_seg = M.embeber_estereo(spk, L, R, segs)

    # turnos votantes: terminan despues del IVR y son embebibles.
    # OJO: es por FIN (> IVR_SEG), no por inicio: un tecnico que arranca en
    # el seg 11 con IVR corto quedaba afuera y la llamada entera moria en X
    # (caso real: t3 10.88-20.26 matcheaba la plantilla equivocada 0.332).
    idx_voto = [i for i, (_a, b, _t) in enumerate(segs)
                if b > M.IVR_SEG and embs[i] is not None]
    grupos = {}  # etiqueta -> [indices de segs]
    if idx_voto:
        lab = M.diarizar([embs[i] for i in idx_voto])
        letras = "ABCD"
        for i, l in zip(idx_voto, lab):
            grupos.setdefault(letras[(l - 1) % 4], []).append(i)

    # match por grupo contra plantillas
    info_grupo = {}
    nearest = {}      # et -> (voz mas cercana, dist) aunque se rechace
    canal_grupo = {}  # et -> canal dominante (L/R) de esa voz
    for et, idxs in grupos.items():
        V = np.array([embs[i] for i in idxs])
        cen = V.mean(0)
        cen /= np.linalg.norm(cen) or 1.0
        nombre, dist = M.match_plantilla(cen, [(p[0], p[1], p[2]) for p in plantillas]) \
            if plantillas else (None, 9.0)
        nearest[et] = (nombre, float(dist))
        chs = [canales_seg[i] for i in idxs if canales_seg[i]]
        canal_grupo[et] = Counter(chs).most_common(1)[0][0] if chs else None
        umbral = next((p[2] for p in plantillas if p[0] == nombre), 0.55)
        voz_id = next((p[3] for p in plantillas if p[0] == nombre), None)
        # Banda de rechazo: si el tecnico mas cercano esta lejos, NO es el.
        # Sin esto, una llamada de un tecnico SIN plantilla se le atribuye al
        # mas parecido de los conocidos (falso positivo con nombre inventado).
        if nombre is not None and dist > VOZ_UMBRAL_RECHAZO:
            nombre = None
        if nombre is None or dist > umbral:
            info_grupo[et] = (None, None, float(dist), False)
        else:
            info_grupo[et] = (nombre, voz_id, float(dist), True)

    # roles: el grupo matchado con mas turnos = principal; si hay match,
    # los demas no-matchados son CLIENTE; sin ningun match: Hablante/OTRO.
    # Grupos de operadora/buzon van OTRO (con su VozId) aunque haya match.
    hay_match = any(v[3] for v in info_grupo.values())
    with engine.connect() as _co:
        op_plant = cargar_operadora(_co, modelo_ver)
    op_grupo = {}  # etiqueta -> (nombre, voz_id, dist)
    for et, idxs in grupos.items():
        V = np.array([embs[i] for i in idxs])
        cen = V.mean(0)
        cen /= np.linalg.norm(cen) or 1.0
        onombre, odist = M.match_plantilla(
            cen, [(p[0], p[1], 0.45) for p in op_plant]) if op_plant else (None, 9.0)
        if onombre is not None and odist <= 0.45:
            op_grupo[et] = (onombre,
                            next(p[2] for p in op_plant if p[0] == onombre),
                            float(odist))
    orden = sorted(grupos, key=lambda e: -len(grupos[e]))
    principal = None
    for et in orden:
        if info_grupo[et][3]:
            principal = et
            break
    roles = {}
    for et in grupos:
        if et in op_grupo:
            roles[et] = "OTRO"
        elif info_grupo[et][3]:
            roles[et] = "TECNICO"
        elif hay_match:
            roles[et] = "CLIENTE"
        else:
            roles[et] = "OTRO"
    es_buzon = detectar_buzon([t for _a, _b, t in segs], hay_match,
                               list(grupos), list(op_grupo))

    # Candidato para la cola de revision: mejor voz cercana aunque la banda de
    # rechazo la descarte. No atribuye; solo se guarda para escuchar.
    candidato = None  # (nombre, dist, et, canal)
    if principal is None and nearest:
        cands = sorted((d, n, et) for et, (n, d) in nearest.items() if n)
        if cands and cands[0][0] <= VOZ_UMBRAL_CANDIDATO:
            _d, _n, _et = cands[0]
            candidato = (_n, float(_d), _et, canal_grupo.get(_et))
    canal_tec = canal_grupo.get(principal) if principal is not None else (
        candidato[3] if candidato else None)
    _p("match", " | ".join(
        f"{et}/{canal_grupo.get(et) or '-'} {nearest[et][0] or '?'} "
        f"{nearest[et][1]:.2f}" for et in grupos))

    # filas de turnos (TODOS los segmentos, incl. pre-IVR como OTRO/X)
    filas_turno, lineas = [], []
    for n, (a, b, t) in enumerate(segs, 1):
        etiq = "X"
        for e, ix in grupos.items():
            if (n - 1) in ix:
                etiq = e
                break
        if etiq == "X":
            rol, voz_id, nombre = "OTRO", None, None
        else:
            rol = roles[etiq]
            if rol == "OTRO" and etiq in op_grupo:
                nombre, voz_id = op_grupo[etiq][0], op_grupo[etiq][1]
            elif rol == "OTRO":
                voz_id, nombre = None, None
            else:
                nombre, voz_id = info_grupo[etiq][0], info_grupo[etiq][1]
        filas_turno.append({"nro": n, "ini": a, "fin": b, "texto": t,
                            "etiq": etiq, "rol": rol, "voz_id": voz_id,
                            "nombre": nombre})
        lineas.append(formatear_linea(rol, nombre, etiq, t))

    # Re-transcribir el canal del TECNICO: whisper sobre el mono descarta sus
    # turnos cuando su pierna es mas debil (medido en un caso real).
    # Se recupera el texto del canal propio, sin el filtro de duracion.
    if principal is not None:
        for i in grupos[principal]:
            a, b, t = segs[i]
            ch = canales_seg[i] or "L"
            sig = L if ch == "L" else R
            txt2 = M.transcribir_ventana(wm, sig[int(a * M.SR):int(b * M.SR)])
            if txt2 and len(txt2) > len(t or ""):
                filas_turno[i]["texto"] = txt2
                lineas[i] = formatear_linea(
                    "TECNICO", filas_turno[i]["nombre"],
                    filas_turno[i]["etiq"], txt2)

    # embedding lado tecnico (para re-match futuro sin audio)
    emb_tec = None
    if principal is not None:
        V = np.array([embs[i] for i in grupos[principal]])
        emb_tec = (V.mean(0) / (np.linalg.norm(V.mean(0)) or 1.0)).astype(np.float32)

    # A_Llamada_Id best-effort
    with engine.connect() as conn:
        llamada_id = resolver_llamada_id(conn, conv["fecha"], conv["calling"])

    # INSERTs
    _p("guardando", f"conv #{conv['cdrId']}")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO dbo.A_Conversacion (CdrId, A_Llamada_Id, Fecha, "
                 "DuracionSeg, RutaAudio, Transcripto, EmbTecnico, "
                 "ModeloTranscripcion, ModeloVersion, CalidadTransc, PctBasura, "
                 "EsBuzon, CanalTecnico, CandidatoTecnico, CandidatoDist) "
                 "VALUES (:c, :l, :f, :d, :r, :t, :e, :mt, :mv, :q, :pb, :bz, "
                 ":cn, :ctn, :ctd)"),
            {"c": conv["cdrId"], "l": llamada_id, "f": conv["fecha"],
             "d": conv["duration"],
             # relativa con / (portable Windows<->Linux/Azure)
             "r": os.path.relpath(destino, VOZ_AUDIO_ROOT).replace(os.sep, "/"),
             "t": "\n".join(lineas),
             "e": vec_para_db(emb_tec) if emb_tec is not None else None,
             "mt": f"faster-whisper-{wm_name}", "mv": modelo_ver,
             "q": calidad, "pb": pct_basura, "bz": 1 if es_buzon else 0,
             "cn": canal_tec,
             "ctn": candidato[0] if candidato else None,
             "ctd": candidato[1] if candidato else None},
        )
        conv_id = conn.execute(
            text("SELECT Id FROM dbo.A_Conversacion WHERE CdrId = :c"),
            {"c": conv["cdrId"]},
        ).scalar()
        if filas_turno:
            conn.execute(
                text("INSERT INTO dbo.A_ConversacionTurno (ConversacionId, Nro, "
                     "InicioSeg, FinSeg, VozId, EtiquetaLocal, Rol, Texto) "
                     "VALUES (:cid, :nro, :ini, :fin, :voz, :etiq, :rol, :texto)"),
                [{"cid": conv_id, "nro": f["nro"], "ini": f["ini"], "fin": f["fin"],
                  "voz": f["voz_id"], "etiq": f["etiq"], "rol": f["rol"],
                  "texto": f["texto"]} for f in filas_turno],
            )
        # R por grupo (incluye OTRO/X agregado como CLIENTE? no: X va OTRO.
        # Grupos operadora llevan su VozId (EsTecnico=0) con ALTA/MEDIA, nunca
        # principal: el join de reporting los filtra (solo EsTecnico=1).
        agg = Counter()
        for f in filas_turno:
            agg[(f["etiq"], f["rol"], f["voz_id"])] += 1
        dist_grupo = {}
        for et in grupos:
            if info_grupo[et][3]:
                dist_grupo[et] = info_grupo[et][2]
            elif et in op_grupo:
                dist_grupo[et] = op_grupo[et][2]
        for (etiq, rol, voz_id), k in agg.items():
            segs_g = sum(f["fin"] - f["ini"] for f in filas_turno
                         if f["etiq"] == etiq and f["rol"] == rol)
            if voz_id is not None:
                dist = dist_grupo.get(etiq, 9.0)
                conf = "ALTA" if dist <= 0.45 else "MEDIA"
                es_p = 1 if etiq == principal else 0
            else:
                dist, conf, es_p = None, "BAJA", 0
            conn.execute(
                text("INSERT INTO dbo.R_Conversacion_Voz (ConversacionId, EtiquetaLocal, "
                     "VozId, Rol, EsPrincipal, Confianza, Distancia, Turnos, Segundos) "
                     "VALUES (:cid, :etiq, :voz, :rol, :p, :cf, :d, :k, :s)"),
                {"cid": conv_id, "etiq": etiq, "voz": voz_id, "rol": rol,
                 "p": es_p, "cf": conf, "d": dist, "k": k, "s": segs_g},
            )
    nombre_p = info_grupo.get(principal, (None,))[0] if principal else None
    # Canal del tecnico (solo reporta: alimenta el corpus para decidir si la
    # regla IN->L / OUT->R se puede fijar; ver docs/canales-estereo.md).
    canal = None
    if principal is not None and info_grupo.get(principal, (None, None, None, False))[3]:
        try:
            tpl = next(p[1] for p in plantillas
                       if p[0] == info_grupo[principal][0])
            canal = M.canal_del_tecnico(destino, tpl)["lado"]
        except Exception:
            canal = None
    if canal is None:
        canal = canal_tec
    return {"estado": "ok", "cdrId": conv["cdrId"], "conv_id": conv_id,
            "tecnico": nombre_p, "turnos": len(filas_turno),
            "llamada_id": llamada_id, "canal": canal, "buzon": es_buzon,
            "candidato": (candidato[0], candidato[1]) if candidato else None}


wm_name = VOZ_WHISPER_MODEL


# --- Paralelismo (backfill) -------------------------------------------------
# Cada worker carga sus propios modelos una vez (whisper + ECAPA pesan
# ~700 MB; no se pueden picklear) y su propio engine (los pools de
# conexiones no se comparten entre procesos). El token GCAPI lo reparte el
# padre por envio; ante 401 el padre renueva y re-envia UNA vez (igual que
# en secuencial). workers=1 = camino secuencial de siempre, sin cambios.
_W = {}


def _worker_init():
    from etl.conn.anura_client import AnuraClient as _AC
    from etl.conn.db import get_engine as _ge

    _W["wm"] = M.get_whisper(VOZ_WHISPER_MODEL)
    _W["spk"] = M.get_speaker()
    _W["engine"] = _ge()
    _W["client"] = _AC()


def _worker_una(payload):
    """Procesa UNA conversacion en el worker. Devuelve (cdrId, resultado).

    Los errores viajan como tupla (no se pueden picklear tracebacks):
    ("ok", r) | ("falla", "Tipo: msg") | ("falla401", "msg").
    """
    import requests as _rq

    conv, plantillas, beam, modelo_tr, modelo_ver, token = payload
    try:
        r = procesar_llamada(_W["client"], _W["engine"], conv, plantillas,
                             _W["wm"], _W["spk"], beam, modelo_tr, modelo_ver,
                             token)
        return (conv["cdrId"], "ok", r)
    except Exception as exc:
        if isinstance(exc, _rq.HTTPError) and exc.response is not None \
                and exc.response.status_code == 401:
            return (conv["cdrId"], "falla401", str(exc)[:160])
        return (conv["cdrId"], "falla", f"{type(exc).__name__}: {exc}"[:160])


def main(desde=None, hasta=None, limite=None, semilla=False, stats=None,
         progreso=None, forzar_seed=False, workers=1):
    """Procesa un rango de fechas. `progreso(etapa, detalle, conv)` es opcional."""
    engine = get_engine()
    if semilla:
        seed_voiceprints(engine, VOZ_VOICEPRINTS_JSON, UMBRALES_SEED, VOZ_MODELO_VERSION,
                         forzar=forzar_seed)
    limite = VOZ_LIMITE if limite is None else limite
    hoy = date.today()
    if hasta is None:
        hasta = hoy
    if desde is None:
        desde = hasta - timedelta(days=VOZ_DIAS_ATRAS)
    dias = []
    d = desde
    while d <= hasta:
        dias.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    client = AnuraClient()
    access_token = client.get_access_token()
    plantillas = []
    with engine.connect() as conn:
        plantillas = cargar_plantillas(conn, VOZ_MODELO_VERSION)
    print(f"plantillas activas ({VOZ_MODELO_VERSION}): {len(plantillas)}")

    wm = M.get_whisper(VOZ_WHISPER_MODEL)
    spk = M.get_speaker()
    beam = VOZ_WHISPER_BEAM

    ok = nombradas = sin_match = 0
    errores = []
    hechos = 0
    pool = None
    if workers and workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        pool = ProcessPoolExecutor(max_workers=workers,
                                   initializer=_worker_init)
    try:
        for dia in dias:
            try:
                convs = A.conversaciones_grabadas(client, dia, access_token)
            except Exception:
                access_token = client.get_access_token()
                convs = A.conversaciones_grabadas(client, dia, access_token)
            total_dia = len(convs)
            if pool is None:
                for i_conv, conv in enumerate(convs, 1):
                    if hechos >= limite:
                        break
                    hechos, ok, nombradas, sin_match, access_token = _uno(
                        conv, i_conv, total_dia, dia, client, engine,
                        plantillas, wm, spk, beam, progreso, access_token,
                        hechos, ok, nombradas, sin_match, errores)
            else:
                hechos, ok, nombradas, sin_match, access_token = _lote(
                    pool, client, convs, total_dia, dia, engine, plantillas,
                    beam, progreso, access_token, limite,
                    hechos, ok, nombradas, sin_match, errores)
            if hechos >= limite:
                break
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    print(f"\nconversaciones: {ok} ok | con tecnico: {nombradas} | "
          f"sin match: {sin_match} | errores: {len(errores)}")
    for c, e in errores[:10]:
        print(f"  err cdr{c}: {e}")
    return {"ok": ok, "nombradas": nombradas, "sin_match": sin_match,
            "errores": errores}


def _uno(conv, i_conv, total_dia, dia, client, engine, plantillas, wm, spk,
         beam, progreso, access_token, hechos, ok, nombradas, sin_match,
         errores):
    """Camino secuencial de siempre (una conversacion). Devuelve contadores."""

    def _prog(etapa, detalle=""):
        if progreso:
            progreso(etapa, detalle, {"cdrId": conv["cdrId"], "i": i_conv,
                                      "n": total_dia, "dia": dia})

    try:
        r = procesar_llamada(client, engine, conv, plantillas, wm, spk,
                             beam, VOZ_WHISPER_MODEL, VOZ_MODELO_VERSION,
                             access_token, progreso=_prog)
    except Exception as exc:
        # 401 = sesion GCAPI vencida (pasa tras varios minutos de
        # transcripcion): renueva token y reintenta UNA vez.
        import requests as _rq

        es_401 = isinstance(exc, _rq.HTTPError) and \
            exc.response is not None and exc.response.status_code == 401
        if not es_401:
            errores.append((conv["cdrId"], f"{type(exc).__name__}: {exc}"[:160]))
            _prog("ERROR", f"{type(exc).__name__}")
            return hechos, ok, nombradas, sin_match, access_token
        try:
            access_token = client.get_access_token()
            r = procesar_llamada(client, engine, conv, plantillas, wm, spk,
                                 beam, VOZ_WHISPER_MODEL, VOZ_MODELO_VERSION,
                                 access_token, progreso=_prog)
        except Exception as exc2:
            errores.append((conv["cdrId"], f"reintento: {exc2}"[:160]))
            _prog("ERROR", f"reintento: {exc2}"[:80])
            return hechos, ok, nombradas, sin_match, access_token
    hechos, ok, nombradas, sin_match = _anotar(
        r, conv, hechos, ok, nombradas, sin_match, errores, _prog)
    return hechos, ok, nombradas, sin_match, access_token


def _anotar(r, conv, hechos, ok, nombradas, sin_match, errores, _prog):
    """Contabiliza un resultado (mutates errores). Devuelve contadores."""
    est = r["estado"]
    if est == "ok":
        hechos += 1
        ok += 1
        if r.get("tecnico"):
            nombradas += 1
        else:
            sin_match += 1
        print(f"  ok cdr{conv['cdrId']} tec={r.get('tecnico') or '?'} "
              f"turnos={r.get('turnos')} llamada={r.get('llamada_id')} "
              f"canal={r.get('canal')}")
        _prog("OK", f"tec={r.get('tecnico') or '?'} turnos={r.get('turnos')}")
    elif est in ("existe", "existe-hermana"):
        pass  # ya estaba en base (idempotente): no mole
    else:
        errores.append((conv["cdrId"], est))
        _prog("ERROR", est)
    return hechos, ok, nombradas, sin_match


def _lote(pool, client, convs, total_dia, dia, engine, plantillas, beam,
          progreso, access_token, limite, hechos, ok, nombradas, sin_match,
          errores):
    """Un dia en paralelo: envia, y ante 401 renueva token y re-envia 1 vez."""
    from concurrent.futures import as_completed

    pendientes = [c for c in convs][:max(limite - hechos, 0)]
    futs = {pool.submit(_worker_una,
                        (c, plantillas, beam, VOZ_WHISPER_MODEL,
                         VOZ_MODELO_VERSION, access_token)): c
            for c in pendientes}
    reenviados = set()
    while futs:
        for fut in as_completed(list(futs)):
            conv = futs.pop(fut)
            try:
                cdr_id, estado, dato = fut.result()
            except Exception as exc:  # el worker murio: cuenta como error
                errores.append((conv["cdrId"],
                                f"worker: {type(exc).__name__}"[:160]))
                continue

            def _prog(etapa, detalle=""):
                if progreso:
                    progreso(etapa, detalle, {"cdrId": conv["cdrId"],
                                              "n": total_dia, "dia": dia})

            if estado == "falla401" and conv["cdrId"] not in reenviados:
                access_token = client.get_access_token()
                reenviados.add(conv["cdrId"])
                futs[pool.submit(_worker_una,
                                 (conv, plantillas, beam, VOZ_WHISPER_MODEL,
                                  VOZ_MODELO_VERSION, access_token))] = conv
                continue
            if estado == "falla401":
                errores.append((conv["cdrId"], f"reintento: {dato}"[:160]))
                continue
            if estado == "falla":
                errores.append((conv["cdrId"], dato[:160]))
                continue
            hechos, ok, nombradas, sin_match = _anotar(
                dato, conv, hechos, ok, nombradas, sin_match, errores, _prog)
    return hechos, ok, nombradas, sin_match, access_token


def reprocesar_rango(desde=None, hasta=None, limite=None, progreso=None):
    """Re-atribuye conversaciones YA cargadas con las plantillas actuales.

    Caso de uso: el dia X entro con desconocidos y DESPUES se enrolo la voz
    del tecnico -> se pasa de nuevo y ahora si la toma, sin re-descargar
    ni re-transcribir (usa turnos guardados + audio en disco).

    Sin limite por defecto (None = todo el rango): es una pasada de puesta
    al dia deliberada, no un batch nocturno.

    Reglas (solo rellena huecos, nunca degrada):
      - Solo grupos con VozId NULL (lo nombrado o rechazado no se toca).
      - Match con las mismas reglas del pipeline: rechazo VOZ_UMBRAL_RECHAZO,
        umbral por persona, ALTA si dist <= 0.45.
      - Principal: si no hay principal nombrado y un grupo matcha, el de
        mas turnos. Los OTRO no se tocan (a diferencia del pipeline fresco,
        que los marca CLIENTE: aca no se afirma lo que no se midio).
    """
    from config.settings import VOZ_MODELO_VERSION

    engine = get_engine()
    hoy = date.today()
    if hasta is None:
        hasta = hoy
    if desde is None:
        desde = hasta - timedelta(days=7)
    with engine.connect() as conn:
        plantillas = cargar_plantillas(conn, VOZ_MODELO_VERSION)
    print(f"plantillas activas ({VOZ_MODELO_VERSION}): {len(plantillas)}")
    if not plantillas:
        print("sin plantillas: nada que re-atribuir")
        return {"ok": 0, "nombradas": 0, "sin_match": 0, "errores": []}

    spk = M.get_speaker()
    with engine.connect() as conn:
        convs = conn.execute(text(
            "SELECT co.Id, co.CdrId, co.RutaAudio FROM dbo.A_Conversacion co "
            "WHERE co.Fecha >= :a AND co.Fecha < :b "
            "AND EXISTS (SELECT 1 FROM dbo.R_Conversacion_Voz r "
            "WHERE r.ConversacionId = co.Id AND r.VozId IS NULL) "
            "ORDER BY co.Fecha"), {
                "a": datetime(desde.year, desde.month, desde.day),
                "b": datetime(hasta.year, hasta.month, hasta.day) + timedelta(days=1),
            }).fetchall()
    print(f"conversaciones con huecos de voz: {len(convs)}")
    lista = convs[:limite] if limite else convs
    if limite and len(convs) > limite:
        print(f"  limite {limite}: se procesan las primeras {limite} "
              f"(quedan {len(convs) - limite} para la proxima pasada)")
    ok = nombradas = sin_match = 0
    errores = []
    for i_conv, (cid, cdr, ruta) in enumerate(lista, 1):
        if progreso:
            progreso("re-match", f"{len(lista)}", {"cdrId": cdr, "i": i_conv,
                                                   "n": len(lista)})
        with engine.connect() as conn:
            filas = conn.execute(text(
                "SELECT Id, EtiquetaLocal, VozId, EsPrincipal FROM dbo.R_Conversacion_Voz "
                "WHERE ConversacionId = :c"), {"c": cid}).fetchall()
            turnos = conn.execute(text(
                "SELECT EtiquetaLocal, InicioSeg, FinSeg, Texto "
                "FROM dbo.A_ConversacionTurno WHERE ConversacionId = :c ORDER BY Nro"),
                {"c": cid}).fetchall()
        nulos = [f for f in filas if f[2] is None]
        if not nulos:
            continue
        ogg = os.path.join(VOZ_AUDIO_ROOT, (ruta or "").replace("/", os.sep))
        if not ruta or not os.path.exists(ogg):
            errores.append((cdr, "sin audio en disco"))
            continue
        L, R = M.decode_estereo(ogg)
        matches = []
        nearest = []
        canal_etq = {}
        for _rid, etq, _v, _pr in nulos:
            if etq == "X":
                continue
            ss = [(a, b, t) for e, a, b, t in turnos
                  if e == etq and b > M.IVR_SEG and (b - a) >= 1.6]
            if not ss:
                continue
            vecs, chs = M.embeber_estereo(spk, L, R, ss)
            chs = [c for c in chs if c]
            if chs:
                canal_etq[etq] = Counter(chs).most_common(1)[0][0]
            vecs = [v for v in vecs if v is not None]
            if not vecs:
                continue
            cen = np.array(vecs).mean(0)
            cen /= np.linalg.norm(cen) or 1.0
            nombre, dist = M.match_plantilla(
                cen, [(p[0], p[1], p[2]) for p in plantillas])
            nearest.append((float(dist), nombre, etq))
            umbral = next((p[2] for p in plantillas if p[0] == nombre), 0.55)
            voz_id = next((p[3] for p in plantillas if p[0] == nombre), None)
            if nombre is None or dist > VOZ_UMBRAL_RECHAZO or dist > umbral:
                continue
            conf = "ALTA" if dist <= 0.45 else "MEDIA"
            matches.append((etq, nombre, voz_id, float(dist), conf, len(ss)))
        if not matches:
            sin_match += 1
            if nearest:
                cd, cn, _cet = sorted(nearest)[0]
                if cn and cd <= VOZ_UMBRAL_CANDIDATO:
                    with engine.begin() as conn:
                        ya = conn.execute(text(
                            "SELECT COUNT(*) FROM dbo.R_Conversacion_Voz "
                            "WHERE ConversacionId = :c AND EsPrincipal = :pr "
                            "AND VozId IS NOT NULL"), {"c": cid, "pr": 1}).scalar()
                        if not ya:
                            conn.execute(text(
                                "UPDATE dbo.A_Conversacion SET CandidatoTecnico = :n, "
                                "CandidatoDist = :d WHERE Id = :c"),
                                {"n": cn, "d": cd, "c": cid})
            continue
        ok += 1
        nombradas += 1
        matches.sort(key=lambda m: -m[5])
        princ = matches[0]
        with engine.begin() as conn:
            hay_nombrado = conn.execute(text(
                "SELECT COUNT(*) FROM dbo.R_Conversacion_Voz "
                "WHERE ConversacionId = :c AND EsPrincipal = :pr AND VozId IS NOT NULL"),
                {"c": cid, "pr": 1}).scalar()
            if not hay_nombrado:
                conn.execute(text(
                    "UPDATE dbo.R_Conversacion_Voz SET EsPrincipal = :pr "
                    "WHERE ConversacionId = :c"), {"pr": 0, "c": cid})
            for etq, nombre, voz_id, dist, conf, _n in matches:
                conn.execute(text(
                    "UPDATE dbo.R_Conversacion_Voz SET VozId = :v, Rol = :r, "
                    "EsPrincipal = :pr, Confianza = :cf, Distancia = :d "
                    "WHERE ConversacionId = :c AND EtiquetaLocal = :g"),
                    {"v": voz_id, "r": "TECNICO",
                     "pr": 1 if (not hay_nombrado and etq == princ[0]) else 0,
                     "cf": conf, "d": dist, "c": cid, "g": etq})
                conn.execute(text(
                    "UPDATE dbo.A_ConversacionTurno SET VozId = :v, Rol = :r "
                    "WHERE ConversacionId = :c AND EtiquetaLocal = :g"),
                    {"v": voz_id, "r": "TECNICO", "c": cid, "g": etq})
            if canal_etq.get(princ[0]):
                conn.execute(text(
                    "UPDATE dbo.A_Conversacion SET CanalTecnico = :cn, "
                    "CandidatoTecnico = NULL, CandidatoDist = NULL WHERE Id = :c"),
                    {"cn": canal_etq.get(princ[0]), "c": cid})
        print(f"  ok cdr{cdr} tec={princ[1]} {princ[4]} {princ[3]:.3f} "
              f"(g{princ[0]}/{ (canal_etq.get(princ[0]) or '-') })")
    print(f"\nreprocesadas: {ok} con tecnico | sin match: {sin_match} | "
          f"errores: {len(errores)}")
    for c, e in errores[:10]:
        print(f"  err cdr{c}: {e}")
    return {"ok": ok, "nombradas": nombradas, "sin_match": sin_match,
            "errores": errores}
