"""
Validates and installs externally-issued (PKI) TLS certificates for the
controller, as an alternative to the self-signed cert install_sysible.sh
generates on first run (see that script's "PROVISION THE TLS CERTIFICATE"
block).

Three files live in certs/ once this (or install_sysible.sh) has run:
  - server.crt   The leaf cert, plus the intermediate chain concatenated
                  on ("fullchain" style) if one was uploaded - this is
                  what gets handed to uvicorn's --ssl-certfile at
                  startup.
  - server.key   The private key - --ssl-keyfile.
  - trust.crt    What gets copied out to clients/agents for pinning (see
                  client/api.py's _CA_CERT_FILE / host_agent/agent.py's
                  pinned-cert handling - both just read whatever file is
                  at their configured local path, so nothing there has
                  to change to support this). For a self-signed leaf
                  this is identical to server.crt (a self-signed cert is
                  its own trust anchor). For a PKI-issued leaf it has to
                  be the *issuing* CA chain instead - the leaf alone
                  does NOT verify against itself the way a self-signed
                  one does, so pinning the leaf again would just break
                  TLS verification for every client/agent that picks it
                  up.

Nothing in client/api.py or host_agent/agent.py needs to change to
support PKI certs - installing one here only changes what gets copied
out to refresh those local pinned-cert files going forward (trust.crt's
content instead of server.crt's), and what new agent bundles embed (see
backend/agent_bundle.py).
"""

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = Path(os.getenv("SYSIBLE_CERT_DIR", str(PROJECT_ROOT / "certs")))
CERT_FILE = CERT_DIR / "server.crt"
KEY_FILE = CERT_DIR / "server.key"
TRUST_FILE = CERT_DIR / "trust.crt"

SERVICE_NAME = "sysible-backend"


class TLSValidationError(ValueError):
    """Raised for any problem with an uploaded cert/key/chain that
    should be surfaced to the admin as a 400, not a 500 - bad/corrupt
    PEM, key/cert mismatch, expired cert, password-protected key, etc."""


def _utcnow():
    return datetime.now(timezone.utc)


def _split_pem_blocks(pem_bytes: bytes, label: str) -> list:
    """A cert or chain file may contain more than one PEM block
    (leaf+intermediate+root, all concatenated back to back) - split on
    BEGIN/END markers rather than assuming exactly one block."""
    try:
        text = pem_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TLSValidationError(f"File is not valid PEM text: {e}") from e

    begin, end = f"-----BEGIN {label}-----", f"-----END {label}-----"
    blocks, pos = [], 0
    while True:
        start = text.find(begin, pos)
        if start == -1:
            break
        stop = text.find(end, start)
        if stop == -1:
            raise TLSValidationError(f"Malformed PEM: unterminated {label} block")
        stop += len(end)
        blocks.append(text[start:stop].encode("utf-8"))
        pos = stop
    return blocks


def _load_cert(pem_bytes: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem_bytes)
    except ValueError as e:
        raise TLSValidationError(f"Could not parse certificate: {e}") from e


def _load_chain(pem_bytes: bytes) -> list:
    certs = [_load_cert(block) for block in _split_pem_blocks(pem_bytes, "CERTIFICATE")]
    if not certs:
        raise TLSValidationError("Chain file did not contain any certificates")
    return certs


def _load_key(pem_bytes: bytes):
    try:
        return serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as e:
        raise TLSValidationError(
            "Could not parse private key - make sure this is the PEM "
            "private key that matches the certificate, with no "
            f"passphrase (password-protected keys aren't supported): {e}"
        ) from e


