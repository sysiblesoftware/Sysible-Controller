# Sysible Web GUI

A browser-based front end to the Sysible Controller, for Windows (and
any) machines that can't run the PySide6 desktop client but can reach
the controller over the network.

It is a **separate service** from the controller and the desktop app. It
runs as its own process on its own port (`sysible-webgui`, HTTPS on 8800
by default), reuses the desktop client's existing Python logic, and
serves a React single-page app. It has **full feature parity** with the
desktop GUI — all 18 tools, every action, the live terminals, file
transfer, the superuser activity feed, and the dark/light theme toggle.

On a normal install you don't run any of the commands below by hand: the
installer (`install_sysible.sh`) builds the front end, installs the
`sysible-webgui` systemd service, and seeds a default web-console
administrator. The sections below are for understanding, hand-running, or
developing the service. To operate it day to day, use the controller CLI:

```bash
sudo sysible_controller webgui start     # start | stop | status | logs
sudo sysible_controller reset-admin       # set/reset a web-console login password
```

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

The simplest path on a headless controller is the launcher at the repo root,
which builds the front end on first run, generates and reuses a stable cookie
secret (`run/webgui.secret`, mode 0600), and turns on TLS automatically if the
controller has a cert:

```bash
./start_webgui.sh            # port 8800 by default
./start_webgui.sh 9443       # or pick a port
```

Or run it by hand:

```bash
cd webgui
pip install -r requirements.txt        # plus the controller's own deps
export SYSIBLE_WEBGUI_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
uvicorn server:app --host 0.0.0.0 --port 8800
```

Open `https://<this-host>:8800/` and sign in with a controller administrator
account — the **same accounts** the desktop app uses (administrators are stored
once on the controller and shared by both front ends).

On a fresh install the installer seeds a default superuser named `admin` and
prints its one-time password, in red, at the end of the install output. Log in
with that, then change it under **Settings → My Account**. If administrators
already existed (so no default was seeded) or a password is lost, run
`sudo sysible_controller reset-admin [username] [password]` — it prints a fresh
password the same way and flags the account to change it at next login. A
superuser can also reset any other administrator's password from
**Settings → Administrators** without knowing the old one.

### Identity & run-as (same model as the desktop)

Signing in issues a signed login token; the BFF encrypts that token into the
session cookie with a server-side key, so it survives a service restart, is
never stored in the clear, and is never echoed back to the browser. Every
action and terminal the browser runs is dispatched **as the administrator who
is signed in** — the controller derives the run-as Linux user from the token
(`runuser -u <admin>` on agent hosts), exactly as the desktop client does, so
the host's own sudo policy and audit trail stay authoritative and the activity
feed attributes each action to the right person. Password ("become") sudo is
supported: each administrator stores their own sudo password (encrypted at rest
on the controller) from the header's **Sudo Password** button, and it's fed to
`sudo -S` over stdin only.

### Environment variables

| Variable                       | Purpose                                              |
|--------------------------------|------------------------------------------------------|
| `SYSIBLE_WEBGUI_SECRET`        | Stable secret for signing the session cookie. Set this in production — the random fallback logs everyone out on restart. (`start_webgui.sh` and `webgui_manager` persist one for you.) |
| `SYSIBLE_WEBGUI_HTTPS_ONLY`    | `1` to mark the session cookie Secure + send HSTS (set automatically when TLS is on). |
| `SYSIBLE_WEBGUI_SESSION_MAX_AGE` | Session lifetime in seconds before re-login (default 43200 = 12h). |
| `SYSIBLE_WEBGUI_LOGIN_MAX_ATTEMPTS` | Failed logins per IP before a temporary lockout (default 8). |
| `SYSIBLE_WEBGUI_LOGIN_WINDOW`  | Lockout/counting window in seconds (default 300). |
| `SYSIBLE_WEBGUI_TASK_TIMEOUT`  | Seconds to wait for an agent task result (default 60). |
| `SYSIBLE_WEBGUI_NOBUILD`       | `1` to skip the front-end build on start (serve the existing `dist/` as-is). |
| `SYSIBLE_API_BASE_URL`, `SYSIBLE_API_KEY`, `SYSIBLE_CA_CERT` | Controller connection — read by `client.api`, same as the desktop app. |

## Updating / redeploying

`start_webgui.sh` rebuilds the front end whenever the source (`frontend/src`,
`index.html`, `package.json`, `vite.config.js`) is **newer than the built
bundle** — not only when `dist/` is missing — so after pulling new code a plain
restart picks up the change:

```bash
git pull
sudo sysible_controller webgui stop && sudo sysible_controller webgui start
```

