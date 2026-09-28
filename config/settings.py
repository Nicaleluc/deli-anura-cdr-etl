"""Configuración de deli-anura-cdr-etl (slice de `deli/config/settings.py`).

Solo lo que usa este repo: credenciales Anura (`CLIENT_ID/PASSWORD`) y
SQL Server (`SQL_*`, vía `etl/conn/db.py`). Credenciales en `.env`
(raíz de este repo); acá solo se leen y tipan. Parser propio.
"""
import os
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = _BASE_DIR / ".env"


def _cargar_env() -> None:
    if not _ENV_FILE.exists():
        return
    for linea in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave = clave.strip()
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        os.environ.setdefault(clave, valor)


_cargar_env()


def _get(clave: str, default: str = "") -> str:
    return os.getenv(clave, default)


# ANURA (OAuth Keycloak → GCAPI; ver etl/conn/anura_client.py)
CLIENT_ID = _get("CLIENT_ID")
CLIENT_PASSWORD = _get("CLIENT_PASSWORD")

# SQL SERVER (tabla A_Llamada; ver etl/conn/db.py)
SQL_SERVER = _get("SQL_SERVER")
SQL_DATABASE = _get("SQL_DATABASE")
SQL_USERNAME = _get("SQL_USERNAME")
SQL_PASSWORD = _get("SQL_PASSWORD")

# Fail-fast (pack 1): mensaje claro si falta config, en vez de error
# criptico mas tarde (401 de Anura / fallo de conexion SQL).
_requeridas = {
    "CLIENT_ID": CLIENT_ID,
    "CLIENT_PASSWORD": CLIENT_PASSWORD,
    "SQL_SERVER": SQL_SERVER,
    "SQL_DATABASE": SQL_DATABASE,
    "SQL_USERNAME": SQL_USERNAME,
    "SQL_PASSWORD": SQL_PASSWORD,
}
_faltantes = sorted(k for k, v in _requeridas.items() if not v)
if _faltantes:
    raise RuntimeError(
        "Faltan variables en .env (copiar .env.template a .env y completar): "
        + ", ".join(_faltantes)
    )
