"""
Single place where the desktop client talks to the Sysible backend.

Every page should go through here instead of calling `requests`
directly, so the API base URL and the admin API key are only
configured in one spot.
"""

import base64
import json
import os
import random
import re
import secrets
import shlex
import string
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SYSIBLE_API_URL", "https://127.0.0.1:9000")

_API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))

# =========================================================
# TLS
# The controller serves a self-signed cert (no public domain on a
# LAN deployment, so a CA-signed one isn't an option). Rather than
# disabling verification - which would accept ANY cert and defeat
# the point of TLS - pin the specific cert install_sysible.sh
# generated. If the GUI runs on a different machine than the
# controller, copy that machine's $BASE/certs/server.crt over and
# point SYSIBLE_CA_CERT at the local copy.
# =========================================================
_CA_CERT_FILE = os.getenv("SYSIBLE_CA_CERT", "/opt/sysible/certs/server.crt")

if BASE_URL.startswith("https://"):
    if os.path.exists(_CA_CERT_FILE):
        _VERIFY = _CA_CERT_FILE
    else:
        # Fail closed (default cert validation) rather than trusting
        # blindly - this will reject the self-signed cert until the
        # admin sets SYSIBLE_CA_CERT, which is the correct failure mode.
        print(
            f"[api] warning: no pinned CA cert found at {_CA_CERT_FILE} - "
            "set SYSIBLE_CA_CERT to a local copy of the controller's "
            "certs/server.crt. TLS verification will likely fail until then."
        )
        _VERIFY = True
else:
    _VERIFY = True


def _load_api_key():
    env_key = os.getenv("SYSIBLE_API_KEY")
    if env_key:
        return env_key.strip()

    if _API_KEY_FILE.exists():
        return _API_KEY_FILE.read_text().strip()

    return None


_API_KEY = _load_api_key()


def _headers():
    return {"X-API-Key": _API_KEY} if _API_KEY else {}


_SESSION = requests.Session()


def ping():
    try:
        r = _SESSION.get(f"{BASE_URL}/", timeout=2, verify=_VERIFY)
        return r.ok
    except requests.RequestException:
        return False


def _request(method, path, **kwargs):
    r = _SESSION.request(
        method,
        f"{BASE_URL}{path}",
        headers=_headers(),
        timeout=kwargs.pop("timeout", 15),
        verify=_VERIFY,
        **kwargs,
    )

    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass

        raise requests.exceptions.HTTPError(
            f"{r.status_code} {detail or r.reason}", response=r
        )

    if not r.content:
        return None

    return r.json()


def _download_binary(path):
    r = _SESSION.get(
        f"{BASE_URL}{path}", headers=_headers(), timeout=30, verify=_VERIFY
    )

    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass

        raise requests.exceptions.HTTPError(
            f"{r.status_code} {detail or r.reason}", response=r
        )

    return r.content
