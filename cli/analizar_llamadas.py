"""Resumen + categoria de llamadas con Ollama local (dry-run por defecto).

Uso:
  py -u -m cli.analizar_llamadas                      # dry-run, ultimas 20
  py -u -m cli.analizar_llamadas --limite 25
  py -u -m cli.analizar_llamadas --aplicar
  py -u -m cli.analizar_llamadas --categorias "Facturacion,Certificados/AFIP,Soporte remoto,Estanque,Administracion,Otro"
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time

from etl.dominios.analisis.analizar import main


def _csv(valor: str) -> list[str]:
    return [x.strip() for x in valor.split(",") if x.strip()]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Resumen/categoria de llamadas (Ollama, dry-run).")
    ap.add_argument("--desde", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--hasta", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--limite", type=int, default=20, help="max conversaciones (default 20)")
    ap.add_argument("--aplicar", action="store_true", help="persistir en A_ConversacionAnalisis")
    ap.add_argument("--modelo", default=None, help="modelo Ollama (default settings)")
    ap.add_argument("--categorias", type=_csv, default=None,
                    help="lista fija de categorias (coma-separadas); omitir = libre")
    args = ap.parse_args()

    desde = datetime.combine(args.desde, time.min) if args.desde else None
    hasta = datetime.combine(args.hasta, time.max) if args.hasta else None
    main(desde=desde, hasta=hasta, limite=args.limite, aplicar=args.aplicar,
         modelo=args.modelo, categorias=args.categorias)
