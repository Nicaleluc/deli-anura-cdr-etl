"""Roster de técnicos y umbrales de voice-ID (dato local, fuera del repo).

El roster real (nombres de personas y umbrales calibrados) es dato personal y
**no se versiona**. Se carga de `config/roster.json` (ignorado por git) o del
archivo apuntado por la variable `VOZ_ROSTER_JSON`.

Formato (copiar `config/roster.example.json` a `config/roster.json`):

    {
      "umbrales": {"Nombre Apellido": 0.55},
      "excluidos": ["Nombre que atiende pero no es técnico"]
    }

  - `umbrales`: nombre completo -> umbral de match (default 0.55).
  - `excluidos`: nombres del roster que no compiten como técnico.

Sin archivo el roster queda vacío: el pipeline funciona igual, pero
`es_tecnico_roster()` no bloquea bautismos y el seed usa 0.55 para todos.
"""
from __future__ import annotations

import json
from pathlib import Path

from config.settings import VOZ_ROSTER_JSON

VACIO = {"umbrales": {}, "excluidos": []}


def cargar_roster(path=None) -> dict:
    """Lee el roster y lo normaliza; vacío si el archivo no existe."""
    ruta = Path(path or VOZ_ROSTER_JSON)
    if not ruta.exists():
        return {"umbrales": {}, "excluidos": []}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"roster ilegible ({ruta}): {exc}") from exc
    return {
        "umbrales": {str(k): float(v)
                     for k, v in (datos.get("umbrales") or {}).items()},
        "excluidos": [str(x) for x in (datos.get("excluidos") or [])],
    }
