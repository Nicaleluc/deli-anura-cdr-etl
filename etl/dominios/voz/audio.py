"""
Descarga de grabaciones Anura -> disco (por dia).

Fuente: AdminService.getCdrs (id, wasRecorded, calling, dialTime) +
AdminService.getRecordingURL (URLs firmadas ogg/mpeg/wav, vencen en 30 min:
descargar en el acto). Solo conversaciones reales: IN + MULTIDIAL + grabada.

Excepciones medidas (2026-10-02, un CDR medido; causa real hallada 2026-10-05):
- Muchas piernas NO salen en la pagina 1 de getCdrs (el 3er arg es NUMERO
  DE PAGINA, no flag: p1 trae ~80 %, el resto esta en p2+). `get_cdrs_day`
  ya pagina hasta agotar ids nuevos. Si aun asi falta la hermana IN, se
  toma la OUT grabada (misma conversacion, canales espejados: ver
  docs/canales-estereo.md). El guard anti-duplicados de procesar_llamada
  evita doble fila si la IN aparece despues (o viceversa).
- Piernas IN con lastAction DIAL tambien son conversacion real grabada
  (ej. un ID numérico de 10 dígitos); se aceptan igual que MULTIDIAL.

Layout: <VOZ_AUDIO_ROOT>/YYYY-MM-DD/cdr<id>_<telefono>_<HHMMSS>.ogg
"""

import os
import time
from datetime import datetime

import requests

OGG_MIME = "audio/ogg"

# lastAction de pierna IN que es conversacion real (no IVR/jingle).
ULTIMAS_IN = ("MULTIDIAL", "DIAL")
# Tolerancia para considerar dos piernas hermanas (CSV vs API difieren).
TOL_HERMANA_S = 15


def hermana_in_presente(out, cdrs):
    """True si en la lista del dia hay una pierna IN de la misma llamada.

    Pura (testeable sin API): mismo llamante y duracion parecida.
    """
    for c in cdrs or []:
        if c.get("direction") != "IN":
            continue
        if str(c.get("calling") or "").strip() != str(out.get("calling") or "").strip():
            continue
        if abs(int(c.get("duration") or 0) - int(out.get("duration") or 0)) <= TOL_HERMANA_S:
            return True
    return False


def conversaciones_grabadas(client, dia_text, access_token=None):
    """CDRs del dia que son conversacion real con audio."""
    cdrs = client.get_cdrs_day(dia_text, access_token)
    out = []
    for c in cdrs or []:
        if not c.get("wasRecorded"):
            continue
        if c.get("direction") != "IN":
            continue
        if c.get("lastAction") not in ULTIMAS_IN:
            continue
        dial = c.get("dialTime") or {}
        ts = dial.get("time")
        if not ts:
            continue
        out.append({
            "cdrId": c["id"],
            "calling": str(c.get("calling") or "").strip(),
            "callingName": c.get("callingName"),
            "duration": int(c.get("duration") or 0),
            "fecha": datetime.fromtimestamp(ts / 1000.0),
            "via": "IN",
        })
    # Fallback: OUT grabada sin hermana IN visible (IN oculta en la API).
    for c in cdrs or []:
        if not c.get("wasRecorded"):
            continue
        if c.get("direction") != "OUT":
            continue
        if hermana_in_presente(c, cdrs):
            continue
        dial = c.get("dialTime") or {}
        ts = dial.get("time")
        if not ts:
            continue
        out.append({
            "cdrId": c["id"],
            "calling": str(c.get("calling") or "").strip(),
            "callingName": c.get("callingName"),
            "duration": int(c.get("duration") or 0),
            "fecha": datetime.fromtimestamp(ts / 1000.0),
            "via": "OUT",
        })
    # mas recientes primero (getCdrs ya viene desc)
    return out


def ruta_audio(root, dia_text, cdr_id, telefono, fecha):
    nombre = f"cdr{cdr_id}_{telefono}_{fecha.strftime('%H%M%S')}.ogg"
    return os.path.join(root, dia_text, nombre)


def descargar_ogg(url, destino, timeout=180, reintentos=2):
    """Baja el .ogg a disco. Devuelve bytes o lanza."""
    if os.path.exists(destino) and os.path.getsize(destino) > 2000:
        return os.path.getsize(destino)
    ultimo_error = None
    for _ in range(reintentos):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            if len(resp.content) <= 2000:
                raise RuntimeError(f"audio vacio ({len(resp.content)} bytes)")
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino, "wb") as f:
                f.write(resp.content)
            return len(resp.content)
        except Exception as exc:  # noqa: BLE001 - reintento ante transitorios
            ultimo_error = exc
            time.sleep(2)
    raise ultimo_error


def url_ogg(client, cdr_id, access_token=None):
    items = client.get_recording_urls(cdr_id, access_token)
    for it in items:
        if it.get("mimeType") == OGG_MIME and it.get("URL"):
            return it["URL"]
    return None
