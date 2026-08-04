"""Controller-side secret custody — encryption at rest for sensitive values
kept by the controller (the console sudo-password vault, and the key that
makes the tamper-evident activity chain unforgeable).

Master-key resolution, highest priority first:

  1. SYSIBLE_SECRET_KEY      — a urlsafe-base64 Fernet key, injected at runtime
                               from your platform's secret store (Vault, cloud
                               KMS). Nothing sensitive touches the filesystem.
  2. SYSIBLE_SECRET_KEY_CMD  — a shell command whose stdout IS that key; the
                               hook to fetch it from an external KMS/Vault/agent
                               at startup without baking the key into config.
  3. <run dir>/controller_secret.key (0600) — generated on first use if absent;
                               the zero-config default, a root-only local key.

Stored values are 'v1:<fernet-token>'. decrypt() also passes bare legacy
plaintext (no 'v1:' prefix) through unchanged, so turning encryption on never
strands a value written before it — the value is transparently re-encrypted the
next time it's saved.

The primitive is deliberately generic (encrypt/decrypt of arbitrary strings) so
the same custody applies as more secrets move behind it.
"""
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - cryptography is a hard dep, but degrade safely
    _HAVE_CRYPTO = False

_PREFIX = "v1:"
_KEY_FILE_NAME = "controller_secret.key"
_REPO_ROOT = Path(__file__).resolve().parent.parent

_cached_key = None  # bytes | None


def _run_dir() -> Path:
    d = (os.getenv("SYSIBLE_RUN_DIR")
         or os.getenv("SYSIBLE_DATA_DIR")
         or str(_REPO_ROOT / "run"))
    return Path(d)


def _key_from_cmd():
    """Run SYSIBLE_SECRET_KEY_CMD and return its stdout as the key, or None."""
    cmd = os.getenv("SYSIBLE_SECRET_KEY_CMD")
    if not cmd:
        return None
    import subprocess
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             timeout=10, check=True).stdout
        return out.strip() or None
    except Exception:
        return None


