"""Fragmentos de audio por tecnico para la carpeta `escuchar/`.

Corta el tramo que mas hablo el tecnico (grupo que mas segundos tiene en
A_ConversacionTurno, excluyendo el IVR) para que el operador reconozca la voz
y confirme el nombre.

Uso:
    py -u etl/dominios/voz/fragmentos.py --tecnico "Nombre Apellido"
    py -u etl/dominios/voz/fragmentos.py --todos
    py -u etl/dominios/voz/fragmentos.py --cdr-id 1234567890 --tecnico "Candidato"
"""
from __future__ import annotations

import argparse
import os

import soundfile as sf
from sqlalchemy import text

from config.settings import VOZ_AUDIO_ROOT
from etl.conn.db import get_engine

PACK = os.path.dirname(VOZ_AUDIO_ROOT)
ESCUCHAR = os.path.join(PACK, "escuchar")

# Fragmentos muy cortos no sirven para voice-ID. Se armedirectamente una ventana
# de duracion util (5..12 s) recorriendo turnos consecutivos del mismo grupo.
MIN_SEG = 3.0
VENTANA_MIN = 5.0
VENTANA_MAX = 12.0
MAX_SEG = 14.0
SR = 16000
# Antes de este segundo es IVR/jingle (IVR_SEG de etl/dominios/voz/modelos.py).
IVR_SEG = 15.0
# Dos turnos del mismo grupo son del mismo hablante si el segundo arranca
# antes de que termine el primero (con esta tolerancia de solape/hueco).
CONTIG_TOL = 0.35
# Un clip para voice-ID necesita al menos ~4 s limpios.
MIN_TRAMO = 4.0


def norm_txt(s) -> str:
    """minusculas sin acentos, para comparar nombres."""
    import unicodedata
    return unicodedata.normalize("NFKD", str(s)).encode(
        "ascii", "ignore").decode().lower().strip()


def nombre_carpeta(nombre: str) -> str:
    """'Nombre Apellido' -> 'Nombre_Apellido'."""
    limpio = "".join(ch if ch.isalnum() else "_" for ch in nombre)
    return "_".join(p for p in limpio.split("_") if p)


def nombre_archivo(nombre: str, n: int) -> str:
    """'Nombre Apellido', 1 -> 'Nombre_Apellido_001'."""
    return f"{nombre_carpeta(nombre)}_{n:03d}"


