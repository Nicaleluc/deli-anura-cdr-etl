"""Identidad de nombres: decide si dos menciones son la misma persona.

Reglas (acordadas):
  mismo     -> tokens iguales (normalizado)
  completa  -> uno contenido en el otro ("Juan" vs "Juan Perez")
  ambiguo   -> comparten algun token pero ninguno contiene al otro
               ("Juan Perez" vs "Juan Gomez") -> revisar, no unir
  distinto  -> sin tokens en comun
"""
from __future__ import annotations

import re

from etl.dominios.contactos.normalizar import sin_acentos


def tokens(nombre: str) -> frozenset[str]:
    s = sin_acentos((nombre or "").lower())
    return frozenset(t for t in re.findall(r"[a-z0-9]+", s) if t)


def relacion(a: str, b: str) -> str:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return "distinto"
    if ta == tb:
        return "mismo"
    if ta <= tb or tb <= ta:
        return "completa"
    if ta & tb:
        return "ambiguo"
    return "distinto"


def mas_completo(a: str, b: str) -> str:
    """El de mas tokens (mas informativo)."""
    return a if len(tokens(a)) >= len(tokens(b)) else b
