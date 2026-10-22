"""Resolucion razon social -> CUIT contra `dbo.D_PadronARCA` (6,17M filas).

Estrategia (sin inventar): match exacto normalizado (sin puntos, mayusculas,
espacios colapsados). Si no hay exacto, candidatos por prefijo del primer token;
si hay exactamente 1 -> se usa; si hay >1 -> ambiguo (None + cantidad).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import text

from etl.dominios.contactos.normalizar import sin_acentos

_UMBRAL_SIM = 0.90
_MARGEN_SIM = 0.03


def _norm(s: str) -> str:
    s = sin_acentos((s or "").upper()).replace(".", "").replace(",", "")
    return re.sub(r"\s+", " ", s).strip()


def _compact(s: str) -> str:
    """Sin espacios: 'EMFACO S A' == 'EMFACO SA' == 'EMFACOSA'."""
    return _norm(s).replace(" ", "")


def _clave(razon: str) -> str | None:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", sin_acentos(razon or "")) if len(t) >= 3]
    return toks[0] if toks else None


def resolver_cuit(conn, razon: str, umbral_sim: float = _UMBRAL_SIM) -> tuple[str | None, int]:
    """-> (cuit | None, candidatos).

    1. match exacto (compacto, sin espacios): 'EMFACO SA' == 'EMFACO S A'.
    2. prefijo compacto unico.
    3. similitud >= umbral con margen claro (cubre variantes tipo
       'Rabaiotti S.r.l' vs ARCA 'RABAIOTT').
    >1 candidato fuerte o dudoso -> (None, n) para revision.
    """
    if not _norm(razon):
        return (None, 0)
    objetivo = _compact(razon)
    clave = _clave(razon)
    if not clave or len(objetivo) < 3:
        return (None, 0)
    filas = conn.execute(
        text("SELECT TOP 5000 RazonSocial, Cuit FROM dbo.D_PadronARCA WHERE RazonSocial LIKE :p"),
        {"p": clave + "%"},
    ).fetchall()

    exactos = [c for rs, c in filas if _compact(rs) == objetivo]
    if len(exactos) == 1:
        return (exactos[0], 1)

    prefijos = [c for rs, c in filas if _compact(rs).startswith(objetivo)]
    if len(prefijos) == 1 and not exactos:
        return (prefijos[0], 1)

    puntajes = sorted(
        ((SequenceMatcher(None, objetivo, _compact(rs)).ratio(), c) for rs, c in filas),
        reverse=True,
    )
    fuertes = [c for s, c in puntajes if s >= umbral_sim]
    if len(fuertes) == 1:
        mejor = puntajes[0][0]
        segundo = puntajes[1][0] if len(puntajes) > 1 else 0.0
        if mejor - segundo >= _MARGEN_SIM:
            return (fuertes[0], 1)
    return (None, max(len(fuertes), len(prefijos)))
