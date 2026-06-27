# Sysible Controller — Security Audit

Scope: `backend/` (FastAPI API + Webserver Portal), `webgui/` (BFF + React SPA),
`client/` (command builders), `host_agent/`. Method: source review of the trust
boundaries (unauthenticated → portal → agent → admin) against the supplied
vulnerability taxonomy. Branch: `dev`.

**Threat-model note that frames everything below:** an authenticated Sysible
administrator is *intended* to run arbitrary privileged commands across the
fleet (that is the product). So "command injection in a `cmd_*` builder" is not
a privilege boundary for an admin — the meaningful boundaries are
**unauthenticated → anything**, **host operator (portal) → controller**,
**agent → controller**, and **sysadmin → superuser**. Findings are judged
against those.

---

## Executive summary

Overall the codebase is in good shape: parameterized SQL throughout, no
`shell=True` on untrusted input, `shlex.quote` across the builders, no insecure
deserialization, TLS verification never disabled, React auto-escaping with a
strict CSP, `SameSite=Strict` session cookies, PBKDF2 password hashing, and the
controller API key never reaching the browser. No critical or high-severity
issue was found.

The actionable items are one **medium** (the web console leaks its API schema /
interactive docs, unauthenticated) and a handful of **low / defense-in-depth**
hardening items.

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| F1 | **Medium** | Web console exposes `/docs`, `/redoc`, `/openapi.json` unauthenticated (backend & portal disable these; the BFF does not) | `webgui/server.py:71` |
| F2 | Low | A `sysadmin` can read superuser-oriented data (admin roster, admin audit log) via the BFF | `webgui/server.py:637,698` |
| F3 | Low | Path-traversal hardening: temp path built from a raw `{filename}` param | `webgui/server.py:997` |
| F4 | Low | Terminal WebSocket has no explicit Origin check (CSWSH) — mitigated by SameSite=Strict | `webgui/server.py:1264` |
| F5 | Low | Agent sends `agent_secret` as a URL query parameter on the poll GET | `host_agent/agent.py:296` |
| F6 | Low | `X-Forwarded-For` trusted unconditionally for the login throttle (spoofable) | `webgui/server.py:211` |
| F7 | Very low | PBKDF2-HMAC-SHA256 at 200k iterations (below current OWASP guidance) | `backend/portal_auth.py:37` |
| F8 | Very low | A few endpoints accept `body: dict` (loose input typing / mass-assignment-style) | `webgui/server.py` (`create_environment`, policies) |
| F9 | Info | SSH `StrictHostKeyChecking=accept-new` (TOFU) — first-connect MITM window | `backend/remote_routes.py:508` |
| F10 | Info | Session cookie `Secure` only when `HTTPS_ONLY=1` | `webgui/server.py:96` |

---

## Findings (detail & remediation)

### F1 — Medium: Interactive API docs / OpenAPI schema exposed on the web console
`backend/app.py:118` and `backend/portal_app.py:33` deliberately create FastAPI
with `docs_url=None, redoc_url=None` (and the portal also `openapi_url=None`),
with a comment explaining the API surface shouldn't be published. The BFF does
**not**: `app = FastAPI(title="Sysible Web GUI")` (`webgui/server.py:71`). Those
auto-registered routes are matched before the SPA catch-all, and they carry no
auth dependency, so anyone who can reach `https://<controller>:8800/openapi.json`
(or `/docs`, `/redoc`) gets a full map of the API — without logging in. The
individual endpoints still require the session cookie, so this is information
disclosure / improper asset management, not direct access — but it hands an
attacker the whole attack surface for free and is inconsistent with the other
two apps.

**Fix:**
```python
app = FastAPI(title="Sysible Web GUI", docs_url=None, redoc_url=None, openapi_url=None)
```

### F2 — Low: `sysadmin` can read superuser-only data via the BFF
The authorization model is otherwise sound: state-changing, superuser-gated
calls go through `_as_admin()`, which attaches the caller's own encrypted token
so the controller enforces `require_superuser` server-side (a sysadmin's token
is rejected). But a few **read** endpoints are gated only by `require_login` and
call the controller without the token, and the matching controller routes are
`require_api_key`-only (no role check):

- `GET /api/admins` → `api.list_administrators()` (`webgui/server.py:637`) — exposes the administrator roster + roles (no hashes).
- `GET /api/audit-log` → `api.get_admin_audit_log()` (`webgui/server.py:698`) — login attempts and admin-account changes.

