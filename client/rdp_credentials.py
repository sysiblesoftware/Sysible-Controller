"""
Encrypted, per-host storage for remembered RDP credentials.

Lives entirely on the operator's machine (this is desktop client state,
like the SSH terminal's preferences) under ~/.config/sysible. The
password is encrypted at rest with Fernet (AES-128-CBC + HMAC, from the
`cryptography` package that paramiko already pulls in); the symmetric key
is generated once and kept in a 0600 file next to the data - the same
"root/owner-only key file" model SSH private keys use. Username and domain
are not secret and are stored in clear text as identifiers for prefill.

If `cryptography` is somehow unavailable, storage degrades safely: the
username/domain can still be remembered, but the password is dropped
rather than written in clear text.
"""
import json
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_FERNET = True
except Exception:  # pragma: no cover - cryptography is normally present
    _HAVE_FERNET = False

_CONFIG_DIR = Path(os.getenv("SYSIBLE_CONFIG_DIR", str(Path.home() / ".config" / "sysible")))
_KEY_FILE = _CONFIG_DIR / "rdp.key"
_CREDS_FILE = _CONFIG_DIR / "rdp_creds.json"


def encryption_available():
    return _HAVE_FERNET


def _get_key():
    """Load (or create, 0600) the Fernet key. Returns bytes or None."""
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


def _load_all():
    try:
        return json.loads(_CREDS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all(data):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CREDS_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(_CREDS_FILE, 0o600)
    except OSError:
        pass


def load(host):
    """Return {'username','domain','password'} for a remembered host, or
    None. password is '' if it couldn't be decrypted or wasn't stored."""
    rec = _load_all().get(host)
    if not rec:
        return None
    out = {"username": rec.get("username", ""), "domain": rec.get("domain", ""), "password": ""}
    enc = rec.get("password_enc")
    if enc and _HAVE_FERNET:
        key = _get_key()
        if key:
            try:
                out["password"] = Fernet(key).decrypt(enc.encode()).decode()
            except (InvalidToken, ValueError):
                out["password"] = ""
    return out


def save(host, username, domain, password):
    """Remember credentials for `host`. The password is encrypted; if
    encryption is unavailable it is simply not stored (never in clear)."""
    data = _load_all()
    rec = {"username": username or "", "domain": domain or ""}
    if password and _HAVE_FERNET:
        key = _get_key()
        if key:
            try:
                rec["password_enc"] = Fernet(key).encrypt(password.encode()).decode()
            except Exception:
                pass
    data[host] = rec
    _save_all(data)


def forget(host):
    data = _load_all()
    if host in data:
        del data[host]
        _save_all(data)


def is_remembered(host):
    return host in _load_all()


def list_hosts():
    """Sorted list of host names with remembered RDP details, so the dialog
    can offer them for one-click reconnect instead of retyping the address."""
    return sorted(_load_all().keys())
