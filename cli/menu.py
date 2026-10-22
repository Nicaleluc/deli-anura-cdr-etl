"""
Menu interactivo del pipeline de voz (deli-anura-cdr-etl).

Se usa desde `menu.bat`. Cada opcion es una tarea completa y re-ejecutable
(idempotente): correrla de nuevo no duplica ni rompe nada.

  1) Estado general          - que hay cargado, por dia, MB en disco, que falta
  2) Procesar rango          - descargar + transcribir + identificar voces
  3) Enrolar candidatos      - agrupar hablantes sueltos en voces (tecnico/cliente)
  4) Fusionar duplicados     - candidatos que en realidad son el cliente
  5) Nombrar una voz         - confirmar el nombre de un tecnico (por escucha)
  6) Top candidatos          - tecnicos sin nombre, ordenados por alcance
  7) Exportar clips          - recortar los turnos de una voz a .wav para escuchar
  8) Limpiar espacio         - borrar .ogg ya procesados (libera disco)

No consume tokens: todo corre local (whisper + ECAPA) o contra la API de
Anura (OAuth, sin costo por token).
"""

import os
import sys
from datetime import date, timedelta

from sqlalchemy import text

from config.settings import VOZ_AUDIO_ROOT, VOZ_MODELO_VERSION
from etl.conn.db import get_engine

PACK_AUDIOS = VOZ_AUDIO_ROOT
PACK = os.path.dirname(PACK_AUDIOS)
SALIDA_CLIPS = os.path.join(PACK, "escuchar")

LINEA = "=" * 66


def _h(t=""):
    print(t)


def _titulo(t):
    print()
    print(LINEA)
    print(f" {t}")
    print(LINEA)


