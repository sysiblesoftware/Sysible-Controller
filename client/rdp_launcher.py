"""
Launch a local RDP client (FreeRDP's xfreerdp preferred, Remmina as a
fallback) to connect to a host. The client opens its own window; Sysible
just spawns it detached.

Note on the password: xfreerdp takes it on argv (/p:), so it can briefly
appear in `ps` to other users *on the operator's own machine*. That's a
local-only exposure of a process the operator themselves started; if a
password isn't supplied, the client prompts for it instead.
"""
import os
import shutil
import subprocess
import tempfile
import time

# Looked up in order; first one found wins.
_FREERDP_BINARIES = ["xfreerdp3", "xfreerdp", "wlfreerdp"]
_REMMINA = "remmina"

# How long to watch a freshly-spawned client before deciding it "launched
# OK". A real connection establishes and the client keeps running well past
# this; a failure (no RDP server, refused, auth) exits well within it, so we
# can catch it and show the operator the actual error instead of the window
# just vanishing.
_STARTUP_WATCH_SECONDS = 2.5


def available_client():
    """Return the name of an RDP client we can launch, or None."""
    for b in _FREERDP_BINARIES:
        if shutil.which(b):
            return b
    if shutil.which(_REMMINA):
        return _REMMINA
    return None


def _build_freerdp_args(binary, host, username, domain, password, size):
    args = [binary, f"/v:{host}", "/cert:ignore", "+clipboard"]
    if username:
        args.append(f"/u:{username}")
    if domain:
        args.append(f"/d:{domain}")
    if password:
        args.append(f"/p:{password}")
    if size == "fullscreen":
        args.append("/f")
    elif size == "dynamic":
        args.append("/dynamic-resolution")
    elif size:  # e.g. "1280x800"
        args.append(f"/size:{size}")
    return args


def _build_remmina_args(host, username, domain, size):
    """Remmina ignores resolution on a bare 'rdp://' URI, which is why a
    fallback session opens tiny. Write a temporary .remmina connection
    profile carrying the resolution/scaling instead, and launch that. No
    password is written to the file - Remmina prompts for it."""
    # Map the dialog's size choice to Remmina profile keys.
    # resolution_mode: 1 = use client/initial, 2 = custom WxH
    # scale: 0 none, 1 scaled, 2 dynamic-resolution; viewmode: 1 window, 3 fullscreen
    if size == "fullscreen":
        res = "resolution_mode=1\nscale=2\nviewmode=3\ndynamic_resolution=1\n"
    elif size == "dynamic":
        res = "resolution_mode=1\nscale=2\nviewmode=1\ndynamic_resolution=1\n"
    else:  # e.g. "1280x800"
        w, _, h = size.partition("x")
        res = (f"resolution_mode=2\nresolution_width={w or 1280}\n"
               f"resolution_height={h or 800}\nscale=1\nviewmode=1\n")

    profile = (
        "[remmina]\n"
        f"name=Sysible - {host}\n"
        "protocol=RDP\n"
        f"server={host}\n"
        f"username={username}\n"
        f"domain={domain}\n"
        "ignore-tls-errors=1\n"
        "disablepasswordstoring=1\n"
        "glyph-cache=1\n"
        + res
    )
    d = tempfile.mkdtemp(prefix="sysible-rdp-")
    path = os.path.join(d, "session.remmina")
    with open(path, "w") as f:
        f.write(profile)
    os.chmod(path, 0o600)
    return [_REMMINA, "-c", path]


def _summarize_failure(host, returncode, err_text):
    """Turn an RDP client's exit code + stderr into something an operator can
    act on. The most common surprise is that the target simply has no RDP
    server listening - on Linux that means xrdp (or similar) isn't installed,
    which Sysible's other tools don't set up for you."""
    low = (err_text or "").lower()

    def has(*needles):
        return any(n in low for n in needles)

    if has("errconnect_connect_failed", "connection refused", "unable to connect",
           "errconnect_dns", "name or service not known", "no route to host",
           "transport_connect", "errconnect_connect_cancelled", "freerdp_abort"):
        hint = (
            f"Couldn't reach an RDP server on {host} (TCP 3389).\n\n"
            "RDP needs a remote-desktop server running on the target:\n"
            "  • Linux hosts: install and start an RDP server such as xrdp "
            "(e.g. 'apt install xrdp' / 'dnf install xrdp', then enable it). "
            "A Sysible-managed Linux host does NOT run one by default.\n"
            "  • Windows hosts: enable Remote Desktop.\n"
            "Also confirm the host's firewall allows TCP 3389 from your machine."
        )
    elif has("logon_failure", "errconnect_logon", "authentication", "access denied",
             "errconnect_password", "account", "credential"):
        hint = ("The RDP server rejected the credentials. Check the username, "
                "domain, and password (leave the password blank to be prompted "
                "by the client instead).")
    elif has("errconnect_security", "tls", "certificate", "protocol_security"):
        hint = ("RDP security negotiation failed. The server may require a "
                "different security mode; try again, or use Remmina.")
    else:
        tail = (err_text or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit code {returncode}"
        hint = f"The RDP client exited immediately: {detail}"

    return hint


def launch(host, username="", domain="", password="", size="1280x800"):
    """Spawn an RDP client to `host`. Returns (ok, message).

    Watches the spawned client for a couple of seconds: if it dies right away
    (no RDP server, refused connection, bad credentials) we report *why*
    instead of letting the dialog close as if it had connected."""
    host = (host or "").strip()
    if not host:
        return False, "No host/address given."

    client = available_client()
    if client is None:
        return False, (
            "No RDP client found. Install FreeRDP (the 'freerdp2-x11' / "
            "'freerdp' package providing xfreerdp) or Remmina, then try again."
        )

    if client == _REMMINA:
        args = _build_remmina_args(host, username, domain, size)
        note = ("Launched Remmina (it will prompt for the password). For sharper, "
                "fully resizable RDP, install FreeRDP (the 'freerdp2-x11'/'freerdp' "
                "package that provides xfreerdp).")
    else:
        args = _build_freerdp_args(client, host, username, domain, password, size)
        note = f"Connecting to {host} with {client}…"

    # Capture stderr to a temp file so we can read back the reason if the
    # client exits immediately. (DEVNULL would make a failure invisible -
    # the window would just disappear.)
    try:
        err_file = tempfile.NamedTemporaryFile(
            prefix="sysible-rdp-", suffix=".log", mode="w+", delete=False
        )
    except Exception:
        err_file = None

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=(err_file if err_file is not None else subprocess.DEVNULL),
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"RDP client '{args[0]}' is not installed."
    except Exception as e:
        return False, f"Could not start the RDP client: {e}"

    # Watch briefly for an immediate failure.
    deadline = time.monotonic() + _STARTUP_WATCH_SECONDS
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is None:
            time.sleep(0.1)
            continue
        # Exited within the watch window.
        err_text = ""
        if err_file is not None:
            try:
                err_file.flush()
                err_file.seek(0)
                err_text = err_file.read()
            except Exception:
                pass
        _cleanup(err_file)
        if rc == 0:
            # Clean immediate exit - e.g. Remmina handing off to an already
            # running instance. Treat as launched.
            return True, note
        return False, _summarize_failure(host, rc, err_text)

    # Still running after the watch window - a real session.
    _cleanup(err_file)
    return True, note


def _cleanup(err_file):
    if err_file is None:
        return
    try:
        err_file.close()
        os.unlink(err_file.name)
    except Exception:
        pass
