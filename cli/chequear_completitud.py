"""CLI chequear_completitud: cobertura difusa API vs CSV por dia.

Uso: py -m cli.chequear_completitud [--dias N] [--umbral 0.98] [--voz]
Sale con codigo 1 si algun dia esta debajo del umbral (apto para alerta).
El match es difuso (±3 s, mismo origen+direccion): el join a segundo
exacto pierde ~15% por fuzz de timestamps (medido 2026-10-02: 99.6% con ±3s).

Con --voz agrega por dia: candidatas grabables (IN MULTIDIAL/DIAL + OUT sin
hermana, igual que conversaciones_grabadas) vs CdrIds en A_Conversacion:
lo pendiente del batch de voz.
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict

from etl.conn.anura_client import AnuraClient
from etl.conn.db import get_engine
from sqlalchemy import text

TOL_S = 3


def cobertura(dia, client, engine, token):
    api = client.gcapi("AdminService.getCdrs", [dia, dia, 1], token) or []
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT FechaDT, Nro_Origen, Direccion FROM dbo.A_Llamada "
            "WHERE FechaDT >= :a AND FechaDT < DATEADD(day,1,:a)"),
            {"a": dia}).fetchall()
    por_clave = defaultdict(list)
    for f, o, d in rows:
        por_clave[(str(o or ""), d)].append(f)
    match = 0
    for x in api:
        t = dt.datetime.fromtimestamp(x["dialTime"]["time"] / 1000)
        cands = por_clave.get((str(x.get("calling") or ""), x.get("direction")), [])
        if cands and min(abs((f - t).total_seconds()) for f in cands) <= TOL_S:
            match += 1
    return len(api), len(rows), (match / len(api) if api else 1.0)


def main():
    ap = argparse.ArgumentParser(description="Cobertura API vs CSV por dia")
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--umbral", type=float, default=0.98)
    ap.add_argument("--hoy", action="store_true",
                    help="incluye el dia corriente (suele estar debajo del "
                         "umbral por lag de la tabla de stats: no es alertable)")
    ap.add_argument("--voz", action="store_true",
                    help="agrega faltantes de voz por dia (grabables vs "
                         "A_Conversacion)")
    a = ap.parse_args()
    client, engine = AnuraClient(), get_engine()
    token = client.get_access_token()
    hoy = dt.date.today()
    # Sin --hoy: ayer hacia atras (el dia corriente va con lag y no es
    # alertable; lo cubre el re-scan de 7 dias del batch de voz).
    ks = range(a.dias - 1, -1, -1) if a.hoy else range(a.dias, 0, -1)
    ok = True
    print(f"{'dia':12s} {'API':>6s} {'CSV':>6s} {'cobertura':>10s}")
    for k in ks:
        d = (hoy - dt.timedelta(days=k)).isoformat()
        try:
            napi, ncsv, cob = cobertura(d, client, engine, token)
        except Exception as ex:
            print(f"{d} ERROR {type(ex).__name__}: {str(ex)[:100]}")
            ok = False
            continue
        marca = "" if cob >= a.umbral else "  <-- BAJO UMBRAL"
        print(f"{d} {napi:6d} {ncsv:6d} {cob:9.1%}{marca}")
        ok = ok and cob >= a.umbral
        if a.voz:
            from etl.dominios.voz.audio import conversaciones_grabadas

            cand = conversaciones_grabadas(client, d, token)
            ids = [x["cdrId"] for x in cand]
            with engine.connect() as conn:
                hechas = {r[0] for r in conn.execute(
                    text("SELECT CdrId FROM dbo.A_Conversacion WHERE Fecha >= :a "
                         "AND Fecha < DATEADD(day,1,:a)"), {"a": d}).fetchall()} \
                    if ids else set()
            faltan = [i for i in ids if i not in hechas]
            print(f"             voz: {len(ids)} grabables, {len(hechas)} en BD, "
                  f"faltan {len(faltan)}" +
                  (f" (ej. {faltan[:5]})" if faltan else ""))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