Re-running `sudo ./install_sysible.sh` also rsyncs the new source to
`/opt/sysible` and rebuilds. Set `SYSIBLE_WEBGUI_NOBUILD=1` to serve the
existing `dist/` without rebuilding (e.g. when Node isn't available at run
time and you've committed a prebuilt bundle). Asset filenames are content-
hashed, so once the new bundle is served a normal reload picks it up; only
`index.html` may need a hard refresh.

## Security posture (network exposure)

This console is built to be reachable over a network, so it ships hardened by
default:

- **Session cookie** — signed, http-only, `SameSite=Strict` (closes CSRF on
  the state-changing `POST`s without a separate token), marked `Secure` under
  TLS, and expiring after `SESSION_MAX_AGE`. The signing secret is persisted
  so restarts don't invalidate everyone's session.
- **Login throttle** — per-IP attempt limit with a cooldown (HTTP 429) to slow
  password guessing. Successful login clears the counter and rotates the
  session (session-fixation hardening).
- **Security headers** — `Content-Security-Policy` (same-origin only),
  `X-Frame-Options: DENY` / `frame-ancestors 'none'` (anti-clickjacking),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and HSTS
  when served over TLS.
- The controller **API key never reaches the browser** — the BFF holds it and
  the SPA only ever sees `{action, params, targets}`.

## Production / TLS

Either let the launcher use the controller's own cert (it sets
`SYSIBLE_WEBGUI_HTTPS_ONLY=1` and passes the cert to uvicorn), or run behind a
TLS-terminating reverse proxy (nginx/Caddy) and set `SYSIBLE_WEBGUI_HTTPS_ONLY=1`
yourself. Either way the service serves both the SPA and `/api/*` on one
origin, so no CORS configuration is needed. If you front it with a proxy, make
sure it forwards `X-Forwarded-For` (so the login throttle sees real client IPs)
and upgrades websockets (for the terminal). During front-end development,
`npm run dev` (port 5173) proxies `/api` to `http://localhost:8800` so cookies
work without CORS.

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

**Full parity: all 18 desktop tiles, 323 actions, every `cmd_*` builder
wired (100%).** Each action maps to the existing `cmd_*` builder, so the
web action runs the identical shell command the desktop tool would.
Highlights:

- **Run Command**, **Service Management**, **User & Group Administration**
- **Host Software Management** (install/update/remove/search/query/clean)
- **Repository Management**, **Cron & Systemd Timers**
- **Network Management** (ip/devices/routes/ping/traceroute/dns/mtu/routes)
- **Storage Administration** (disks/partitions/SMART/LVM/RAID/swap/format,
  plus install buttons for smartmontools/LVM/mdadm)
- **Firewall Administration** (firewalld/nft/iptables, list ALL ports,
  install firewalld/ufw)
- **Security Administration** (SELinux/sshd/audit/updates/hardening/
  rkhunter/lynis)
- **File System Management** (dir/copy/move/chmod/chown/archive/fstab/
  NFS+CIFS mount)
- **System Health, Logs & Recovery** (health/disk/mem-cpu/logs/kernel/
  boot/kernels/support bundle)
- **Time Synchronization**, **Certificate Management**, **Containers & VMs**
- **Directory Services** (AD join/leave, realm/Kerberos status)
- **Backup & Recovery**, **Distro Subscription & Licensing** (RHSM/Pro/SCC)

This includes the deeper actions too: multi-field static-IP / bond /
team / VLAN / bridge, custom systemd unit + timer creation, full LVM /
RAID / swap lifecycle, SELinux fcontext + boolean management, sshd
hardening + authorized-key management, fstab + quota management, and the
RHSM / Ubuntu Pro / SUSE subscription lifecycles.

Builders validate their own input, so bad parameters return a clean 400
to the browser rather than dispatching a malformed command.

> Note: one builder name (`cmd_set_password_aging`) exists in two
> modules — a per-user version and a host-default version. The web
> service calls each from its specific module so both the User & Group
> "Set password aging" and the Security "Set default password aging"
> actions are correct.

## Sysible Connect (browser terminal)

The **Sysible Connect** dashboard tile opens a live SSH terminal in the
browser (xterm.js). The BFF exposes a `/api/terminal/ws` websocket that
bridges the controller's poll-based SSH PTY API to a stream: a background
task polls the controller for output and pushes it to the browser, while
keystrokes and resizes flow the other way. It's gated by the same login
session, so the controller API key never reaches the browser. Terminals
are SSH-based, so the host picker lists SSH and agent+SSH (merged) hosts.

The dev proxy forwards websockets; behind a reverse proxy, make sure
websocket upgrade headers are passed through for `/api/terminal/ws`
(nginx: `proxy_set_header Upgrade $http_upgrade; proxy_set_header
Connection "upgrade";`).

## File transfer

The **File Transfer** dashboard tile uploads a local file to a host path
or downloads a file from a host, reusing the desktop client's SSH
transfer. Uploads are spooled to a server-side temp file and pushed with
`upload_file_ssh`; downloads are fetched with `download_file_ssh` and
streamed back to the browser as an attachment (temp files are cleaned up
either way). SSH-based, so the host picker lists SSH and agent+SSH hosts.

## Parity status

The browser GUI now covers the full desktop surface: all 18 tool tiles
(every `cmd_*` builder), the Sysible Connect terminal, and file transfer.
