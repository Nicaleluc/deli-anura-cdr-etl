"""Cliente minimo de Ollama (local, sin tokens). Devuelve JSON parseado."""
from __future__ import annotations

import json

import requests

from config.settings import OLLAMA_MODEL, OLLAMA_URL


def generar_json(prompt: str, modelo: str | None = None, timeout: int = 300) -> dict:
    """POST /api/generate con format=json -> dict. Lanza si no responde."""
    modelo = modelo or OLLAMA_MODEL
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": modelo, "prompt": prompt, "stream": False,
              "format": "json", "options": {"temperature": 0.2}},
        timeout=timeout,
    )
    resp.raise_for_status()
    texto = resp.json().get("response", "")
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return {}