In the desktop model these live behind the Settings (superuser) surface. A
sysadmin shouldn't necessarily see the admin roster or the admin audit log.

**Fix:** route these through `_as_admin()` and add `require_superuser` to the
corresponding controller routes (`/admin/administrators` GET, `/admin/audit-log`).

### F3 — Low: Path-traversal hardening on portal-upload download
`webgui/server.py:997` builds `dest = tmp / filename` from the `{filename}` path
parameter before calling `api.download_portal_upload(filename, str(dest))`.
Today this is contained — FastAPI's `{filename}` is a single path segment (a
literal `/` won't route here) and the controller validates the *source* name via
`portal_files.safe_filename`. But the BFF is writing to a path it derived from
untrusted input without normalizing it itself.

**Fix:** `dest = tmp / Path(filename).name` (or reuse a `safe_filename` helper)
so the BFF never trusts the routed value for a filesystem write.

### F4 — Low: WebSocket lacks an explicit Origin check (CSWSH)
`terminal_ws` (`webgui/server.py:1264`) authenticates via the session cookie
(good — no unauthenticated terminals) but does not validate the `Origin` header.
The classic Cross-Site WebSocket Hijacking defense is an Origin allowlist. Here
it's mitigated because the session cookie is `SameSite=Strict`, so a handshake
initiated from another origin won't carry the cookie and is closed with 1008.

**Fix (defense-in-depth):** reject the upgrade unless
`ws.headers.get("origin")` matches the console's own scheme+host.

### F5 — Low: Agent secret in a URL query string
On the task-poll GET the agent passes its credential as
`params={"agent_secret": ...}` (`host_agent/agent.py:296`). Over TLS it's
encrypted in transit, but query strings are the part of a URL most likely to be
written to access logs / proxy logs in cleartext. POST paths already put it in
the JSON body.

**Fix:** send `agent_secret` in a request header (e.g. `X-Agent-Secret`) or the
body rather than the query string.

### F6 — Low: `X-Forwarded-For` trusted unconditionally for throttling
`_client_ip` (`webgui/server.py:211`) reads the first `X-Forwarded-For` value.
If the console is reachable directly (no trusted proxy), a client can spoof XFF
to dodge the per-IP login throttle. The code comments acknowledge this and note
the throttle is "best-effort hardening, not an access control," which is the
right framing.

**Fix:** only honor XFF when a `SYSIBLE_WEBGUI_TRUSTED_PROXY` is configured;
otherwise use the socket peer. At minimum, document that the throttle assumes a
trusted proxy.

### F7 — Very low: PBKDF2 iteration count
`PBKDF2_ITERATIONS = 200_000` (`backend/portal_auth.py:37`). PBKDF2-HMAC-SHA256
with a 16-byte random salt and constant-time compare is solid, but current OWASP
guidance for PBKDF2-SHA256 is ≥600,000 iterations.

**Fix:** raise to ~600k, or migrate to Argon2id/scrypt (re-hash on next login).

### F8 — Very low: Loose request typing on a few endpoints
`create_environment`, `set_policy`, and `set_env_policy` accept `body: dict`
and forward it to the controller. The controller validates, so this isn't
exploitable today, but typed Pydantic models would prevent unexpected-key /
mass-assignment-style surprises and document the contract.

### F9 — Info: SSH TOFU host-key policy
`StrictHostKeyChecking=accept-new` (`backend/remote_routes.py:508`) trusts a
host key on first contact — a standard convenience tradeoff, but it leaves a
first-connection MITM window for SSH-managed hosts. Worth a line in `SECURITY.md`.

### F10 — Info: Cookie `Secure` flag is conditional
The session cookie is marked `Secure` only when `SYSIBLE_WEBGUI_HTTPS_ONLY=1`
(`webgui/server.py:96`). The installer enables TLS by default and sets this, so
production is fine; just don't run the console over plain HTTP.

---

## Taxonomy coverage

### Authentication & Authorization
- **Broken/Missing auth, Missing authz checks** — Every `/api/*` route except
  `/api/login` requires a valid session (`require_login`); the WebSocket checks
  it too. Superuser actions are enforced **server-side on the controller** via
  the caller's token (`_as_admin`), not just in the UI. Sound. Exceptions: F2.
