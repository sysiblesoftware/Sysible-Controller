# Sysible Web GUI

A browser-based front end to the Sysible Controller, for Windows (and
any) machines that can't run the PySide6 desktop client but can reach
the controller over the network.

It is a **separate service** from the controller and the desktop app. It
runs as its own process on its own port, reuses the desktop client's
existing Python logic, and serves a React single-page app.

---

## Architecture

```
 Browser (React SPA)
        │  same-origin fetch, http-only session cookie
        ▼
 webgui/server.py  ── BFF (FastAPI) ──────────────────────────────┐
        │  imports & reuses, never reimplements:                   │
        │    client.api            (admin login, agents, edition)  │
        │    client._api_dispatch  (agent-queue vs SSH dispatch)   │
        │    client._api_* (cmd_*) (shell-command builders)        │
        │  holds the controller API key server-side                │
        ▼                                                          │
 Sysible Controller API  ◄─────────────────────────────────────────┘
```

Why a BFF rather than a JS rewrite: the desktop tools are hundreds of
pure-Python `cmd_*` functions that build exact shell strings, plus a
dispatch layer that hides "agent task queue vs. synchronous SSH exec."
The browser sends `{action, params, targets}`; the server builds the
**same** command the desktop would and dispatches it. The two front ends
stay in lockstep, and the controller API key never reaches the browser.

---

## Prerequisites

- The Sysible Controller running and reachable.
- This service runs in (or alongside) the controller's Python
  environment so `import client.*` works and the controller API key +
  base URL are available the same way the desktop app reads them:
  - `SYSIBLE_API_BASE_URL` (e.g. `https://controller.local:8000`)
  - `SYSIBLE_API_KEY` **or** the on-disk key file the desktop client uses
  - `SYSIBLE_CA_CERT` if the controller uses a pinned TLS cert
- Node.js 18+ to build the front end (build-time only; not needed at run
  time once `dist/` exists).

## Build the front end

```bash
cd webgui/frontend
npm install
npm run build        # outputs webgui/frontend/dist/
```

## Run the service

```bash
cd webgui
pip install -r requirements.txt
# also ensure the controller's own deps are importable (run in its venv
# or: pip install -r ../requirements.txt)

export SYSIBLE_WEBGUI_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
uvicorn server:app --host 0.0.0.0 --port 8800
```

Open `http://<this-host>:8800/` and sign in with a controller
administrator account (the same credentials the desktop app uses).

### Environment variables

| Variable                       | Purpose                                              |
|--------------------------------|------------------------------------------------------|
| `SYSIBLE_WEBGUI_SECRET`        | Stable secret for signing the session cookie. Set this in production — the random fallback logs everyone out on restart. |
| `SYSIBLE_WEBGUI_HTTPS_ONLY`    | `1` to mark the session cookie Secure (set when behind TLS). |
| `SYSIBLE_WEBGUI_TASK_TIMEOUT`  | Seconds to wait for an agent task result (default 60). |
| `SYSIBLE_API_BASE_URL`, `SYSIBLE_API_KEY`, `SYSIBLE_CA_CERT` | Controller connection — read by `client.api`, same as the desktop app. |

## Production / TLS

Run the service behind a TLS-terminating reverse proxy (nginx/Caddy) and
set `SYSIBLE_WEBGUI_HTTPS_ONLY=1`. The service serves both the SPA and
`/api/*` on one origin, so no CORS configuration is needed. During
front-end development, `npm run dev` (port 5173) proxies `/api` to
`http://localhost:8800` so cookies work without CORS.

---

## Extending toward full desktop parity

All tool behavior lives in **`webgui/actions.py`**. To expose another
desktop action in the browser, register an `Action` that points at the
`cmd_*` builder that already exists in `client/_api_*.py`:

```python
_register(Action(
    name="net_show_ip",
    tool="Network Management",
    label="Show IP configuration",
    params=[],
    build=lambda p: api.cmd_show_ip_config(),
))
```

That's the whole change — no new endpoint, no new React component. The
dashboard tile for that tool lights up automatically (it's disabled
until its tool has at least one registered action), and the tool page
renders the form from the action's `params`.

Param types the form supports: `text`, `password`, `number`, `select`
(with `options=[...]`), `checkbox`.

### Current coverage

Seeded as a representative vertical slice that proves the pattern
end-to-end:

- **Run Command** — arbitrary shell across selected hosts.
- **Service Management** — list / list running / status / start / stop /
  restart.
- **User & Group Administration** — create / delete / lock / unlock /
  set shell / list groups & members.

The remaining desktop tiles appear on the dashboard as "soon" and are
the same shape to fill in.

## Not yet included

- **Sysible Connect terminal** (browser SSH/agent shells) — deferred; it
  needs an xterm.js + websocket PTY bridge and is a phase of its own.
- File upload/download tools (the `python-multipart` dep is already
  listed for when they land).
