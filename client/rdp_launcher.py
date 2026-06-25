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


def _build_freerdp_args(binary, host, username, domain, password, size, screen_size=None):
    # Quality: open the session at a real, fixed resolution and let the GFX
    # (H.264/RemoteFX) pipeline carry it. We deliberately do NOT use
    # /dynamic-resolution - it renegotiates the remote desktop down to the
    # initial window size and renders blurry/upscaled; a fixed full-resolution
    # session is crisp (matches what `xfreerdp /size:WxH` looks like by hand).
    # /network:auto is also left off: on a misjudged link it silently turns on
    # compression and drops visual quality.
    # /gfx:AVC444 = H.264 4:4:4 chroma = sharpest text (4:2:0 smears it).
    args = [binary, f"/v:{host}", "/cert:ignore", "+clipboard", "/gfx:AVC444", "/bpp:32"]
    if username:
        args.append(f"/u:{username}")
    if domain:
        args.append(f"/d:{domain}")
    if password:
        args.append(f"/p:{password}")
    if size == "fullscreen":
        args.append("/f")
    elif size == "dynamic":
        # "Fit my screen": a fixed session at the local screen's real pixel
        # resolution - large and sharp, no dynamic-resolution blur.
        if screen_size:
            args.append(f"/size:{screen_size}")
    elif size:  # explicit "WxH" - a sharp fixed window at exactly that size
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


def _raw_error_line(err_text, returncode):
    """The most informative single line from the client's stderr - usually the
    last [ERROR] line or the ERRCONNECT_* code. Shown verbatim so we diagnose
    from what actually happened instead of guessing."""
    lines = [ln.strip() for ln in (err_text or "").splitlines() if ln.strip()]
    if not lines:
        return f"exit code {returncode}"
    for ln in reversed(lines):
        low = ln.lower()
        if "errconnect" in low or "[error]" in low or "error:" in low:
            return ln
    return lines[-1]


def _summarize_failure(host, returncode, err_text):
    """Turn an RDP client's exit code + stderr into something an operator can
    act on, and always append the raw client error so nothing is hidden."""
    low = (err_text or "").lower()
    raw = _raw_error_line(err_text, returncode)

    def has(*needles):
        return any(n in low for n in needles)

    if has("errconnect_logon", "logon_failure", "errconnect_password",
           "errconnect_account", "access denied", "authentication", "credential",
           "nla", "errconnect_no_or_missing_credentials"):
        hint = ("The RDP server rejected (or required) credentials. Windows "
                "hosts usually require Network Level Authentication, so the "
                "username/domain/password must be correct and non-blank. Check "
                "them and try again.")
    elif has("errconnect_connect_failed", "connection refused", "unable to connect",
             "errconnect_dns", "name or service not known", "no route to host",
             "transport_connect", "timed out", "timeout"):
        hint = (
            f"Couldn't open a TCP connection to {host} on port 3389.\n"
            "  • Confirm the host is reachable from this machine (VPN / subnet / "
            "name resolution).\n"
            "  • On a Windows target, make sure Remote Desktop is enabled and "
            "its firewall allows 3389 from your network.")
    elif has("errconnect_security", "tls", "certificate", "protocol_security",
             "negotiat"):
        hint = ("RDP security/TLS negotiation failed. The server may require a "
                "different security mode; try again, or use Remmina.")
    elif has("dynamic-resolution", "unknown option", "invalid", "usage:",
             "command line"):
        hint = ("The RDP client rejected a command-line option (likely the "
                "Dynamic display mode on this FreeRDP build). Pick a fixed size "
                "in the Display dropdown and try again.")
    else:
        hint = "The RDP client exited immediately."

    return f"{hint}\n\nClient reported: {raw}"


def launch(host, username="", domain="", password="", size="1280x800", screen_size=None):
    """Spawn an RDP client to `host`. Returns (ok, message).

    `screen_size` ("WxH") is the local screen size, used as the initial desktop
    size for the dynamic/fit-window mode so it opens crisp at full size.

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
        args = _build_freerdp_args(client, host, username, domain, password, size, screen_size)
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
