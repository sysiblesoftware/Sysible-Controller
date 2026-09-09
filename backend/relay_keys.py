"""
Relay identity key material: the ed25519 key a bastion authorizes so the
controller can ProxyJump through it.

Created on demand, the first time the relay needs it, and NEVER regenerated — a
bastion has already put the public half in its authorized_keys, so minting a
fresh key would silently lock the controller out of every host behind it. That
never-clobber rule is the whole reason this lives in one place rather than being
open-coded wherever a key is needed.

Uses `cryptography` (already a core dependency) rather than shelling to
ssh-keygen, which is not guaranteed on a minimal controller host, with an
ssh-keygen fallback.
"""
import os
from pathlib import Path


def _default_relay_key():
    """Where the controller keeps its relay private key.

    Derived from the DATABASE's directory, not a fixed /opt path: in a container
    the code tree is rebuilt on every update and only the data volume survives, so
    a key under /opt would be regenerated on each rebuild and every bastion would
    stop trusting the controller. SYSIBLE_DB_PATH already points at that volume.
    """
    override = (os.getenv("SYSIBLE_RELAY_KEY_PATH") or "").strip()
    if override:
        return override
    try:
        from backend.db import DB_PATH
        return str(Path(DB_PATH).resolve().parent / "relay_keys" / "relay_ed25519")
    except Exception:
        return "/opt/sysible/relay_keys/relay_ed25519"


DEFAULT_RELAY_KEY = _default_relay_key()


def _mint_openssh_ed25519(comment):
    """Mint a fresh OpenSSH-format ed25519 keypair; returns (priv_pem_str, pub_line_str)."""
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.OpenSSH, _ser.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        _ser.Encoding.OpenSSH, _ser.PublicFormat.OpenSSH).decode() + f" {comment}\n"
    return priv, pub


def _pub_line_from_priv(priv_pem, comment):
    """Derive the OpenSSH public line from an OpenSSH-format private key string."""
    from cryptography.hazmat.primitives import serialization as _ser
    key = _ser.load_ssh_private_key(priv_pem.encode(), password=None)
    return key.public_key().public_bytes(
        _ser.Encoding.OpenSSH, _ser.PublicFormat.OpenSSH).decode() + f" {comment}\n"


def _materialize(priv_path, priv_pem, pub_line):
    """Write the shared key to the local path (0600 private) if missing/different, so
    file-based SSH tooling on THIS replica reads the same key every other replica has."""
    try:
        os.makedirs(os.path.dirname(priv_path), exist_ok=True)
    except OSError:
        pass
    try:
        cur = None
        if os.path.isfile(priv_path):
            with open(priv_path, "r", encoding="utf-8") as fh:
                cur = fh.read()
        if cur != priv_pem:
            # Replace atomically-ish: remove then O_EXCL create 0600 (umask-independent).
            try:
                if os.path.isfile(priv_path):
                    os.remove(priv_path)
            except OSError:
                pass
            fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, priv_pem.encode())
            finally:
                os.close(fd)
        if pub_line:
            # O_NOFOLLOW: refuse to follow a symlink planted at the .pub path (which could
            # redirect this write to overwrite an attacker-chosen file). O_TRUNC because the
            # .pub content legitimately changes when the DB-authoritative key is materialised.
            _pfd = os.open(priv_path + ".pub",
                           os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            try:
                os.write(_pfd, pub_line.encode())
            finally:
                os.close(_pfd)
        return True
    except Exception:
        return False


def ensure_ed25519_key(priv_path, comment):
    """Create an ed25519 keypair (OpenSSH format) at ``priv_path`` (+ ``.pub``) if absent.

    Uses `cryptography` (a core dependency) rather than shelling to ssh-keygen, which isn't
    guaranteed on a minimal controller host, with an ssh-keygen fallback. The private key is
    written 0600. Returns True if the key exists afterwards (created or already there), False
    if it couldn't be created (e.g. the directory isn't writable). Never regenerates an
    existing key -- so it's safe to call on every bootstrap and at install time."""
    if not priv_path:
        return False
    # Never clobber existing key material: if the private key OR its .pub is already present,
    # leave it alone (a pre-existing .pub with the private key held elsewhere is a valid setup).
    if os.path.isfile(priv_path) or os.path.isfile(priv_path + ".pub"):
        return True
    try:
        os.makedirs(os.path.dirname(priv_path), exist_ok=True)
    except OSError:
        pass
    try:
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        priv_pem = key.private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.OpenSSH, _ser.NoEncryption())
        pub_line = key.public_key().public_bytes(
            _ser.Encoding.OpenSSH, _ser.PublicFormat.OpenSSH).decode() + f" {comment}\n"
        # umask-independent 0600 for the private key, created EXCLUSIVELY: O_EXCL fails if the
        # path already exists (the isfile precheck above already guarantees it doesn't, so this
        # is consistent, not a behaviour change) and O_NOFOLLOW refuses to follow a symlink --
        # together they close the TOCTOU/symlink-plant window where the private key could be
        # written through an attacker-controlled link to a path outside the key directory.
        fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, priv_pem)
        finally:
            os.close(fd)
        # The private key above was created O_EXCL (path didn't pre-exist), and we only
        # reach here when neither the key nor its .pub existed, so O_EXCL|O_NOFOLLOW on the
        # .pub is consistent and closes the symlink-plant window on it too.
        _pfd = os.open(priv_path + ".pub",
                       os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            os.write(_pfd, pub_line.encode())
        finally:
            os.close(_pfd)
        return True
    except Exception:
        import subprocess
        try:
            if os.path.exists(priv_path):
                os.remove(priv_path)
            gen = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", comment, "-f", priv_path],
                capture_output=True, text=True, timeout=15)
            if gen.returncode == 0 and os.path.isfile(priv_path):
                os.chmod(priv_path, 0o600)
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False
