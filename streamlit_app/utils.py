import os
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Carica variabili da .env
load_dotenv()

# URL base del backend (default: http://localhost:8000)
BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")


def _raise_verbose_http_error(resp: requests.Response):
    """Mostra dettagli del body in caso di errore HTTP (utile per debug 500)."""
    try:
        content = resp.json()
    except Exception:
        content = resp.text
    message = f"{resp.status_code} {resp.reason} for {resp.url}\nBody: {content}"
    raise requests.HTTPError(message, response=resp)


# ------------------------
# Sessione con retry
# ------------------------
_session = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(502, 503, 504),
    allowed_methods=("GET", "POST")
)
_session.mount("http://", HTTPAdapter(max_retries=_retries))
_session.mount("https://", HTTPAdapter(max_retries=_retries))


# ------------------------
# API Helpers
# ------------------------
def api(path: str, params=None, timeout=60):
    """
    GET JSON verso il backend.
    timeout è il timeout *read* massimo in secondi.
    Il connect timeout rimane fisso a 10s (robusto).
    """
    url = f"{BACKEND}{path if path.startswith('/') else '/' + path}"
    r = _session.get(url, params=params or {}, timeout=(10, timeout))
    if not r.ok:
        _raise_verbose_http_error(r)
    return r.json()


def api_post(path: str, payload: dict, timeout=900):
    """
    POST JSON verso il backend.
    timeout è il timeout *read* massimo in secondi.
    Usare timeout alto (es. 300–1200) per training GDS.
    """
    url = f"{BACKEND}{path if path.startswith('/') else '/' + path}"
    r = _session.post(url, json=payload or {}, timeout=(10, timeout))
    if not r.ok:
        _raise_verbose_http_error(r)
    return r.json()
