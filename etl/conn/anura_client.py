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
