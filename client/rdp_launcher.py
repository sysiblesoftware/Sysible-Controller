"""
Launch a local RDP client (FreeRDP's xfreerdp preferred, Remmina as a
fallback) to connect to a host. The client opens its own window; Sysible
just spawns it detached.

Note on the password: xfreerdp takes it on argv (/p:), so it can briefly
appear in `ps` to other users *on the operator's own machine*. That's a
local-only exposure of a process the operator themselves started; if a
password isn't supplied, the client prompts for it instead.
"""
import shutil
import subprocess

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


def _build_remmina_args(host, username, domain):
    # Remmina prompts for the password itself (we don't put it in the URI).
    user = username
    if domain and username:
        user = f"{domain}\\{username}"
    target = f"rdp://{user}@{host}" if user else f"rdp://{host}"
    return [_REMMINA, "-c", target]


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
        args = _build_remmina_args(host, username, domain)
        note = "Launched Remmina (it will prompt for the password)."
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