def turnos_por_conversacion(engine, al_ids: list[int]):
    """{A_Llamada_Id: [turnos dict]} con Rol del tecnico y segundos hablados."""
    if not al_ids:
        return {}
    ids = [int(i) for i in al_ids]
    ph = ",".join(f":a{k}" for k in range(len(ids)))
    params = {f"a{k}": v for k, v in enumerate(ids)}
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT co.A_Llamada_Id, t.Nro, t.EtiquetaLocal, t.Rol, t.InicioSeg,
                   t.FinSeg, t.Texto
            FROM dbo.A_Conversacion co
            JOIN dbo.A_ConversacionTurno t ON t.ConversacionId = co.Id
            WHERE co.A_Llamada_Id IN ({ph})
            ORDER BY co.A_Llamada_Id, t.Nro"""), params).fetchall()
    out: dict[int, list] = {}
    for al, nro, eti, rol, ini, fin, txt in rows:
        out.setdefault(al, []).append({
            "nro": nro, "etq": eti, "rol": rol,
            "ini": float(ini), "fin": float(fin),
            "seg": float(fin) - float(ini), "txt": (txt or "").strip(),
        })
    return out


def turnos_candidatos(turnos: list[dict]) -> list[dict]:
    """Turnos candidatos a ser el tecnico.

    OJO: `Rol='OTRO'` NO es sinonimo de tecnico. El pipeline solo marca CLIENTE
    cuando esta seguro; si no, deja todo en OTRO y quedan juntos el tecnico y el
    cliente (verificado el 12-09-2026 en 405980 y 406033, donde el cliente habla
    la mayor parte y sigue etiquetado OTRO).

    Por eso el filtro real es: grupo != X (X = IVR/jingle) y fuera de los primeros
    IVR_SEG segundos. NO se exige `seg >= MIN_SEG`: hay llamadas (405980) donde la
    diarizacion parte todo en trozos de 2 s, pero contiguos y del mismo grupo, asi
    que la ventana se arma juntando varios (ver `elegir_ventana`).
    """
    fuera_ivr = [t for t in turnos
                 if t["etq"] != "X" and t["ini"] >= IVR_SEG]
    return [t for t in fuera_ivr if len(t["txt"]) >= 4]


def tramos_contiguos(turnos: list[dict], etiqueta: str) -> list[dict]:
    """Todos los tramos contiguos (>= MIN_SEG) del grupo, sin otros hablantes.

    Recorre los turnos en orden y cierra un tramo en cada hueco: asi el audio es
    de un solo hablante. Devuelve de mas a menos largo.
    """
    propios = [t for t in turnos if t["etq"] == etiqueta and t["ini"] >= IVR_SEG]
    tramos = []
    if not propios:
        return tramos
    ini, fin = propios[0]["ini"], propios[0]["fin"]
    txt = [propios[0]["txt"]]
    for t in propios[1:]:
        contiguo = (t["ini"] - fin) <= CONTIG_TOL
        if contiguo and (t["fin"] - ini) <= VENTANA_MAX:
            fin, _ = t["fin"], txt.append(t["txt"])
            continue
        if fin - ini >= MIN_SEG:
            tramos.append({"ini": ini, "fin": fin, "seg": fin - ini,
                           "etq": etiqueta, "txt": " ".join(txt)})
        ini, fin, txt = t["ini"], t["fin"], [t["txt"]]
    if fin - ini >= MIN_SEG:
        tramos.append({"ini": ini, "fin": fin, "seg": fin - ini,
                       "etq": etiqueta, "txt": " ".join(txt)})
    tramos.sort(key=lambda x: -x["seg"])
    return tramos


def elegir_ventana(turnos: list[dict], etiqueta: str) -> dict | None:
    """Mejor fragmento del grupo `etiqueta`, SIN cortar a otro hablante.

    Solo se pegan turnos **contiguos en el tiempo**: el siguiente tiene que
    arrancar antes de que termine el anterior (con tolerancia). Filtrar por grupo
    y despues unir a ciegas metia el audio de otro hablante en medio, y por eso
    varios clips salieron con dos hablantes mezclados (verificado
    etiquetando a mano los clips).
    """
    propios = [t for t in turnos if t["etq"] == etiqueta and t["ini"] >= IVR_SEG]
    if not propios:
        return None
    mejor = None
    for i in range(len(propios)):
        ini, fin = propios[i]["ini"], propios[i]["fin"]
        txt = [propios[i]["txt"]]
        j = i
        while j + 1 < len(propios) and (propios[j + 1]["fin"] - ini) <= VENTANA_MAX:
            if propios[j + 1]["ini"] - propios[j]["fin"] > CONTIG_TOL:
                break        # hay otro hablante en medio: no se pega
            j += 1
            fin = propios[j]["fin"]
            txt.append(propios[j]["txt"])
            if (fin - ini) >= VENTANA_MIN:
                break
        seg = fin - ini
        if seg < MIN_SEG:
            continue
        if mejor is None or seg > mejor["seg"]:
            mejor = {"ini": ini, "fin": fin, "seg": seg,
                     "etq": etiqueta, "txt": " ".join(txt)}
    return mejor


def elegir_grupo(turnos: list[dict], voz_por_grupo: dict) -> str | None:
    """El grupo mas probable = tecnico.

    Prioridad:
      1. el grupo al que el voice-ID le asigno una voz TECNICA confirmada;
      2. el grupo OTRO/OTRO que mas segundos hablo.
    """
    cand = turnos_candidatos(turnos)
    if not cand:
        return None
    # 1) voz real de tecnico confirmada
    convoz = [t["etq"] for t in cand
              if voz_por_grupo.get(t["etq"], {}).get("nombre")
              and voz_por_grupo[t["etq"]]["nombre"] != "Sin identificar"]
    if len(set(convoz)) == 1:
        return convoz[0]
    if convoz:
        # mas de un grupo con voz: gana el que mas hablo
        conteo = {e: 0.0 for e in set(convoz)}
        for t in cand:
            if t["etq"] in conteo:
                conteo[t["etq"]] += t["seg"]
        return max(conteo, key=conteo.get)
    # 2) el que mas hablo
    seg = {e: 0.0 for e in {t["etq"] for t in cand}}
    for t in cand:
        seg[t["etq"]] += t["seg"]
    return max(seg, key=seg.get)


def voces_por_grupo(engine, al_ids: list[int]):
    """{A_Llamada_Id: {grupo: {nombre, distancia}}} desde R + A_Voz."""
    if not al_ids:
        return {}
    ids = [int(i) for i in al_ids]
    ph = ",".join(f":a{k}" for k in range(len(ids)))
    params = {f"a{k}": v for k, v in enumerate(ids)}
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT co.A_Llamada_Id, t.EtiquetaLocal, v.Nombre, r.Distancia
            FROM dbo.A_Conversacion co
            JOIN dbo.A_ConversacionTurno t ON t.ConversacionId = co.Id
            JOIN dbo.R_Conversacion_Voz r ON r.ConversacionId = co.Id AND r.VozId = t.VozId
            JOIN dbo.A_Voz v ON v.Id = r.VozId
            WHERE co.A_Llamada_Id IN ({ph})"""), params).fetchall()
    out: dict[int, dict] = {}
    for al, etq, nombre, dist in rows:
        out.setdefault(al, {}).setdefault(etq, {"nombre": nombre, "distancia": dist})
    return out


