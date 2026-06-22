"""
Webserver Portal - the host-facing surface a remote host operator
reaches in a browser to log in (simple username/password, configured
in the Webserver Portal Configuration page) and download a ready-to-run
agent bundle for this controller.

This is a separate FastAPI app from backend.app:app on purpose - see
backend/portal_manager.py for why it's a standalone process on its own
HTTPS port (same self-signed cert as the controller) rather than
routes on the main API. It does NOT use require_api_key
(backend/auth.py): that key is for the admin GUI, not a host operator
typing a username/password into a browser.
"""

import html
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from backend.agent_bundle import build_agent_bundle, resolve_controller_addresses
from backend.db import create_enroll_token, get_controller_config, get_portal_credentials, log_portal_event
from backend import portal_auth, portal_files

app = FastAPI(title="Sysible Webserver Portal")

SESSION_COOKIE = "sysible_portal_session"

# Same logo used throughout the desktop client (client/branding.py) -
# duplicated here rather than imported from the client package to keep
# backend/ free of any dependency on client/, since this process can
# run on a headless server with no PySide6 installed at all.
LOGO_PATH = Path(__file__).resolve().parent.parent / "sysible_logo.png"


def _page(body: str, message: str = "", wide: bool = False) -> str:
    banner = f'<div class="alert">{message}</div>' if message else ""
    width = "680px" if wide else "380px"

    logo_tag = (
        '<img src="/static/logo.png" alt="Sysible" onerror="this.remove()">'
        if LOGO_PATH.exists() else ""
    )
    favicon_tag = (
        '<link rel="icon" type="image/png" href="/static/logo.png">'
        if LOGO_PATH.exists() else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Sysible Controller - Webserver Portal</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {favicon_tag}
  <style>
    :root {{
      --bg: #1E1E1E;
      --card: #262626;
      --card-border: #3A3A3A;
      --input-bg: #2B2B2B;
      --input-border: #505050;
      --text: #EAEAEA;
      --text-dim: #9aa5b1;
      --accent: #3C4B64;
      --accent-border: #506080;
      --accent-hover: #4C6285;
      --accent-pressed: #23395D;
      --error: #ff5c5c;
      --link: #6fa8ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 36px 16px 48px;
    }}
    .topbar {{ display: flex; align-items: center; gap: 12px; margin-bottom: 26px; }}
    .topbar img {{ height: 34px; }}
    .topbar .name {{ font-size: 16px; font-weight: 600; letter-spacing: 0.2px; line-height: 1.3; }}
    .topbar .sub {{ font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.7px; }}
    .box {{
      background: var(--card); border: 1px solid var(--card-border);
      padding: 32px 36px; border-radius: 10px; width: {width}; max-width: 92vw;
      box-shadow: 0 10px 30px rgba(0,0,0,0.4);
    }}
    .row {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    h1 {{ font-size: 19px; margin: 0 0 4px; font-weight: 600; }}
    .lede {{ color: var(--text-dim); font-size: 13px; margin: 0 0 22px; }}
    h2 {{ font-size: 12px; margin: 26px 0 10px; color: var(--text-dim);
          text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; }}
    h2:first-of-type {{ margin-top: 6px; }}
    label {{ display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 4px; }}
    input {{
      width: 100%; padding: 10px 12px; margin: 0 0 16px; font-size: 14px;
      background: var(--input-bg); color: var(--text);
      border: 1px solid var(--input-border); border-radius: 6px;
    }}
    input:focus {{ outline: none; border-color: var(--accent-border); background: #303030; }}
    input[type=file] {{ padding: 8px 0; border: none; background: none; }}
    button, .btn {{
      display: inline-block; width: 100%; padding: 11px; text-align: center;
      background: var(--accent); color: #fff; border: 1px solid var(--accent-border);
      border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;
      text-decoration: none; transition: background 0.15s ease; font-family: inherit;
    }}
    button:hover, .btn:hover {{ background: var(--accent-hover); }}
    button:active, .btn:active {{ background: var(--accent-pressed); }}
    .btn-sm {{ width: auto; padding: 7px 16px; font-size: 12px; }}
    .btn-quiet {{ background: transparent; border-color: var(--input-border); color: var(--text-dim); }}
    .btn-quiet:hover {{ background: #303030; color: var(--text); }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    li {{ display: flex; justify-content: space-between; align-items: center;
          padding: 10px 0; border-bottom: 1px solid var(--card-border); gap: 12px; }}
    li:last-child {{ border-bottom: none; }}
    li .meta {{ color: var(--text-dim); font-size: 12px; margin-left: 8px; }}
    .empty {{ color: var(--text-dim); font-size: 13px; margin: 4px 0; }}
    .alert {{
      padding: 10px 14px; margin-bottom: 18px; border-radius: 6px; font-size: 13px;
      background: rgba(255,92,92,0.12); border: 1px solid rgba(255,92,92,0.4); color: var(--error);
    }}
    .footer {{ margin-top: 24px; color: var(--text-dim); font-size: 11px; text-align: center; }}
  </style>
</head>
<body>
  <div class="topbar">
    {logo_tag}
    <div>
      <div class="name">Sysible Controller</div>
      <div class="sub">Webserver Portal</div>
    </div>
  </div>
  <div class="box">
    {banner}
    {body}
  </div>
  <div class="footer">Sysible Enterprise Software</div>
</body>
</html>"""


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


@app.get("/static/logo.png")
async def logo():
    """Unauthenticated on purpose - it's just the product mark shown on
    the login page itself, nothing sensitive. No static-file mount
    exists in this standalone portal app, so this single route stands
    in for one."""
    if not LOGO_PATH.exists():
        return Response(status_code=404)

    return FileResponse(LOGO_PATH, media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def login_page(error: str = ""):
    messages = {
        "badlogin": "Invalid username or password.",
        "notconfigured": "The portal has no login credentials configured yet - ask your admin to set them in Webserver Portal Configuration.",
        "expired": "Your session expired - please log in again.",
    }

    body = """
    <h1>Sign In</h1>
    <p class="lede">Enter your Webserver Portal credentials to continue.</p>
    <form method="post" action="/login">
      <label>Username</label>
      <input type="text" name="username" autofocus required>
      <label>Password</label>
      <input type="password" name="password" required>
      <button type="submit">Log In</button>
    </form>
    """

    return _page(body, messages.get(error, ""))


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # Logged either way (success or failure) so the Webserver Portal
    # Configuration page can show real login history, not just "is it
    # configured" - the host operator's IP is the only identifying
    # detail worth keeping here, the password obviously never is.
    ip = request.client.host if request.client else ""

    creds = get_portal_credentials()

    if not creds or not creds.get("username"):
        return RedirectResponse("/?error=notconfigured", status_code=303)

    valid = secrets.compare_digest(username, creds["username"]) and portal_auth.verify_password(
        password, creds.get("password_salt"), creds.get("password_hash")
    )

    if not valid:
        log_portal_event("login_failed", username, ip)
        return RedirectResponse("/?error=badlogin", status_code=303)

    log_portal_event("login_success", username, ip)
    token = portal_auth.create_session(ip)

    response = RedirectResponse("/files", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=True,
        max_age=portal_auth.SESSION_TTL_SECONDS,
    )

    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)

    if token:
        portal_auth.revoke_session(token)

    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)

    return response


@app.get("/files", response_class=HTMLResponse)
async def files_hub(request: Request):
    token_cookie = request.cookies.get(SESSION_COOKIE)

    if not portal_auth.validate_session(token_cookie):
        return RedirectResponse("/?error=expired", status_code=303)

    config = get_controller_config()

    bundle_section = (
        '<p><a class="btn" href="/files/bundle">Download Agent Bundle</a></p>'
        if resolve_controller_addresses(config)
        else '<p class="empty">The controller hostname/IP has not been set '
             'yet - ask your admin to configure it in Controller '
             'Configuration before an agent bundle can be built.</p>'
    )

    downloads = portal_files.list_downloads()

    if downloads:
        rows = "".join(
            f'<li><span>{html.escape(f["filename"])}'
            f'<span class="meta">{_human_size(f["size"])}</span></span>'
            f'<a class="btn btn-sm" href="/files/download/{quote(f["filename"])}">Download</a></li>'
            for f in downloads
        )
        downloads_html = f"<ul>{rows}</ul>"
    else:
        downloads_html = '<p class="empty">Nothing staged for you yet.</p>'

    body = f"""
    <div class="row">
      <h1>Files Hub</h1>
      <a class="btn btn-sm btn-quiet" href="/logout">Log out</a>
    </div>
    <p class="lede">Download your agent bundle, grab files staged for you, or send a file to the controller.</p>

    <h2>Agent Bundle</h2>
    {bundle_section}

    <h2>Files Staged For You</h2>
    {downloads_html}

    <h2>Send A File To The Controller</h2>
    <form method="post" action="/files/upload" enctype="multipart/form-data">
      <input type="file" name="file" required>
      <button type="submit">Upload</button>
    </form>
    """

    return _page(body, wide=True)


@app.get("/files/bundle")
async def download_bundle(request: Request, cli: int = 0):
    # cli=1 is set by the copy-paste curl command. For that path we return
    # real HTTP error codes instead of browser-style 303 redirects, so a
    # failure (bad login, expired session, no controller address) aborts
    # the curl `&&` chain with a clear message - rather than silently
    # saving a redirect page and letting `unzip` fail confusingly with
    # "cannot find sysible-agent-bundle.zip".
    token_cookie = request.cookies.get(SESSION_COOKIE)

    if not portal_auth.validate_session(token_cookie):
        if cli:
            return Response(
                "Not logged in (the portal login failed or the session "
                "expired). Check the username and password in the curl "
                "command and try again.\n",
                status_code=401, media_type="text/plain",
            )
        return RedirectResponse("/?error=expired", status_code=303)

    config = get_controller_config()
    addresses = resolve_controller_addresses(config)

    if not addresses:
        if cli:
            return Response(
                "The controller has no configured address, so an agent "
                "bundle can't be built. Set one in Controller "
                "Configuration in the desktop app, then retry.\n",
                status_code=409, media_type="text/plain",
            )
        return RedirectResponse("/files", status_code=303)

    enroll_token = secrets.token_hex(16)
    create_enroll_token(enroll_token)

    filename, zip_bytes = build_agent_bundle(
        addresses, config["port"], enroll_token
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/files/download/{filename}")
async def download_staged_file(filename: str, request: Request):
    token_cookie = request.cookies.get(SESSION_COOKIE)

    if not portal_auth.validate_session(token_cookie):
        return RedirectResponse("/?error=expired", status_code=303)

    try:
        path = portal_files.download_path(filename)
    except (portal_files.InvalidFilename, FileNotFoundError):
        return RedirectResponse("/files", status_code=303)

    return FileResponse(path, filename=path.name)


@app.post("/files/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    token_cookie = request.cookies.get(SESSION_COOKIE)

    if not portal_auth.validate_session(token_cookie):
        return RedirectResponse("/?error=expired", status_code=303)

    data = await file.read()
    portal_files.save_upload(file.filename, data)

    return RedirectResponse("/files", status_code=303)


@app.get("/download")
async def download_legacy(request: Request):
    """Old direct-bundle URL, kept as a redirect now that downloading
    lives inside the /files hub - avoids breaking anything that still
    links straight to /download (browser history, an old bookmark)."""
    return RedirectResponse("/files/bundle", status_code=303)


@app.get("/health")
async def health():
    """Unauthenticated - lets the GUI's portal status check confirm
    the process isn't just alive (PID exists) but actually serving."""
    return {"status": "running"}