def _load_or_create_key():
    """Resolve the master key (env → command → 0600 file, generating the file on
    first use). Cached for the process; call reset_cache() in tests."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    env = os.getenv("SYSIBLE_SECRET_KEY")
    if env:
        _cached_key = env.strip().encode()
        return _cached_key

    # When the operator explicitly configured an off-box key source
    # (SYSIBLE_SECRET_KEY_CMD → KMS/Vault), that source is AUTHORITATIVE. If it
    # yields nothing (KMS outage, bad command), we must FAIL CLOSED — NOT silently
    # fall through to the local file key. Falling through would (a) key secrets
    # under an unintended on-box key during an outage and (b) be undetectable to
    # strict mode (a key still "resolves"). Returning None makes encrypt() raise in
    # strict mode / warn loudly otherwise, rather than degrade to a different key.
    if os.getenv("SYSIBLE_SECRET_KEY_CMD"):
        from_cmd = _key_from_cmd()
        if from_cmd:
            _cached_key = from_cmd
            return _cached_key
        _warn_key_cmd_failed_once()
        return None

    if not _HAVE_CRYPTO:
        return None

    kf = _run_dir() / _KEY_FILE_NAME
    try:
        try:
            # Create-exclusively with O_EXCL|O_NOFOLLOW at 0600 (no O_TRUNC), the
            # same hardening as auth.py's API-key file: a local user can't pre-plant
            # a symlink to redirect the freshly-generated MASTER key to an
            # attacker-readable path, and there's no exists()->open() TOCTOU. This
            # key decrypts every vault secret and derives the audit HMAC key, so its
            # creation must be race-safe.
            key = Fernet.generate_key()
            fd = os.open(str(kf), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            _cached_key = key
        except FileExistsError:
            # Already present — read it back, refusing to follow a symlink.
            rfd = os.open(str(kf), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                _cached_key = os.read(rfd, 4096).strip()
            finally:
                os.close(rfd)
    except Exception:
        # Couldn't create the dir/file (e.g. no perms) — try a read as a last resort
        # so an existing key on a read-only mount still works. Still refuse to follow
        # a symlink (O_NOFOLLOW): the primary read above is O_NOFOLLOW, so this
        # fallback must be too, or a planted link at the key path would defeat it.
        try:
            kf.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            rfd = os.open(str(kf), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                _cached_key = os.read(rfd, 4096).strip()
            finally:
                os.close(rfd)
        except Exception:
            return None
    return _cached_key


def _coerce_to_fernet_key(raw):
    """Return a VALID Fernet key (urlsafe-b64 of 32 bytes) for ``raw``.

    If ``raw`` is already a valid Fernet key it is returned UNCHANGED — so an
    operator-provided key, the auto-created 0600 local file key, and a
    KMS/Vault-issued Fernet key all key existing ciphertext exactly as before.
    Otherwise a key is DERIVED from it by SHA-256. This lets SYSIBLE_SECRET_KEY
    be any high-entropy string (a passphrase, or a Helm-generated random value)
    without silently disabling at-rest encryption: a non-Fernet-format value used
    to make Fernet() raise → _fernet() returned None → strict mode refused every
    write and non-strict stored PLAINTEXT. Derivation is deterministic, so a
    derived key round-trips its own ciphertext. NOTE: audit_hmac_key() /
    derive_fernet_key() intentionally HMAC the RAW master key (not this coerced
    form), so their outputs are unchanged."""
    if not raw:
        return None
    kb = raw if isinstance(raw, bytes) else str(raw).encode()
    if _HAVE_CRYPTO:
        try:
            Fernet(kb)          # already a valid 32-byte urlsafe-b64 key → use as-is
            return kb
        except Exception:
            pass
    import base64
    import hashlib
    return base64.urlsafe_b64encode(hashlib.sha256(kb).digest())


def available() -> bool:
    """True if values can actually be encrypted — crypto present AND a working
    Fernet can be built from the resolved key. Actually constructs the Fernet
    (via _fernet) rather than only checking that a key string resolved, so a
    malformed/short master key reports the vault as UNAVAILABLE instead of
    healthy-but-silently-broken."""
    return _HAVE_CRYPTO and _fernet() is not None


def key_source() -> str:
    """Where the master key comes from, for status display (never the key itself):
    'env' (SYSIBLE_SECRET_KEY), 'command' (SYSIBLE_SECRET_KEY_CMD → KMS/Vault),
    'local-file' (auto-created 0600 file), or 'none' (no crypto / unresolvable)."""
    if os.getenv("SYSIBLE_SECRET_KEY"):
        return "env"
    if os.getenv("SYSIBLE_SECRET_KEY_CMD"):
        return "command"
    if not _HAVE_CRYPTO:
        return "none"
    return "local-file"


def _fernet():
    if not _HAVE_CRYPTO:
        return None
    key = _coerce_to_fernet_key(_load_or_create_key())
    if not key:
        return None
    try:
        return Fernet(key)
    except Exception:
        return None


def is_encrypted(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(_PREFIX)


_warned_unavailable = False


def _warn_unavailable_once():
    global _warned_unavailable
    if not _warned_unavailable:
        _warned_unavailable = True
        import sys
        print("[secret_vault] WARNING: encryption unavailable (no cryptography or no "
              "resolvable master key) — secrets are being stored WITHOUT at-rest "
              "encryption. Set SYSIBLE_SECRET_KEY / SYSIBLE_SECRET_KEY_CMD or ensure "
              "the key file is writable.", file=sys.stderr)


_warned_key_cmd_failed = False


def _warn_key_cmd_failed_once():
    global _warned_key_cmd_failed
    if not _warned_key_cmd_failed:
        _warned_key_cmd_failed = True
        import sys
        print("[secret_vault] WARNING: SYSIBLE_SECRET_KEY_CMD is configured but "
              "returned no key (KMS/Vault outage or bad command). FAILING CLOSED — "
              "the controller will NOT fall back to a local file key, to avoid keying "
              "secrets under an unintended key. Restore the key source; with "
              "SYSIBLE_SECRET_REQUIRED=1 writes of new secrets will raise until it "
              "recovers.", file=sys.stderr)


def _strict_required():
    """At-rest encryption must NOT silently degrade to plaintext — a KMS/key outage
    should fail the write, not store the secret in cleartext. STRICT BY DEFAULT: a
    master key is normally always resolvable (a 0600 local key auto-creates at first
    use), so this only bites a genuinely broken host (no crypto lib / unwritable key
    dir), where failing closed is the correct posture. A deliberate degraded/dev
    environment can opt OUT with SYSIBLE_SECRET_REQUIRED=0."""
    return (os.getenv("SYSIBLE_SECRET_REQUIRED", "1") or "").strip().lower() not in (
        "0", "false", "no", "off")


class EncryptionUnavailable(RuntimeError):
    """Raised in strict mode when a secret cannot be encrypted at rest."""


def encrypt(plaintext):
    """Encrypt a secret for storage → 'v1:<token>'. If encryption isn't available
    (no cryptography, or no resolvable key), returns the plaintext unchanged so
    the caller still works — decrypt() round-trips either form — but emits a loud
    one-time warning. In STRICT mode (SYSIBLE_SECRET_REQUIRED=1) it instead RAISES
    EncryptionUnavailable so a key outage fails the write rather than silently
    persisting cleartext. None passes through untouched."""
    if plaintext is None:
        return plaintext
    f = _fernet()
    if not f:
        if _strict_required():
            raise EncryptionUnavailable(
                "at-rest encryption is required (SYSIBLE_SECRET_REQUIRED=1) but no "
                "master key is resolvable — refusing to store the secret in plaintext")
        _warn_unavailable_once()
        return plaintext
    try:
        return _PREFIX + f.encrypt(str(plaintext).encode()).decode()
    except Exception:
        if _strict_required():
            raise EncryptionUnavailable("at-rest encryption failed and is required")
        return plaintext


def decrypt(stored):
    """Inverse of encrypt(). Bare legacy plaintext (no 'v1:' prefix) is returned
    unchanged. A value that can't be decrypted is returned as-is (the caller then
    simply fails to verify against it, rather than crashing)."""
    if not stored or not is_encrypted(stored):
        return stored
    f = _fernet()
    if not f:
        return stored
    try:
        return f.decrypt(stored[len(_PREFIX):].encode()).decode()
    except Exception:
        return stored


def reset_cache():
    """Test hook: forget the cached master key (e.g. after changing env vars)."""
    global _cached_key
    _cached_key = None


def audit_hmac_key_from(master_key):
    """Derive the audit-chain HMAC key from an explicit master key (bytes), or None
    if no key. Domain-separated so it never equals the Fernet encryption key. Used
    directly by master-key rotation, which must re-key the chain under the new key."""
    if not master_key:
        return None
    import hashlib
    import hmac as _hmac
    mk = master_key if isinstance(master_key, bytes) else str(master_key).encode()
    return _hmac.new(mk, b"sysible-audit-chain-v1", hashlib.sha256).digest()


def derive_fernet_key(purpose):
    """A domain-separated **Fernet key** (urlsafe-b64, 32 bytes) derived from the
    resolved master key, or None when no master key is resolvable. Lets a secondary
    at-rest store (e.g. the console sudo-password vault) inherit the SAME KMS/Vault-
    pluggable master-key custody instead of minting its own co-located key — so
    moving the master key off-box protects those secrets too. `purpose` is a bytes
    label that separates this key from the encryption key and other derived keys."""
    mk = _load_or_create_key()
    if not mk:
        return None
    import base64
    import hashlib
    import hmac as _hmac
    p = purpose if isinstance(purpose, bytes) else str(purpose).encode()
    digest = _hmac.new(mk if isinstance(mk, bytes) else str(mk).encode(),
                       b"sysible-fernet-" + p, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


def audit_hmac_key():
    """A stable HMAC key for the tamper-evident audit chain, derived from the
    resolved master key so it needs no separate custody and rotates with it.
    Returns bytes, or None when no master key is resolvable (the chain then falls
    back to plain SHA-256).

    Deriving from the master key means that when that key lives off-box (KMS/env,
    the recommended enterprise posture), a root attacker who can edit the DB still
    cannot forge the chain — they lack the key to recompute valid entry HMACs."""
    return audit_hmac_key_from(_load_or_create_key())


# ----------------------------------------------------------------------
# Explicit-key ops — used by master-key rotation (re-wrapping stored values
# from an old key to a new one). Separate from the ambient-key encrypt/decrypt
# so a rotation can hold both keys at once.
# ----------------------------------------------------------------------
def encrypt_with(plaintext, key):
    if plaintext is None or not _HAVE_CRYPTO or not key:
        return plaintext
    try:
        return _PREFIX + Fernet(key).encrypt(str(plaintext).encode()).decode()
    except Exception:
        return plaintext


def decrypt_with(stored, key):
    if not stored or not is_encrypted(stored) or not _HAVE_CRYPTO or not key:
        return stored
    try:
        return Fernet(key).decrypt(stored[len(_PREFIX):].encode()).decode()
    except Exception:
        return stored