def recortar(ogg: str, ini: float, fin: float, destino: str):
    data, sr = sf.read(ogg, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    a = max(0, int(ini * sr))
    b = min(len(data), int(fin * sr))
    frag = data[a:b]
    sf.write(destino, frag, sr, subtype="PCM_16")
    return len(frag) / sr


def seleccion(engine, tecnico=None, cdr_ids=None):
    """[(A_Llamada_Id, Fecha)] con audio transcrito, leidas de la BD.

    - `cdr_ids`: llamadas puntuales (sirve para candidatos sin nombre).
    - `tecnico`: solo las conversaciones donde esa voz es el tecnico principal.
    - ninguno de los dos: todas las conversaciones con tecnico identificado.
    """
    if cdr_ids:
        ph = ",".join(f":c{k}" for k in range(len(cdr_ids)))
        sql = ("SELECT co.A_Llamada_Id, co.Fecha FROM dbo.A_Conversacion co "
               f"WHERE co.CdrId IN ({ph}) AND co.A_Llamada_Id IS NOT NULL "
               "ORDER BY co.Fecha")
        params = {f"c{k}": v for k, v in enumerate(cdr_ids)}
    else:
        sql = ("SELECT DISTINCT co.A_Llamada_Id, co.Fecha "
               "FROM dbo.A_Conversacion co "
               "JOIN dbo.R_Conversacion_Voz r ON r.ConversacionId = co.Id "
               " AND r.EsPrincipal = 1 AND r.Rol = 'TECNICO' "
               "JOIN dbo.A_Voz v ON v.Id = r.VozId "
               "WHERE co.A_Llamada_Id IS NOT NULL "
               " AND v.Nombre NOT IN ('desconocido', 'Sin identificar')")
        params = {}
        if tecnico:
            sql += " AND v.Nombre = :n"
            params["n"] = tecnico
        sql += " ORDER BY co.Fecha"
    with engine.connect() as conn:
        return [(int(al), fecha) for al, fecha in
                conn.execute(text(sql), params).fetchall()]


def tecnicos_identificados(engine):
    """Nombres de tecnico con conversaciones cargadas (para --todos)."""
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT DISTINCT v.Nombre FROM dbo.A_Voz v "
            "JOIN dbo.R_Conversacion_Voz r ON r.VozId = v.Id "
            " AND r.EsPrincipal = 1 AND r.Rol = 'TECNICO' "
            "WHERE v.Nombre NOT IN ('desconocido', 'Sin identificar') "
            "ORDER BY v.Nombre")).fetchall()]


