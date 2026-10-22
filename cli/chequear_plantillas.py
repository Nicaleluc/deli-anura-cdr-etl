"""Higiene de plantillas A_Voz: debiles, colisiones y banda angosta.

    python -m cli.chequear_plantillas [--pack RUTA]

Reporta, para las plantillas de tecnico activas (Clips>0):
  - DEBIL: pocos clips (<5) -> distancia intra inestable, mas falsos.
  - COLISION: otra plantilla a <0.45 -> se pueden confundir (revisar).
  - BANDA ANGOSTA: el voiceprints.json la marca 8 kHz -> las llamadas 16 kHz
    corren altas y suelen quedar rechazadas (reconstruir con wideband).
  - Sin nota de origen en el pack (dificil de auditar).

No escribe nada; solo diagnostica. Exit 1 si hay colisiones.
"""
import argparse
import json
import sys

import numpy as np

from config.settings import VOZ_VOICEPRINTS_JSON
from etl.conn.db import get_engine
from sqlalchemy import text


def _vec(s):
    v = np.fromstring(str(s).strip("[]").replace(",", " "), sep=" ")
    return v / (np.linalg.norm(v) or 1.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pack", default=VOZ_VOICEPRINTS_JSON,
                    help="ruta a voiceprints.json (default: settings)")
    ap.add_argument("--umbral-colision", type=float, default=0.45)
    args = ap.parse_args()

    engine = get_engine()
    with engine.connect() as conn:
        filas = conn.execute(text(
            "SELECT Id, Nombre, Clips, Umbral, CAST(Centroide AS NVARCHAR(MAX)) "
            "FROM dbo.A_Voz WHERE Activo=1 AND EsTecnico=1 AND Clips>0 "
            "ORDER BY Nombre")).fetchall()

    try:
        pack = json.load(open(args.pack, encoding="utf-8"))
    except Exception:
        pack = {}

    plantillas = [(int(i), n, int(k), float(u), _vec(c)) for i, n, k, u, c in filas]
    print(f"plantillas de tecnico activas: {len(plantillas)}\n")

    debiles, angostas, colisiones = [], [], []
    for vid, nombre, clips, umbral, vec in plantillas:
        notas = []
        if clips < 5:
            debiles.append(nombre)
            notas.append("DEBIL")
        info = pack.get(nombre) or {}
        nota = str(info.get("nota", "")) + " " + str(info.get("fuente", ""))
        if "8 kHz" in nota or "8kHz" in nota or "banda-angosta" in nota.lower():
            angostas.append(nombre)
            notas.append("BANDA-ANGOSTA")
        if not info:
            notas.append("SIN-NOTA-PACK")
        cercanos = sorted(((float(1 - vec @ o[4]), o[1]) for o in plantillas
                           if o[0] != vid))
        vecino, dvec = cercanos[0][1], cercanos[0][0]
        if dvec < args.umbral_colision:
            colisiones.append((nombre, vecino, dvec))
            notas.append(f"COLISION con {vecino} {dvec:.3f}")
        if notas:
            print(f"  {nombre:<22} clips={clips:<3} umbral={umbral:.2f}  "
                  + ", ".join(notas))

    print(f"\ndebiles: {len(debiles)} | banda angosta: {len(angostas)} | "
          f"colisiones: {len(colisiones)}")
    for a, b, d in colisiones:
        print(f"  COLISION: {a} ~ {b} ({d:.3f})")
    return 1 if colisiones else 0


if __name__ == "__main__":
    sys.exit(main())
