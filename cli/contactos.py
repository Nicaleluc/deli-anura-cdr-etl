"""Motor de contactos: A_Llamada.Origen -> evidencia -> D_Contacto.

Uso:
  py -u -m cli.contactos                          # dry-run, ultimos 30 dias
  py -u -m cli.contactos --desde 2026-09-22       # dry-run, desde esa fecha
  py -u -m cli.contactos --aplicar                # escribe
  py -u -m cli.contactos --buscar 1100001111      # diagnostico de un telefono
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time

from etl.dominios.contactos.motor import main


def _fecha(valor: str) -> date:
    return date.fromisoformat(valor)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Contactos desde Origen Anura (dry-run por defecto).")
    ap.add_argument("--desde", type=_fecha, default=None, help="YYYY-MM-DD (default: hoy-30)")
    ap.add_argument("--hasta", type=_fecha, default=None, help="YYYY-MM-DD (default: hoy)")
    ap.add_argument("--limite", type=int, default=None, help="tope de telefonos a procesar")
    ap.add_argument("--aplicar", action="store_true", help="persistir (sin esto es dry-run)")
    ap.add_argument("--buscar", default=None, help="diagnostico de un telefono puntual")
    args = ap.parse_args()

    desde = datetime.combine(args.desde, time.min) if args.desde else None
    hasta = datetime.combine(args.hasta, time.max) if args.hasta else None
    main(desde=desde, hasta=hasta, limite=args.limite, aplicar=args.aplicar, buscar=args.buscar)