def procesar(args, filas, tecnico: str) -> int:
    print(f"\n=== {tecnico}: {len(filas)} llamadas transcritas ===")
    if not filas:
        print("  sin audio transcrito: nada que recortar")
        return 0

    engine = get_engine()
    al_ids = [al for al, _ in filas]
    with engine.connect() as c:
        rutas = {}
        ph = ",".join(f":a{k}" for k in range(len(al_ids)))
        params = {f"a{k}": v for k, v in enumerate(al_ids)}
        for al, cdr, ruta in c.execute(text(f"""
            SELECT co.A_Llamada_Id, co.CdrId, co.RutaAudio
            FROM dbo.A_Conversacion co WHERE co.A_Llamada_Id IN ({ph})"""), params).fetchall():
            rutas[al] = (cdr, ruta)
    turnos = turnos_por_conversacion(engine, al_ids)
    voces = voces_por_grupo(engine, al_ids)

    carpeta = os.path.join(args.salida, nombre_carpeta(tecnico))
    os.makedirs(carpeta, exist_ok=True)
    lineas, n = [], 0
    for al, fecha in filas:
        if al not in rutas or not turnos.get(al):
            continue
        cdr, ruta = rutas[al]
        if not ruta:
            continue
        ogg = os.path.join(PACK, "audios", ruta.replace("/", os.sep))
        if not os.path.exists(ogg):
            print(f"  falta audio: {ogg}")
            continue
        # Se recorta un fragmento por CADA grupo de la diarizacion: la
        # diarizacion reparte al tecnico en varios grupos (y en algunos casos
        # etiqueta como tecnico al cliente), asi que en vez de adivinar se le
        # dan todos los candidatos al humano para que etiquete.
        grupos = sorted({t["etq"] for t in turnos[al]
                         if t["etq"] != "X" and t["ini"] >= IVR_SEG
                         and len(t["txt"]) >= 4})
        if not grupos:
            print(f"  conv {al}: sin turnos fuera del IVR")
            continue
        if not args.por_grupo:
            g = elegir_grupo(turnos[al], voces.get(al, {}))
            grupos = [g] if g else []

        hhmm = fecha.strftime("%H:%M")
        for g in grupos:
            ventanas = (tramos_contiguos(turnos[al], g) if args.todos_los_tramos
                        else [elegir_ventana(turnos[al], g)])
            # solo tramos limpios y de duracion util
            ventanas = [v for v in ventanas if v and v["seg"] >= MIN_TRAMO]
            if args.max_tramos:
                ventanas = ventanas[:args.max_tramos]
            for t in ventanas:
                dest = os.path.join(carpeta,
                                    f"{nombre_archivo(tecnico, n + 1)}.wav")
                seg = recortar(ogg, t["ini"], t["fin"], dest)
                n += 1
                frag = t["txt"][:80]
                info = voces.get(al, {}).get(g)
                marca = ""
                if info:
                    marca = f"  [voz={info['nombre']} d={info['distancia']:.3f}]"
                lineas.append(
                    f"- `{os.path.basename(dest)}` ({seg:.1f}s, {hhmm}, g{g})"
                    f" - \"{frag}\"")
                print(f"  {os.path.basename(dest)}  {seg:5.1f}s  "
                      f"{hhmm} g{g}{marca}")

    leeme = os.path.join(carpeta, "LEEME.md")
    with open(leeme, "w", encoding="utf-8") as f:
        f.write(f"# {tecnico} - fragmentos de audio\n\n")
        f.write(f"{len(filas)} llamadas transcritas con audio. Se recortan todos los "
                "tramos limpios (>=4 s, un solo hablante) de cada grupo de la "
                "diarizacion, porque el sistema reparte al tecnico en varios grupos "
                "y a veces etiqueta como tecnico al cliente.\n\n")
        f.write(f"**Escucha y decime cuales son {tecnico}.** Con los confirmados "
                "corro `--semilla` para atribuir sus llamadas.\n\n")
        f.write("Formato: `archivo` (seg, hora, grupo) - frase.\n\n")
        f.write("## Fragmentos\n\n")
        f.write("\n".join(lineas) + "\n")
    print(f"\n{n} fragmentos -> {carpeta}")
    print(f"LEEME: {leeme}")
    return n


def main():
    ap = argparse.ArgumentParser(description="Fragmentos para identificar voces")
    ap.add_argument("--tecnico", help="nombre exacto de F_Tecnico")
    ap.add_argument("--por-grupo", action="store_true",
                    help="un clip por grupo de la diarizacion (para etiquetar)")
    ap.add_argument("--todos-los-tramos", action="store_true",
                    help="emite cada tramo contiguo, no solo el mas largo")
    ap.add_argument("--max-tramos", type=int, default=0,
                    help="limita cuantos tramos por grupo (0 = todos)")
    ap.add_argument("--todos", action="store_true",
                    help="procesa todos los tecnicos con llamadas transcritas")
    ap.add_argument("--cdr-id", type=int, action="append", default=None,
                    help="CDR puntual (repetible); sirve para candidatos sin nombre")
    ap.add_argument("--salida", default=ESCUCHAR)
    args = ap.parse_args()
    if not args.tecnico and not args.todos and not args.cdr_id:
        ap.error("falta --tecnico, --todos o --cdr-id")

    engine = get_engine()
    if args.cdr_id:
        etiqueta = args.tecnico or "Candidatos"
        procesar(args, seleccion(engine, cdr_ids=args.cdr_id), etiqueta)
    elif args.todos:
        nombres = tecnicos_identificados(engine)
        total = sum(procesar(args, seleccion(engine, tecnico=n), n)
                    for n in nombres)
        print(f"\n{len(nombres)} tecnicos, {total} fragmentos en {args.salida}")
    else:
        procesar(args, seleccion(engine, tecnico=args.tecnico), args.tecnico)


if __name__ == "__main__":
    main()
