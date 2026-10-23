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

# VOZ (etl/dominios/voz + cli/atribuir_voz). Todas OPCIONALES con default:
# nunca rompen el fail-fast de arriba ni el ETL de CDRs.
# Raiz donde se guardan los .ogg por dia (<root>/YYYY-MM-DD/cdr....ogg).
# Default: carpeta audios/ del pack en el repo hermano deli-support-docs.
# Ese repo puede estar al lado de este (clon standalone) o dos niveles arriba
# (cuando este repo vive dentro de `deli/`): se busca en ambos lugares.
def _buscar_docs(nombre="deli-support-docs"):
    for base in (_BASE_DIR.parent, _BASE_DIR.parent.parent):
        if (base / nombre).is_dir():
            return base / nombre
    return _BASE_DIR.parent / nombre


_REPO_HERMANO = _buscar_docs()
VOZ_AUDIO_ROOT = _get(
    "VOZ_AUDIO_ROOT",
    str(_REPO_HERMANO / "integraciones" / "anura-voice-pack" / "audios"),
)
# voiceprints.json (plantillas iniciales) del pack hermano.
VOZ_VOICEPRINTS_JSON = _get(
    "VOZ_VOICEPRINTS_JSON",
    str(_REPO_HERMANO / "integraciones" / "anura-voice-pack" / "voiceprints.json"),
)
# Roster de técnicos (nombres/umbrales/alias): dato personal, NO se versiona.
# Copiar config/roster.example.json a config/roster.json (ver modulos voz.roster).
VOZ_ROSTER_JSON = _get(
    "VOZ_ROSTER_JSON",
    str(_BASE_DIR / "config" / "roster.json"),
)
VOZ_WHISPER_MODEL = _get("VOZ_WHISPER_MODEL", "small")
VOZ_WHISPER_BEAM = int(_get("VOZ_WHISPER_BEAM", "5") or 5)
VOZ_MODELO_VERSION = _get("VOZ_MODELO_VERSION", "ecapa-voxceleb-v1")
# Banda de rechazo del voice-ID: si el tecnico mas cercano esta a distancia
# > este valor, NO se le atribuye (queda 'Sin identificar'). Sin esto, una
# llamada de un tecnico sin plantilla se le asigna al mas parecido y se
# inventa un nombre. Medido: aciertos <=0.35, falsos 0.42-0.57 (19-09).
VOZ_UMBRAL_RECHAZO = float(_get("VOZ_UMBRAL_RECHAZO", "0.40") or 0.40)
# Cola de revision por proximidad: si el tecnico sin cerrar mas cercano esta
# a <= este valor, se guarda como CANDIDATO (V_A_ColaCandidatos) para que un
# humano lo escuche, en vez de tirarlo a 'desconocido' a ciegas. No atribuye.
VOZ_UMBRAL_CANDIDATO = float(_get("VOZ_UMBRAL_CANDIDATO", "0.60") or 0.60)
VOZ_DIAS_ATRAS = int(_get("VOZ_DIAS_ATRAS", "7") or 7)
VOZ_LIMITE = int(_get("VOZ_LIMITE", "50") or 50)

# ANALISIS de llamadas (resumen + categoria) via Ollama local. OPCIONALES.
OLLAMA_URL = _get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