def _pedir(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def _fecha(prompt, default=None):
    while True:
        v = _pedir(f"{prompt} (AAAA-MM-DD): ", default or "")
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            print("  fecha invalida, ej 2026-09-26")


# ---------------------------------------------------------------- 1) estado
def op_estado(_engine):
    _titulo("1) Estado general")
    with _engine.connect() as c:
        total = c.execute(text("SELECT COUNT(*) FROM dbo.A_Conversacion")).scalar()
        voces = c.execute(text("SELECT COUNT(*) FROM dbo.A_Voz")).scalar()
        pend = c.execute(text(
            "SELECT COUNT(*) FROM dbo.R_Conversacion_Voz "
            "WHERE VozId IS NULL AND EtiquetaLocal<>:x"), {"x": "X"}).scalar()
        ultimo = c.execute(text("SELECT MAX(Fecha) FROM dbo.A_Conversacion")).scalar()
        _h(f"  conversaciones en base : {total}")
        _h(f"  voces en A_Voz         : {voces}")
        _h(f"  grupos sin identificar  : {pend}")
        _h(f"  ultima llamada         : {ultimo}")
        _h("\n  por dia:")
        for f, n in c.execute(text(
                "SELECT CAST(Fecha AS DATE) d, COUNT(*) FROM dbo.A_Conversacion "
                "GROUP BY CAST(Fecha AS DATE) ORDER BY d")):
            _h(f"    {f}  {n:>4} llamadas")
        _h("\n  candidatos tecnico sin nombre (top por alcance):")
        for vid, veces, tels in c.execute(text("""
                SELECT TOP 5 v.Id, v.Veces, COUNT(DISTINCT l.Nro_Origen)
                FROM dbo.A_Voz v
                JOIN dbo.R_Conversacion_Voz r ON r.VozId=v.Id
                JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId
                LEFT JOIN dbo.A_Llamada l ON l.Id=co.A_Llamada_Id
                WHERE v.EsTecnico=:et AND v.Activo=:a AND v.Nombre=:n
                GROUP BY v.Id, v.Veces
                ORDER BY COUNT(DISTINCT l.Nro_Origen) DESC"""), {"et": 1, "a": 1, "n": "Sin identificar"}):
            _h(f"    voz {vid:<4} {veces:>3} llamadas, {tels:>3} telefonos  -> opcion 7 para clips")

    if os.path.isdir(PACK_AUDIOS):
        mb = n = 0
        for d in os.listdir(PACK_AUDIOS):
            p = os.path.join(PACK_AUDIOS, d)
            if os.path.isdir(p):
                for f in os.listdir(p):
                    if f.endswith(".ogg"):
                        n += 1
                        mb += os.path.getsize(os.path.join(p, f)) / 1e6
        _h(f"\n  audios en disco        : {n} archivos, {mb:.0f} MB")
        _h("  (opcion 8 libera los ya procesados)")
    return _pedir("\nEnter para volver al menu... ")


# ------------------------------------------------------------- 2) procesar
def _fmt_progreso(etapa, detalle, ctx):
    i, n = ctx["i"], ctx["n"]
    pct = int(i * 100 / n) if n else 0
    barra = "#" * (pct // 4)
    print(f"  [{i:>3}/{n:<3} {pct:>3}%] {barra:<25} cdr{ctx['cdrId']} "
          f"{etapa} {detalle}", flush=True)


def op_procesar(engine):
    _titulo("2) Procesar rango (descargar + transcribir + voces)")
    _h("  (sin costo de tokens; ~2-3 min por llamada en CPU)")
    _h("  1 = solo lo que falta (saltea lo ya cargado)")
    _h("  2 = reprocesar lo ya cargado (re-atribuye con las voces actuales,")
    _h("      ej. enrolaste un tecnico y queres que tome sus llamadas viejas)")
    modo = (_pedir("  modo [1/2, default 1] ") or "1").strip()
    d = _fecha("  desde", (date.today() - timedelta(days=1)).isoformat())
    h = _fecha("  hasta (inclusive)", date.today().isoformat())
    if not d or not h:
        return _pedir("\nCancelado. Enter para volver... ")
    if d > h:
        d, h = h, d
    _h(f"\n  Rango: {d} .. {h}  (Ctrl+C para cortar; se puede reanudar)")
    _lim = (_pedir("  limite (Enter=50, 0=sin limite) ") or "50").strip()
    try:
        _lim_n = int(_lim)
    except ValueError:
        _lim_n = 50
    limite = 50 if _lim_n == 50 else (None if _lim_n == 0 else _lim_n)
    if limite is None:
        _h("  sin limite: procesa todo el rango (puede tardar horas)")
    try:
        if modo == "2":
            from etl.dominios.voz.conversaciones import reprocesar_rango
            reprocesar_rango(desde=d, hasta=h, limite=limite,
                             progreso=_fmt_progreso)
        else:
            from etl.dominios.voz.conversaciones import main as proc_main
            proc_main(desde=d, hasta=h, limite=limite, progreso=_fmt_progreso)
    except KeyboardInterrupt:
        _h("\n  Interrumpido. Lo hecho queda guardado (se puede reanudar).")
    return _pedir("\nEnter para volver al menu... ")


# ------------------------------------------------------------- 3) enrolar
def op_enrolar(engine):
    _titulo("3) Enrolar candidatos (hablantes -> voces)")
    d = _fecha("  desde", (date.today() - timedelta(days=7)).isoformat())
    h = _fecha("  hasta (inclusive)", date.today().isoformat())
    if not d or not h:
        return _pedir("\nCancelado. Enter para volver... ")
    if d > h:
        d, h = h, d
    from etl.dominios.voz import enrolar
    hx = h + timedelta(days=1)  # enrolar usa 'hasta' exclusivo
    try:
        enrolar.main(desde=d, hasta=hx, aplicar=True, con_nombres=False)
    except KeyboardInterrupt:
        _h("\n  Interrumpido.")
    return _pedir("\nEnter para volver al menu... ")


# ------------------------------------------------------------- 4) fusionar
def op_fusionar(engine):
    _titulo("4) Fusionar duplicados (candidato-tecnico que era el cliente)")
    _h("  Solo candidatos de UN solo telefono cuya voz coincide con la del")
    _h("  cliente de ese telefono (distancia <= 0,45). No toca tecnicos reales.")
    from etl.dominios.voz import enrolar
    try:
        enrolar.fusionar_duplicados_cliente(engine, VOZ_MODELO_VERSION, aplicar=True)
    except KeyboardInterrupt:
        _h("\n  Interrumpido.")
    return _pedir("\nEnter para volver al menu... ")


# -------------------------------------------------------------- 5) nombrar
def op_nombrar(engine):
    _titulo("5) Nombrar una voz (confirmar tecnico por escucha)")
    v = _pedir("  Id de A_Voz (ver opcion 6): ")
    if not v.isdigit():
        print("  Id invalido.")
        return _pedir("\nEnter para volver... ")
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT Nombre, EsTecnico, Estado, Veces FROM dbo.A_Voz WHERE Id=:i"),
            {"i": int(v)}).fetchone()
    if not row:
        print("  No existe ese Id.")
        return _pedir("\nEnter para volver... ")
    _h(f"  Actual: {tuple(row)}")
    n = _pedir("  Nombre real ( vacio = cancelar): ")
    if not n:
        return _pedir("\nCancelado. Enter para volver... ")
    from etl.dominios.voz.enrolar import _consolidar
    with engine.begin() as c:
        c.execute(text("UPDATE dbo.A_Voz SET Nombre=:n, "
                       "FechaActualizacion=SYSUTCDATETIME() WHERE Id=:i"),
                  {"n": n.strip()[:120], "i": int(v)})
    _consolidar(engine, VOZ_MODELO_VERSION)
    with engine.connect() as c:
        print("  ->", tuple(c.execute(text(
            "SELECT Id, Nombre, EsTecnico, Estado, Veces FROM dbo.A_Voz "
            "WHERE Id=:i"), {"i": int(v)}).fetchone()))
    return _pedir("\nEnter para volver al menu... ")


# --------------------------------------------------------------- 6) top
def op_top(engine):
    _titulo("6) Candidatos tecnico sin nombre (top por alcance)")
    with engine.connect() as c:
        rows = c.execute(text("""
                SELECT v.Id, v.Veces, COUNT(DISTINCT l.Nro_Origen) tels
                FROM dbo.A_Voz v
                JOIN dbo.R_Conversacion_Voz r ON r.VozId=v.Id
                JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId
                LEFT JOIN dbo.A_Llamada l ON l.Id=co.A_Llamada_Id
                WHERE v.EsTecnico=:et AND v.Activo=:a AND v.Nombre=:n
                GROUP BY v.Id, v.Veces
                ORDER BY tels DESC, v.Veces DESC"""), {"et": 1, "a": 1, "n": "Sin identificar"}).fetchall()
    _h(f"  {'voz':<6}{'llamadas':>10}{'telefonos':>12}")
    for vid, veces, tels in rows[:25]:
        _h(f"  {vid:<6}{veces:>10}{tels:>12}")
    _h(f"\n  {len(rows)} candidatos sin nombre. Usá opcion 7 para ouvir.")
    return _pedir("\nEnter para volver al menu... ")


# ------------------------------------------------------------ 7) exportar
def op_exportar(engine):
    _titulo("7) Exportar clips de una voz (para escuchar)")
    v = _pedir("  Id de A_Voz: ")
    if not v.isdigit():
        print("  Id invalido.")
        return _pedir("\nEnter para volver... ")
    import soundfile as sf
    from etl.dominios.voz import modelos as M
    vid = int(v)
    with engine.connect() as c:
        ap = c.execute(text(
            "SELECT r.ConversacionId, r.EtiquetaLocal, co.RutaAudio "
            "FROM dbo.R_Conversacion_Voz r "
            "JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId "
            "WHERE r.VozId=:v"), {"v": vid}).fetchall()
        segs = []
        for conv, et, ruta in ap:
            for nro, ini, fin, txt in c.execute(text(
                    "SELECT Nro, InicioSeg, FinSeg, Texto FROM dbo.A_ConversacionTurno "
                    "WHERE ConversacionId=:c AND EtiquetaLocal=:e AND VozId=:v"),
                    {"c": conv, "e": et, "v": vid}).fetchall():
                if fin - ini >= 2.0:
                    segs.append((fin - ini, conv, ruta, nro, ini, fin, txt))
    if not segs:
        print("  Sin turnos suficientes para esta voz.")
        return _pedir("\nEnter para volver... ")
    segs.sort(reverse=True)
    picked, used = [], set()
    for s in segs:
        if s[1] not in used or len(picked) < 8:
            picked.append(s)
            used.add(s[1])
        if len(picked) >= 8:
            break
    folder = os.path.join(SALIDA_CLIPS, f"voz{vid}")
    os.makedirs(folder, exist_ok=True)
    cache = {}
    idx = [f"# Escuchar voz {vid}", "",
           f"Si reconoces el nombre: opcion 5 con Id {vid}", ""]
    for dur, conv, ruta, nro, ini, fin, txt in picked:
        if ruta not in cache:
            p = os.path.join(PACK_AUDIOS, ruta.replace("/", os.sep))
            cache[ruta] = M.decode_audio(p) if os.path.exists(p) else None
        audio = cache[ruta]
        if audio is None:
            continue
        a = audio[int(ini * M.SR):int(fin * M.SR)]
        if a.size < M.SR:
            continue
        name = f"voz{vid}_conv{conv}_{nro}_{ini:.0f}s.wav"
        sf.write(os.path.join(folder, name), a, M.SR, subtype="PCM_16")
        idx.append(f'- `{name}` ({dur:.1f}s) - "{txt[:90]}"')
    with open(os.path.join(folder, "LEEME.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx) + "\n")
    _h(f"  {len(picked)} clips en: {folder}")
    return _pedir("\nEnter para volver al menu... ")


# -------------------------------------------------------------- 8) limpiar
def op_limpiar(engine):
    _titulo("8) Limpiar espacio (borrar audios ya procesados)")
    _h("  Se borran los .ogg cuyo CdrId YA esta en A_Conversacion.")
    _h("  NO se borran los que aun no se procesaron ni los que tienen")
    _h("  grupos sin identificar (esos hacen falta para el paso 3).")
    if not os.path.isdir(PACK_AUDIOS):
        print("  No existe la carpeta de audios.")
        return _pedir("\nEnter para volver... ")
    with engine.connect() as c:
        db = {int(r[0]) for r in c.execute(
            text("SELECT CdrId FROM dbo.A_Conversacion")).fetchall()}
        sin_voz = {int(r[0]) for r in c.execute(text(
            "SELECT DISTINCT co.CdrId FROM dbo.A_Conversacion co "
            "JOIN dbo.R_Conversacion_Voz r ON r.ConversacionId=co.Id "
            "WHERE r.VozId IS NULL AND r.EtiquetaLocal<>:x"), {"x": "X"}).fetchall()}
    borrables, mb = [], 0.0
    for d in os.listdir(PACK_AUDIOS):
        p = os.path.join(PACK_AUDIOS, d)
        if not os.path.isdir(p):
            continue
        for f in os.listdir(p):
            if not f.endswith(".ogg") or not f[3:].split("_")[0].isdigit():
                continue
            cdr = int(f[3:].split("_")[0])
            if cdr in db and cdr not in sin_voz:
                full = os.path.join(p, f)
                borrables.append(full)
                mb += os.path.getsize(full) / 1e6
    _h(f"\n  {len(borrables)} archivos listos para borrar (~{mb:.0f} MB)")
    if not borrables:
        print("  Nada que limpiar.")
        return _pedir("\nEnter para volver... ")
    if _pedir("  Confirmar (escribe SI): ") != "SI":
        print("  Cancelado.")
        return _pedir("\nEnter para volver... ")
    ok = 0
    for full in borrables:
        try:
            os.remove(full)
            ok += 1
        except OSError:
            pass
    _h(f"\n  Borrados {ok} archivos (~{mb:.0f} MB liberados).")
    return _pedir("\nEnter para volver al menu... ")


OPS = [
    ("1", "Estado general", op_estado),
    ("2", "Procesar rango (descargar+transcribir)", op_procesar),
    ("3", "Enrolar candidatos", op_enrolar),
    ("4", "Fusionar duplicados", op_fusionar),
    ("5", "Nombrar una voz", op_nombrar),
    ("6", "Top candidatos sin nombre", op_top),
    ("7", "Exportar clips para escuchar", op_exportar),
    ("8", "Limpiar espacio", op_limpiar),
]


def main():
    engine = get_engine()
    while True:
        _titulo("MENU PIPELINE DE VOZ - deli-anura-cdr-etl")
        for k, t, _ in OPS:
            _h(f"  {k}) {t}")
        _h("  0) Salir")
        op = _pedir("\nElegi una opcion: ")
        if op == "0" or op.lower() in ("q", "salir"):
            print(" Chau.")
            return
        for k, _t, fn in OPS:
            if op == k:
                fn(engine)
                break
        else:
            print(" Opcion invalida.")


if __name__ == "__main__":
    main()
