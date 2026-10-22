"""Extraccion de nombres dichos en la llamada, con el rol de quien los dice.

Salida: [{"rol": "TECNICO"|"CLIENTE", "nombre": str, "confianza": float, "texto": str}]

Estrategia: Ollama local (calidad) con el dialogo etiquetado por rol; reglas
simples como respaldo barato. Solo nombres PROPIOS que la persona dice de si
misma (no de terceros).
"""
from __future__ import annotations

import re

from etl.dominios.analisis.ollama import generar_json

PROMPT = """Del siguiente dialogo de una llamada tecnica, extrae los nombres
PROPIOS que cada persona dice de SI MISMA (no de terceros ni empresas).
Devolve SOLO JSON: {{"menciones": [{{"quien": "TECNICO" o "CLIENTE",
"nombre": "Nombre Apellido", "confianza": 0.0}}]}}
Si nadie dice su nombre, devolve {{"menciones": []}}.

Dialogo:
---
{texto}
---
JSON:"""

_REGLAS = [
    (re.compile(r"\bme llamo\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)?)", re.I), 0.6),
    (re.compile(r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)?)", re.I), 0.6),
    (re.compile(r"\bte habla\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)?)", re.I), 0.7),
    (re.compile(r"\bhabla\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)?)", re.I), 0.5),
]
_NO_NOMBRE = {"un", "una", "el", "la", "de", "del", "con", "por", "para", "que",
              "no", "si", "siempre", "nuevo", "cliente", "tecnico",
              "hola", "buen", "buenas", "gracias", "favor"}


def _texto_dialogo(turnos) -> str:
    lineas = []
    for t in turnos:
        rol = t.get("rol") or t.get("Rol") or "?"
        txt = t.get("texto") or t.get("Texto") or ""
        if txt:
            lineas.append(f"{rol}: {txt}")
    return "\n".join(lineas)[:6000]


def _valido(nombre: str) -> bool:
    partes = (nombre or "").split()
    if not (1 <= len(partes) <= 3):
        return False
    return partes[0].lower() not in _NO_NOMBRE and len(partes[0]) >= 2


def por_reglas(turnos) -> list[dict]:
    out = []
    for t in turnos:
        rol = t.get("rol") or t.get("Rol")
        txt = t.get("texto") or t.get("Texto") or ""
        for rex, conf in _REGLAS:
            m = rex.search(txt)
            if m and _valido(m.group(1)):
                out.append({"rol": rol, "nombre": m.group(1).strip().title(),
                            "confianza": conf, "texto": txt[:200]})
                break
    return out


def extraer(turnos, usar_ollama=True) -> list[dict]:
    # Si Ollama no responde (apagado, timeout), se cae a reglas: un nombre
    # perdido es mejor que un enrolar muerto (incidente: el menu corria con
    # con_nombres=False y la evidencia transcript no crecia nunca).
    if usar_ollama:
        try:
            datos = generar_json(PROMPT.format(texto=_texto_dialogo(turnos)))
        except Exception:
            datos = {}
        menciones = []
        for m in (datos.get("menciones") or []):
            nombre = (m.get("nombre") or "").strip()
            quien = (m.get("quien") or "").strip().upper()
            if nombre and quien in ("TECNICO", "CLIENTE") and _valido(nombre):
                menciones.append({"rol": quien, "nombre": nombre,
                                  "confianza": float(m.get("confianza") or 0.5),
                                  "texto": None})
        if menciones:
            return menciones
    return por_reglas(turnos)