def _public_key_bytes(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _sha256_fingerprint(leaf: x509.Certificate) -> str:
    """SHA-256 fingerprint of the cert (DER), as upper-case colon-separated hex —
    the same form `openssl x509 -fingerprint -sha256` prints. This is the value an
    admin reads out-of-band to confirm the self-signed cert their agents/browser
    pinned is the genuine one (trust-on-first-use verification)."""
    import hashlib
    digest = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _cert_metadata(leaf: x509.Certificate, chain_length: int = 0) -> dict:
    now = _utcnow()
    days_remaining = (leaf.not_valid_after_utc - now).days
    return {
        "subject": leaf.subject.rfc4514_string(),
        "issuer": leaf.issuer.rfc4514_string(),
        "not_valid_before": leaf.not_valid_before_utc.isoformat(),
        "not_valid_after": leaf.not_valid_after_utc.isoformat(),
        "serial_number": format(leaf.serial_number, "x"),
        "sha256_fingerprint": _sha256_fingerprint(leaf),
        "is_self_signed": leaf.subject == leaf.issuer,
        "chain_length": chain_length,
        "days_remaining": days_remaining,
        "is_expired": days_remaining < 0,
    }


def validate_certificate_bundle(cert_pem: bytes, key_pem: bytes, chain_pem: bytes = None) -> dict:
    """Parses and cross-checks an uploaded cert/key(/chain) WITHOUT
    installing anything. Raises TLSValidationError on any problem;
    otherwise returns metadata about the leaf cert for the caller to
    show the admin before committing."""

    leaf = _load_cert(cert_pem)
    key = _load_key(key_pem)

    if _public_key_bytes(leaf.public_key()) != _public_key_bytes(key.public_key()):
        raise TLSValidationError(
            "This private key does not match the certificate - make "
            "sure you uploaded the key that was issued alongside it."
        )

    now = _utcnow()
    if leaf.not_valid_after_utc < now:
        raise TLSValidationError(
            f"This certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d} "
            "- get a renewed one from your PKI team before installing."
        )
    if leaf.not_valid_before_utc > now:
        raise TLSValidationError(
            f"This certificate is not valid until {leaf.not_valid_before_utc:%Y-%m-%d}."
        )

    chain_certs = _load_chain(chain_pem) if chain_pem else []

    return _cert_metadata(leaf, chain_length=len(chain_certs))


def _atomic_write(path: Path, data: bytes, mode: int):
    """Write `data` to `path` atomically at the given mode: fill a temp file
    (fchmod BEFORE writing so a private key is never briefly world-readable),
    fsync it, then os.replace() into place. os.replace is atomic, so a reader
    (uvicorn at startup) sees either the whole old file or the whole new one —
    never a half-written/truncated file. Writing the cert and key this way means
    a crash / ENOSPC mid-op can't leave a truncated server.crt/server.key that
    wedges the controller's next TLS restart. O_NOFOLLOW: don't follow a symlink
    planted at the temp path."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _write_key_file(path: Path, data: bytes):
    """Atomically write a private key at 0600 (see _atomic_write)."""
    _atomic_write(path, data, 0o600)


def _backup_if_exists(path: Path):
    """Mirrors install_sysible.sh / _api_storage.py's existing
    `cp file file.bak.$(date +%s)` convention - timestamped, never
    overwritten, so a bad upload is always recoverable by hand."""
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak.{int(time.time())}"))


def install_certificate(cert_pem: bytes, key_pem: bytes, chain_pem: bytes = None) -> dict:
    """Validates, then installs, a new cert/key(/chain) as the
    controller's TLS identity. Backs up whatever was there before.
    Does NOT restart the backend - uvicorn only reads these files once
    at startup, so call restart_backend() once this returns (the route
    that calls this does exactly that)."""

    info = validate_certificate_bundle(cert_pem, key_pem, chain_pem)

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    _backup_if_exists(CERT_FILE)
    _backup_if_exists(KEY_FILE)
    _backup_if_exists(TRUST_FILE)

    # server.crt = leaf + chain concatenated ("fullchain") so uvicorn
    # presents the whole chain to connecting clients - some TLS clients
    # won't successfully verify a PKI-issued leaf without the
    # intermediate(s) being served alongside it.
    fullchain = cert_pem.rstrip(b"\n") + b"\n"
    if chain_pem:
        fullchain += chain_pem.rstrip(b"\n") + b"\n"
    # Key first, then cert — each written atomically (temp+fsync+replace) so a
    # crash/ENOSPC can't leave a truncated file that wedges the TLS restart.
    _write_key_file(KEY_FILE, key_pem)
    _atomic_write(CERT_FILE, fullchain, 0o644)

    # trust.crt = what clients/agents should pin going forward - the
    # issuing CA chain for a PKI cert, or the leaf itself when no chain
    # was supplied (preserves today's self-signed behavior, and also
    # covers a cert signed by a CA already in the OS trust store - the
    # admin can simply not redistribute trust.crt in that case and let
    # the existing default-to-system-trust-store fallback in
    # client/api.py / host_agent/agent.py take over).
    trust_pem = chain_pem if chain_pem else cert_pem
    _atomic_write(TRUST_FILE, trust_pem.rstrip(b"\n") + b"\n", 0o644)

    return info


def current_is_self_signed() -> bool:
    """True if the cert currently in front of uvicorn is self-signed (the default),
    False if it's an imported PKI cert — so callers can regenerate the self-signed
    cert on an address change WITHOUT ever clobbering an admin-installed PKI cert.
    Absent cert counts as 'self-signed' (nothing to protect)."""
    if not CERT_FILE.exists():
        return True
    try:
        leaf = _load_cert(CERT_FILE.read_bytes())
        return leaf.subject == leaf.issuer
    except Exception:
        # Fail CLOSED: an unreadable/corrupt cert must NOT be treated as
        # self-signed, or a transiently-corrupt admin-installed PKI cert could
        # be silently regenerated over on the next address change / regen press.
        # Better to refuse to regenerate than to clobber a PKI cert we can't read.
        return False


def regenerate_self_signed(hostnames=None, ips=None, days=3650) -> dict:
    """Generate a fresh self-signed cert+key (RSA-2048, SHA-256) whose SAN covers
    the given hostnames/IPs plus localhost/127.0.0.1, and install it as the
    controller's TLS identity — mirrors install_sysible.sh's openssl cert.

    Call after the controller's hostname/IP changes so TLS hostname verification
    keeps working for agents/clients that verify against the pinned cert. Backs up
    the previous cert/key/trust; sets trust.crt = the new self-signed leaf (agents
    must re-pin / have the refreshed trust bundle redistributed). Does NOT restart
    uvicorn — the caller does that once files are in place."""
    import ipaddress
    from datetime import timedelta
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    san, seen = [], set()

    def _add(value):
        value = (value or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            san.append(x509.DNSName(value))

    # Always keep localhost reachable, then the configured names/IPs.
    _add("localhost")
    _add("127.0.0.1")
    for h in (hostnames or []):
        _add(h)
    for i in (ips or []):
        _add(i)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sysible-controller")])
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    _backup_if_exists(CERT_FILE)
    _backup_if_exists(KEY_FILE)
    _backup_if_exists(TRUST_FILE)
    # Key first, then cert/trust — each atomic (temp+fsync+replace), so a crash
    # mid-regenerate can't leave a truncated pair that fails the TLS restart.
    _write_key_file(KEY_FILE, key_pem)
    _atomic_write(CERT_FILE, cert_pem, 0o644)
    _atomic_write(TRUST_FILE, cert_pem, 0o644)   # self-signed: trust bundle IS the leaf
    return _cert_metadata(cert)


def ensure_trust_file_exists() -> bool:
    """Upgrade path for controllers set up before trust.crt existed
    (anything installed before this feature shipped): if server.crt is
    there but trust.crt isn't yet, the controller is still on its
    original self-signed cert, so trust.crt = server.crt is the correct
    fallback - mirrors what a fresh install_sysible.sh run now seeds up
    front. No-op once trust.crt exists. Returns True if it created the
    file, False otherwise."""
    if TRUST_FILE.exists():
        return False
    if CERT_FILE.exists():
        shutil.copy2(CERT_FILE, TRUST_FILE)
        os.chmod(TRUST_FILE, 0o644)
        return True
    return False


def get_tls_info() -> dict:
    """Metadata about whatever cert is currently in front of uvicorn,
    for the Sysible Settings TLS section to display - never returns key
    material."""
    ensure_trust_file_exists()

    if not CERT_FILE.exists():
        return {"installed": False}

    blocks = _split_pem_blocks(CERT_FILE.read_bytes(), "CERTIFICATE")
    if not blocks:
        return {"installed": False}

    leaf = _load_cert(blocks[0])
    info = _cert_metadata(leaf, chain_length=len(blocks) - 1)
    info["installed"] = True
    info["has_trust_file"] = TRUST_FILE.exists()
    return info


def trust_bundle_bytes() -> bytes:
    """Current trust.crt content, for the "Download Trust Certificate"
    button - the file an admin hands to GUI machines/agents that were
    enrolled before this cert was installed, so they can refresh their
    pinned copy by hand (new agent bundles pick this up automatically -
    see backend/agent_bundle.py)."""
    ensure_trust_file_exists()
    if not TRUST_FILE.exists():
        raise FileNotFoundError("No trust certificate installed yet.")
    return TRUST_FILE.read_bytes()


def restart_backend(delay_seconds: float = 1.5):
    """Restarts the sysible-backend systemd service so uvicorn picks up
    the newly-installed cert/key - it only reads --ssl-certfile/
    --ssl-keyfile once, at process start, there's no dynamic reload.
    The backend runs as root (see install_sysible.sh's systemd unit,
    User=root) so it's allowed to call this on itself.

    The restart is scheduled in a *transient* systemd unit via
    systemd-run, NOT run directly from this process. That matters: a
    plain `systemctl restart sysible-backend` spawned from here runs
    inside sysible-backend's own cgroup, so when systemd tears the
    service down (KillMode=control-group, the default) it kills the
    restarter mid-flight along with everything else in the cgroup. That
    can abort the restart before the start half completes, leaving the
    controller stopped - and because a clean stop doesn't trip
    Restart=always, it stays down (the 'HTTPSConnectionPool / failed to
    establish a new connection' symptom after a cert regen). A
    systemd-run timer fires from PID 1's own scope, outside our cgroup,
    so it survives the teardown and always brings the service back.

    Runs in a background thread after a short delay so the HTTP response
    for the request that triggered this has a chance to flush back to
    the caller before the process serving it gets killed."""

    def _do_restart():
        time.sleep(delay_seconds)
        # Transient timer, detached from this service's cgroup, so the
        # restart survives sysible-backend being killed. --collect reaps
        # the unit once it's done; --on-active gives a beat for the
        # response to flush even if delay_seconds was trimmed.
        try:
            rc = subprocess.call(
                [
                    "systemd-run",
                    "--collect",
                    "--on-active=1",
                    "--timer-property=AccuracySec=100ms",
                    "systemctl", "restart", SERVICE_NAME,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                return
        except (FileNotFoundError, OSError):
            pass
        # Fallback for hosts without systemd-run: at least detach into a
        # new session so the client isn't a direct child of uvicorn.
        try:
            subprocess.Popen(
                ["systemctl", "restart", SERVICE_NAME],
                start_new_session=True,
            )
        except OSError:
            pass

    threading.Thread(target=_do_restart, daemon=True).start()


# Where the on-host agent pins the controller's cert (see agent_bundle.py's
# _CERT_INSTALL_PATH / run_agent.sh) and the local self-agent's service name.
_AGENT_PINNED_CERT = "/etc/sysible/controller.crt"
_AGENT_SERVICE = "sysible-agent"


def refresh_local_agent_trust() -> bool:
    """Re-trust the controller's OWN agent after its TLS cert changed.

    A self-signed leaf can't be rotated without invalidating every pin (proven:
    OpenSSL rejects any depth-0 self-signed cert that isn't byte-identical to the
    pinned one), so the moment the controller reissues its cert, the local
    self-agent's pinned copy at /etc/sysible/controller.crt goes stale and the
    controller drops out of its own fleet — "regenerate killed the controller
    enrollment". Since that agent lives on THIS box, we can fix it in place:
    overwrite its pinned cert with the new leaf and bounce the agent so it
    re-verifies over loopback and re-appears in the fleet.

    Best-effort and local-only: REMOTE agents still need the redistributed trust
    bundle / a fresh agent bundle — nothing on the controller can re-pin a cert on
    a host it can no longer authenticate to. Returns True if a local agent was
    found and its trust refreshed.
    """
    import shutil
    import subprocess
    # Only act if a local agent is actually installed.
    agent_here = os.path.exists("/opt/sysible-agent/agent.py")
    if not agent_here:
        try:
            agent_here = subprocess.run(
                ["systemctl", "is-enabled", f"{_AGENT_SERVICE}.service"],
                capture_output=True, text=True, timeout=5,
            ).returncode == 0
        except Exception:
            agent_here = False
    if not agent_here or not CERT_FILE.exists():
        return False
    try:
        os.makedirs(os.path.dirname(_AGENT_PINNED_CERT), exist_ok=True)
        shutil.copy2(str(CERT_FILE), _AGENT_PINNED_CERT)
        os.chmod(_AGENT_PINNED_CERT, 0o644)
    except Exception:
        return False
    # Bounce the local agent so it reloads the refreshed pin and reconnects. The
    # backend is restarting in parallel; the agent retries until it's back, so a
    # brief overlap is harmless. Detached so this isn't a child of either service.
    try:
        subprocess.Popen(["systemctl", "restart", f"{_AGENT_SERVICE}.service"],
                         start_new_session=True)
    except Exception:
        pass
    return True
