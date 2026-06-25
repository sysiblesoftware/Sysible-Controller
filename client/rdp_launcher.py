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

# Looked up in order; first one found wins.
_FREERDP_BINARIES = ["xfreerdp3", "xfreerdp", "wlfreerdp"]
_REMMINA = "remmina"


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


def launch(host, username="", domain="", password="", size="1280x800"):
    """Spawn an RDP client to `host`. Returns (ok, message)."""
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
        note = f"Launched {client} to {host}."

    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"Could not start the RDP client: {e}"
    return True, note
