import base64
import time

import requests

from config.settings import CLIENT_ID, CLIENT_PASSWORD

TOKEN_URL = (
    "https://sso.anura.com.ar/"
    "auth/realms/anura/protocol/openid-connect/token"
)

GCAPI_URL = "https://api.anura.com.ar/GCAPI/JSON-RPC"

EXPORT_CDRS_URL = "https://api.anura.com.ar/GCAPI/Download/ExportCdrs"

# Robustez (pack 1): timeout fijo + reintentos con backoff ante fallas
# transitorias de red. Sin timeout, requests puede colgar para siempre.
TIMEOUT = 60
RETRIES = 3


def _request(method, url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2 ** attempt)
    raise last_error


class AnuraClient:
    def __init__(self, client_id=None, client_password=None):
        self.client_id = client_id or CLIENT_ID
        self.client_password = client_password or CLIENT_PASSWORD

    def get_access_token(self):
        credentials = f"{self.client_id}:{self.client_password}"
        credentials_base64 = base64.b64encode(credentials.encode()).decode()

        response = _request(
            "POST",
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials_base64}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "offline_access",
            },
        )
        return response.json()["access_token"]

    def get_gcapi_token(self, access_token=None):
        token = access_token or self.get_access_token()

        response = _request(
            "POST",
            GCAPI_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "id": 6,
                "method": "AdminService.getDownloadToken",
                "params": [],
            },
        )
        return response.json()["result"]

    def download_cdrs_csv(self, start_date_text, end_date_text, gcapi_token=None):
        token = gcapi_token or self.get_gcapi_token()

        response = _request(
            "GET",
            EXPORT_CDRS_URL,
            params={
                "Authorization": token,
                "startDate": start_date_text,
                "endDate": end_date_text,
                "filter": "",
                "extraInfo": "true",
                "accountId": "",
            },
        )
        return response.text

    def gcapi(self, method, params, access_token=None):
        """Llamada JSON-RPC generica a GCAPI (verificado: system.listMethods
        devuelve 344 metodos). params es lista posicional."""
        token = access_token or self.get_access_token()

        response = _request(
            "POST",
            GCAPI_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "id": 1,
                "method": method,
                "params": params,
            },
        )
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(
                f"GCAPI {method}: {payload['error'].get('msg', payload['error'])}"
            )
        return payload.get("result")

    def get_cdrs_day(self, dia_text, access_token=None, max_paginas=20):
        """CDRs de un dia ('YYYY-MM-DD') con id/wasRecorded/calling/dialTime.

        El 3er arg de getCdrs es NUMERO DE PAGINA, no flag (verificado
        2026-10-05: p1=487 filas, p2=529 con 118 ids nuevos, union=613 =
        filas del ExportCdrs del mismo dia). Sin paginar se pierden ~20 %
        de las piernas (entre ellas MULTIDIAL grabadas). Recorre paginas
        hasta que una no aporta ids nuevos."""
        vistos = set()
        out = []
        for pagina in range(1, max_paginas + 1):
            lote = self.gcapi(
                "AdminService.getCdrs", [dia_text, dia_text, pagina],
                access_token,
            ) or []
            nuevos = [c for c in lote if c.get("id") not in vistos]
            for c in nuevos:
                vistos.add(c.get("id"))
            out.extend(nuevos)
            if not nuevos:
                break
        return out

    def get_recording_urls(self, cdr_id, access_token=None):
        """Lista [{mimeType, URL}] (ogg/mpeg/wav). URLs firmadas: vencen en
        30 minutos, descargar en el acto."""
        return self.gcapi(
            "AdminService.getRecordingURL", [cdr_id], access_token
        ) or []
