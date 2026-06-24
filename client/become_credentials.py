"""
Encrypted store for the operator's sudo ("become") password, used by the
terminal's "Send sudo password" button (and, later, by password-sudo command
dispatch). Lives on the operator's machine under ~/.config/sysible, with the
password encrypted at rest using Fernet (from `cryptography`, already pulled
in by paramiko) keyed by a 0600 key file - the same model as the saved RDP
credentials.

Stored per host name, with a global "*" fallback, so one password can cover a
fleet of identically-provisioned hosts while still allowing per-host
overrides. If `cryptography` is unavailable, nothing is stored (never in
clear text).

This is convenience encryption-at-rest on a single workstation, not a secret
manager: the key sits next to the data (0600), like an SSH private key. Don't
treat it as protection against someone who already has your account.
"""
import json
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_FERNET = True
except Exception:  # pragma: no cover
    _HAVE_FERNET = False

_CONFIG_DIR = Path(os.getenv("SYSIBLE_CONFIG_DIR", str(Path.home() / ".config" / "sysible")))
_KEY_FILE = _CONFIG_DIR / "become.key"
_STORE_FILE = _CONFIG_DIR / "become_creds.json"
_GLOBAL = "*"


def encryption_available():
    return _HAVE_FERNET


def _get_key():
    if not _HAVE_FERNET:
        return None
    try:
        if _KEY_FILE.exists():
            return _KEY_FILE.read_bytes().strip()
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        _KEY_FILE.write_bytes(key)
        os.chmod(_KEY_FILE, 0o600)
        return key
    except OSError:
        return None


def _load():
    try:
        return json.loads(_STORE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _STORE_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(_STORE_FILE, 0o600)
    except OSError:
        pass


def set_password(password, host=_GLOBAL):
    """Store (encrypted) the sudo password for `host` (or globally). A blank
    password removes the stored entry."""
    data = _load()
    if not password:
        data.pop(host, None)
        _save(data)
        return True
    if not _HAVE_FERNET:
        return False
    key = _get_key()
    if not key:
        return False
    try:
        data[host] = Fernet(key).encrypt(password.encode()).decode()
    except Exception:
        return False
    _save(data)
    return True


def get_password(host=_GLOBAL):
    """Decrypted sudo password for `host`, falling back to the global entry,
    or None."""
    data = _load()
    token = data.get(host) or data.get(_GLOBAL)
    if not token or not _HAVE_FERNET:
        return None
    key = _get_key()
    if not key:
        return None
    try:
        return Fernet(key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def is_set(host=_GLOBAL):
    data = _load()
    return bool(data.get(host) or data.get(_GLOBAL))


def clear(host=_GLOBAL):
    data = _load()
    if host in data:
        del data[host]
        _save(data)
