"""
Encrypted-at-rest store for operators' sudo (become) passwords, kept on the
controller for the web console. This is the single at-rest home for become
passwords; the controller holds a copy in RAM only in transit to the agent
(keyed per task, short TTL — see backend/app.py), and nothing persists them
in clear text anywhere.

Model: a Fernet key encrypts every stored password. When a controller secret
vault master key is resolvable (SYSIBLE_SECRET_KEY / _CMD / the 0600 local
key), this store DERIVES its key from it (secret_vault.derive_fernet_key), so
the sudo vault inherits the same KMS/Vault-pluggable custody as the rest of the
controller — moving the master key off-box protects these passwords too. When
no master key is resolvable it falls back to a co-located run/webgui_sudo.key
(mode 0600), the original zero-config behaviour. Reads try the derived key AND
the legacy file key, so passwords written before the master key existed keep
decrypting and re-wrap under the derived key on their next save.

Entries are keyed by (admin username, scope), where scope is the literal host
label or the sentinel "__all__" (fleet default), so one admin's stored
passwords are isolated per host/fleet and from other admins.
"""
import json
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_FERNET = True
except Exception:  # pragma: no cover
    _HAVE_FERNET = False

import threading
from webgui._jsonstore import atomic_write_json

ALL = "__all__"  # fleet-default scope sentinel

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_KEY_FILE = _RUN_DIR / "webgui_sudo.key"
_DATA_FILE = _RUN_DIR / "webgui_sudo.json"
_LOCK = threading.Lock()  # serialize load->mutate->save (see webgui/_jsonstore.py)


def encryption_available():
    return _HAVE_FERNET


def _derived_key():
    """The master-key-derived Fernet key for this store, or None when no
    controller secret-vault master key is resolvable. Domain-separated via the
    'webgui-sudo' purpose so it can never collide with the audit-chain HMAC key
    or the generic encryption key."""
    if not _HAVE_FERNET:
        return None
    try:
        from backend import secret_vault
        return secret_vault.derive_fernet_key(b"webgui-sudo")
    except Exception:
        return None


def _legacy_key(create=True):
    """The co-located run/webgui_sudo.key. With create=False it is only returned
    when it already exists (used on the read path so a status/read never mints a
    fresh legacy key once the store has migrated to a master-derived key)."""
    if not _HAVE_FERNET:
        return None
    try:
        if _KEY_FILE.exists():
            return _KEY_FILE.read_bytes().strip()
        if not create:
            return None
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Create the key file 0600 FROM THE START. The old write_bytes()+chmod
        # left a brief window where the key (which decrypts every stored sudo
        # password AND the admin bearer-token cookie) was world-readable at the
        # umask default. O_EXCL|O_NOFOLLOW also refuses a pre-planted symlink.
        fd = os.open(str(_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key
    except FileExistsError:
        # Another process created it in the race — read theirs.
        return _KEY_FILE.read_bytes().strip()
    except OSError:
        return None


def _primary_key():
    """Key used for NEW writes: the master-derived key when a vault master key is
    resolvable (so the sudo vault inherits KMS/Vault custody), else the legacy
    co-located file key (generated on first use)."""
    return _derived_key() or _legacy_key(create=True)


def _read_keys():
    """Every key to try when decrypting, newest-custody first, so a token written
    under the legacy file key still decrypts after this host gains a master key."""
    keys = []
    for k in (_derived_key(), _legacy_key(create=False)):
        if k and k not in keys:
            keys.append(k)
    return keys


def _load():
    try:
        data = json.loads(_DATA_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    atomic_write_json(_DATA_FILE, data, indent=None)


def set_password(user: str, scope: str, password: str) -> bool:
    """Encrypt+store a password for (user, scope). Returns False if
    encryption isn't available (we never store it in clear)."""
    if not password:
        return False
    key = _primary_key()
    if not key:
        return False
    with _LOCK:
        data = _load()
        data.setdefault(user, {})[scope] = Fernet(key).encrypt(password.encode()).decode()
        _save(data)
    return True


def get_password(user: str, scope: str):
    """Decrypted password for (user, scope), or None. Callers typically try
    a host-specific scope first, then fall back to ALL. Tries every custody key
    (derived, then legacy) so a token survives the migration to a master key."""
    rec = _load().get(user, {})
    token = rec.get(scope)
    if not token or not _HAVE_FERNET:
        return None
    for key in _read_keys():
        try:
            return Fernet(key).decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            continue
    return None


def resolve(user: str, host_label: str):
    """The password that applies when running on `host_label`: a host-scoped
    entry wins over the fleet default."""
    return get_password(user, host_label) or get_password(user, ALL)


def scopes_set(user: str):
    """Which scopes this admin has a password stored for (for UI status)."""
    return sorted(_load().get(user, {}).keys())


def clear(user: str, scope: str = None):
    with _LOCK:
        data = _load()
        if user not in data:
            return
        if scope is None:
            del data[user]
        else:
            data[user].pop(scope, None)
            if not data[user]:
                del data[user]
        _save(data)