- **Privilege escalation (vertical/horizontal), Privilege confusion, BFLA/BOLA,
  IDOR** — Run-as identity is derived from the signed login token on the
  controller, not from client input, so a client can't act as another admin or
  escalate. Admin password reset is `require_superuser` server-side. No
  user-supplied object IDs bypass ownership checks. Minor read-side exposure: F2.
- **Weak password policy** — Configurable admin password policy enforced
  server-side at set time. OK. (Optionally raise PBKDF2 cost — F7.)
- **Brute force / Credential stuffing / Password spraying** — Per-IP login
  throttle on the web console (HTTP 429), plus separate throttles on the admin
  and portal logins. XFF caveat: F6.
- **Session fixation** — Login rotates the session. OK.
- **Session hijacking / prediction** — Cookie is signed (itsdangerous),
  `HttpOnly`, `SameSite=Strict`, `Secure` under TLS; the admin token inside is
  Fernet-encrypted with a server-side key and never sent to the browser. Session
  IDs/tokens come from `secrets`. OK.
- **Improper logout** — Logout clears the session server-side; sessions also
  expire (`max_age`). OK.
- **JWT (all), OAuth, MFA** — **N/A**: no JWTs, no OAuth. MFA is not implemented
  (acceptable for a LAN/VPN admin tool; could be a future enhancement).

### Injection
- **SQL (all variants)** — All `db.py` queries are parameterized; the one
  dynamic fragment (`db.py:1624`) is the safe `?`-placeholder IN-clause with
  values bound separately. **Not vulnerable.**
- **Command / CRLF / Log** — No `shell=True` on untrusted input; local
  subprocess calls are list-form with fixed args (`ssh-keygen`, `journalctl`,
  `ssh`). The fleet command builders use `shlex.quote`. The agent runs
  controller-issued commands with `shell=True` **by design** (controller is the
  trust root; commands are gated by admin auth + per-host secret).
- **NoSQL / LDAP / XPath / XML / GraphQL / SSTI / EL / SMTP / Template** —
  **N/A or not present**: SQLite only; no template engine renders untrusted
  input (portal HTML uses `html.escape`); no XML parsing of untrusted data; no
  GraphQL; no mail composed from user headers.
- **HTML/CSS injection** — Portal server-rendered HTML escapes filenames
  (`html.escape`) and URL-encodes hrefs (`quote`). React escapes by default.

### Cross-Site
- **XSS (reflected/stored/DOM)** — React with no `dangerouslySetInnerHTML`,
  `innerHTML`, `eval`, or `document.write`; strict CSP (`default-src 'self'`,
  no `script-src 'unsafe-inline'`). Terminal output renders inside xterm.js (not
  HTML). Low risk.
- **CSRF** — `SameSite=Strict` cookie closes CSRF on state-changing POSTs.
- **Clickjacking / UI redressing** — `X-Frame-Options: DENY` +
  `frame-ancestors 'none'`.
- **CORS** — No CORS configured; same-origin only.

### File
- **Path/Directory traversal, LFI/RFI, Zip Slip, Symlink** — Portal file pool
  uses `safe_filename` (strips directories, rejects `.`/`..`); SPA catch-all
  always returns `index.html` (never reflects the path to the filesystem);
  static assets via Starlette `StaticFiles` (traversal-safe). Agent bundle zips
  are built by the controller, not from attacker input. Hardening item: F3.
- **Arbitrary upload / unsafe download / overwrite** — Uploads are saved under
  fixed pool dirs with collision-suffixing (no overwrite); downloads resolve
  through `safe_filename`. OK.

### Server-Side
- **SSRF** — No endpoint fetches an attacker-controlled URL; outbound calls go
  to the configured controller or admin-specified hosts. Low.
- **XXE / Insecure deserialization / Unsafe reflection** — **Not present**: no
  XML parsing of untrusted input, no `pickle`/`yaml.load`/`marshal`, no
  reflective dispatch on user input.
- **Prototype pollution** — **N/A** (no untrusted object-merge in the SPA;
  React state only).
- **Race conditions** — The process-global admin token on `client.api` is
  serialized under `_ADMIN_TOKEN_LOCK`, so concurrent requests can't clobber
  each other's identity. Become-password is keyed per task with a short TTL.

### API Security
- **Excessive data exposure / Improper asset management** — F1, F2.
- **Mass assignment** — F8 (loose `dict` bodies; controller still validates).
- **Lack of rate limiting** — Login is throttled; authenticated action
  endpoints are not separately rate-limited (low concern — authenticated admin).
- **GraphQL / gRPC** — **N/A**.

### Session / Cookies
- Signed, `HttpOnly`, `SameSite=Strict`, `Secure` under TLS; secret persisted;
  token encrypted inside. Predictable IDs / token replay: not applicable (random
  `secrets`, expiring sessions). See F10 for the conditional `Secure` flag.

### Information Disclosure
- **Debug interfaces / API docs / version** — F1. Backend & portal disable
  docs; the backend keeps `openapi.json` intentionally. Stack traces are wrapped
  (`_wrap` returns clean 502s; errors aren't echoed verbatim to the browser).
- **Directory listing / backup files / source disclosure** — No directory
  listing; static serving is scoped to `dist/assets`. OK.

### Cryptographic
- **Weak TLS / ciphers / deprecated versions** — TLS termination is uvicorn or a
  fronting proxy; uses the system OpenSSL defaults. Recommend pinning a modern
  minimum (TLS 1.2+) at the proxy if one is used.
- **Hardcoded keys/secrets** — None found; API key and Fernet keys are generated
  at install and stored `0600`. Session secret from env or `secrets`.
- **Poor RNG** — Uses `secrets` for tokens/keys (not `random`).
- **Insecure password storage / weak hashing** — PBKDF2-HMAC-SHA256 + per-cred
  salt + constant-time compare. Solid; F7 is an optional cost bump.
- **Improper certificate validation** — TLS verification is never disabled
  (no `verify=False` / `CERT_NONE`); the agent and clients pin the controller
  cert.

### Business Logic
- E-commerce-style items (price/coupon/inventory/quantity) — **N/A**.
- **Replay / race / workflow bypass** — Single-use enrollment tokens prevent
  bundle replay; become-password TTL limits replay; token lock prevents identity
  races (above).

### Cloud & Infrastructure
- Buckets / IAM / metadata service / k8s / container escape — **N/A** (self-
  hosted single-box systemd deployment). "Secrets in images / open management
  interfaces": the **management interface exposure** point is real and is the
  single most important control — firewall ports 9000 / 8800 / portal to a
  trusted subnet or VPN (see `SECURITY.md`).

### Client-Side / SPA
- DOM XSS / prototype pollution / local-storage token theft — The SPA stores
  **no token in localStorage** (auth is the http-only cookie; the admin token
  never reaches the browser). `localStorage` is used only for UI prefs (theme,
  filters). Tabnabbing: no untrusted `target=_blank` links to attacker origins.

### HTTP Security Headers
- HSTS (under TLS), `X-Frame-Options`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, and a restrictive CSP are all set. `style-src
  'unsafe-inline'` is required by xterm.js — acceptable since `script-src` is
  not relaxed.
- **Request smuggling / response splitting / desync / cache poisoning** —
  Handled by uvicorn/Starlette; no manual header reflection of untrusted input
  found. Host-header use is limited; no open redirect (`form-action 'self'`,
  no user-controlled redirects).

### WebSocket
- Authenticated via session; same-origin by SameSite. Add explicit Origin check
  (F4). No unauthenticated subscription or message-injection path (frames are
  validated; the first frame must be a well-formed `open`).

### Configuration
- **Default credentials** — The installer seeds `admin` with a **random**
  one-time password (not a static `admin/admin`), `must_change_password=1`,
  printed once. Good practice, not the default-creds anti-pattern.
- **Debug mode / exposed admin panel / open redirect / host-header injection** —
  Debug not enabled; the admin panel is the product (protect at the network
  layer); no open redirect or host-header trust found. F1 is the doc-exposure
  exception.

### Supply Chain
- SPA deps are pinned in `package.json` (xterm, React, Vite). Recommend
  committing a lockfile (`package-lock.json`) and enabling Dependabot/`npm
  audit` in CI to cover dependency confusion / malicious-package risk. Python
  deps are pinned in `requirements.txt`.

### Emerging / LLM
- **N/A** — the product integrates no LLM, RAG, or model API.

---

## Recommended order of action
1. **F1** — one-line fix, removes an unauthenticated information leak.
2. **F2** — close the sysadmin read paths to admin roster / audit log.
3. **F3, F4, F5** — small hardening (basename the temp path, Origin check,
   move the agent secret out of the query string).
4. **F6, F7, F8** — proxy-aware throttle, PBKDF2 cost, typed request bodies.
5. Document **F9/F10** and the network-exposure expectation in `SECURITY.md`
   (the firewall point is already covered there).
