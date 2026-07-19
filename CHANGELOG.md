# Changelog

All notable changes to the Sysible Controller are recorded here.

## Unreleased

### Added — enrollment source-IP allowlist (Settings → Enrollment Access)

A console-managed allowlist that restricts which source networks may enroll a NEW host on
`POST /agents/enroll`. The one-time enrollment token is still required on top — the
allowlist narrows *where* a valid token may be presented from, so a leaked-but-unused
bundle can't enroll a rogue host from an off-subnet source. Managed from a new
**Settings → Enrollment Access** tab (add/remove CIDRs with notes; audited).

- **Empty == allow all** (backward compatible); a non-empty list restricts enrollment to
  the listed CIDRs. **Loopback is always allowed** (controller self-enroll / the BFF).
- Accepts IPv4/IPv6 CIDRs or a bare IP (stored as /32 or /128). An unparseable source with
  a non-empty allowlist **fails closed** (denied).
- Enforced at the socket-peer IP (`request.client.host` — not a spoofable
  `X-Forwarded-For`). For behind-a-bastion hosts this is the relay/tunnel peer, which is
  what you allow. **Only the open, token-gated enroll path is gated** — steady-state agent
  traffic (heartbeat/tasks/results, authenticated by the agent secret) is unaffected, so a
  wrong CIDR can't lock out the existing fleet.
- New `GET/POST/DELETE /admin/enroll-allowlist` (superuser); a denied enroll returns 403
  "Enrollment from this network is not permitted."

### Added — `sysible_controller disenroll` (force-remove a host from the controller side)

A new CLI subcommand that force-removes an enrolled host from the controller itself —
the operator escape hatch for when the normal (agent-initiated or console) disenroll
can't complete: a **stale pinned TLS cert** (the agent can't verify the controller after
a cert regen / reinstall / new-IP reissue), an offline or zombie agent, or a graceful
disenroll that keeps re-enrolling.

- Talks to the controller's own API over loopback using the **live serving cert**, so it
  works even when every agent's pinned cert has drifted (the exact failure that leaves a
  host stuck in the roster).
- Resolves the target by `--self` (the controller's own managed-host record, via the
  `is_controller` flag), `--name`, `--ip`, or an exact `host_id`; refuses ambiguous
  matches and asks for `--host-id`.
- Defaults to a **FORCE** removal (purges the enrollment token so a still-running agent
  can't re-enroll — the reason a graceful disenroll appears to "not work"); `--keep-token`
  opts out. `--dry-run` previews; `-y` skips the prompt.
- Falls back to a direct database removal (`tools/unenroll_agent.py`) if the controller
  API is unreachable. For `--self` it also stops the local `sysible-agent` afterward.

Also: the bundle's `disenroll_agent.sh` now detects when the controller notification
didn't get through and prints a follow-up telling the operator to finish the cleanup with
`sysible_controller disenroll` on the controller — so a remote host whose notify failed
(drifted cert / moved controller) no longer silently leaves an orphaned row.

### Added — console health-warning banner ("agents can't check in")

A superuser-only warning banner across every console view that catches the outage class
where the fleet silently stops checking in, backed by a cheap read-only
`GET /admin/health-warnings` endpoint (defensive — each detector is independently wrapped
and degrades to "no warning" rather than a false alarm):

- **Stale / mismatched pinned TLS certificate** — the classic "agents stopped checking in
  after a controller cert regen / reinstall / new-IP reissue." The controller compares the
  cert it now hands out for pinning (`trust.crt`, falling back to the serving leaf) against
  the cert its own loopback agent has pinned locally (`SYSIBLE_CA_CERT`, default
  `/etc/sysible/controller.crt`); if the SHA-256 fingerprints diverge, every agent pinning
  the old cert will fail TLS verification until re-deployed — so the banner says so, with
  the fix (refresh the pinned cert / re-enroll).
- **Mass host silence** — a high fraction of enrolled hosts going stale at once (the
  fleet-wide symptom of cert drift, a moved controller address, or the controller being
  down), distinct from a single host powered off.

Banners are dismissible for the session; the console polls on load and every 60 s.

### Security
- **Package management: option-injection → root RCE closed.** A package field like
  `nginx -o DPkg::Pre-Invoke::=<cmd>` was parsed by apt/dnf as an OPTION rather than a
  package name (arbitrary command as root). Package tokens beginning with `-` are now
  rejected, and install/remove/update place a `--` end-of-options separator before the
  operands.
- **Repository URL CRLF injection closed.** Adding a repository now rejects CR/LF in the
  URL/source line, so it can no longer write a second `[trusted=yes]` apt source into
  `sources.list.d`.
- **Onboarding-portal bind address is configurable.** The self-service portal (default
  :8090) that serves agent bundles was hardcoded to bind `0.0.0.0`; it now honours
  `SYSIBLE_PORTAL_HOST` (default `0.0.0.0`, unchanged) so a multi-homed / segmented
  controller can pin it to a management interface or to `127.0.0.1` behind a reverse
  proxy. The portal stays TLS + login-gated with single-use, host-capped bundle tokens —
  this narrows the network attack surface (it doesn't close an auth hole; every
  bundle/file route is already authenticated).

### Fixed
- **Agent: exponential reconnect backoff.** When the controller is unreachable (it was
  renumbered onto a new IP, a firewall/network change cut the host off, or the controller
  service is down), the agent's poll and heartbeat loops now back off from
  `SYSIBLE_POLL_INTERVAL` up to `SYSIBLE_CONN_BACKOFF_MAX` (60s) instead of retrying every
  ~1.5s forever — much less log spam and CPU/network churn during an outage — and snap
  back to the normal cadence the instant a request succeeds.
- **Request-body size cap** on the controller API (default 16 MiB,
  `SYSIBLE_MAX_REQUEST_BYTES`), so an unbounded agent heartbeat/result body can't be
  buffered into memory to exhaust the controller.
- **Enroll flood guard**: a per-source-IP rate limit on `/agents/enroll` (default
  240/60s, `SYSIBLE_ENROLL_RATE_MAX`) sheds an enrollment storm before it takes the enroll
  lock. Generous by default so legitimate mass rollout (even behind one NAT) is
  unaffected; set to 0 to disable.
- **Identifier validators reject a leading `-`** (usernames, nmcli connection names) — the
  option-injection shape — matching the package-name validator. Benign (these tools have
  no command-executing option, so it was never RCE), kept for consistency.

### Added
- **Headless-install curl one-liner: address the controller by IP.** When the console is
  configured with a hostname but a target host has no DNS for it, the curl command failed
  to resolve. The "Enroll a Host" tab now offers a checkbox to build the command against
  the controller's IP instead of its hostname (curl already uses `-k`, so the self-signed
  cert not covering the IP is fine). Shown whenever both a hostname and an IP are known.

### Fixed
- **"Check for updates" now shows the real git error.** When the update check couldn't
  reach the remote it reported a generic "git ls-remote failed (network or auth)". It now
  includes git's actual stderr (auth prompt, unknown host, TLS/proxy, permission denied),
  so you can see and fix why the check failed.
- **`destroy` now removes this machine's own self-enrolled agent.** The controller enrolls
  itself as a managed host (`/opt/sysible-agent`); `destroy` tore down the backend/console
  but left that local agent, which then crash-looped under systemd Restart=always trying to
  reach the deleted controller (repeated "409 A live agent is already enrolled…"). Destroy
  now stops, disables, and removes the local `sysible-agent` service and directory. Other
  hosts' agents are still untouched.
- **Install: default admin seeding is fail-soft and honest.** The installer's default
  superuser seed now runs as one Python call that catches DB errors (printing an
  explicit warning instead of being silently misreported as "administrators already
  exist"), retries while the datastore comes up, and generates the first-login password
  with a DB-free fallback. On SQLite this is belt-and-suspenders; it matters most on
  Enterprise/Postgres, where a not-yet-ready or unreachable database could otherwise
  leave a fresh install with no console login and no password shown.

### Security — agent re-enrollment hardening

Closes an offline-host identity-takeover gap found by an independent audit of the
`POST /agents/enroll` path. Live-host and revoked-host protections were already sound;
these changes extend the same rigor to the offline case and to token single-use.

- **Re-binding an existing host now requires authorization.** A bearer enrollment token
  alone can no longer overwrite an already-enrolled host's agent secret — closing the
  path where a leaked/replayed token plus a host's (machine-derivable, non-secret)
  `host_id` or spoofed hostname+IP could seize an *offline* host and lock out the real
  agent. Re-enrolling an existing record now requires one of: the current `agent_secret`
  (the agent presents it automatically when it still holds saved state), or an
  admin-issued **reissue token** bound to that one host. Gated by
  `SYSIBLE_ENROLL_REQUIRE_REBIND_AUTH` (default on). A clean disenroll deletes the record,
  so the normal disenroll → re-enroll lifecycle is unchanged; only an *unclean* wipe
  (record still present) now needs a reissue.
- **Reissue enrollment (console + API).** New per-host **Reissue** action mints a
  single-use, host-bound token to reclaim exactly that record after a reinstall.
  `POST /admin/enroll-token/reissue` (superuser + localhost, audited).
- **Enrollment tokens are single-use at the database.** `consume_enroll_token` now
  claims the token with a conditional `UPDATE … WHERE token=? AND (used=0 OR bound_host_id=?)`
  and checks the row count, so two requests racing the same token can't both enroll a
  host — it no longer relies solely on a process-local lock (mirrors the relay-token path).
- **Community enrollment tokens are hashed at rest** (SHA-256), matching Enterprise and
  the console/admin token storage — a leaked Community DB/backup no longer yields
  directly replayable enroll tokens. Existing unused tokens must be re-minted after upgrade.
- **Shorter, tunable token reuse window** — default 7 days → 24h, via
  `SYSIBLE_ENROLL_TOKEN_REUSE_HOURS`.

## 3.0.2 — 2026-07-16

A fleet-management and reliability release on top of 3.0.1: a much sturdier host
enrollment lifecycle (kill-switches and clean-up for runaway/stale hosts), new
system-administration tooling (full firewall management, a time-daemon installer,
an Environment & Shell section), a console that scales to large fleets, and another
round of web-surface hardening. No breaking changes — upgrade in place with
`sysible_controller update`.

### Added
- **Full `ufw` firewall management.** A firewall tool that reads status and the rule
  list, turns the firewall on/off, and adds/deletes allow/deny rules (by port/proto/
  source) — not just an installer. `client/_api_firewall.py`, `webgui/actions.py`.
- **"Environment & Shell" tools section** — manage system-wide environment variables
  (`/etc/environment`, `profile.d` drop-ins) and shell aliases from the console.
- **Time Synchronization — explicit Install buttons for chrony and NTP.** The tool now
  has an **Install** group with **Install chrony** and **Install NTP** buttons (chrony is
  the modern default; NTP installs the classic ntpsec/ntp/ntpd per distro). Previously
  the only way to get a time daemon was the "Configure chrony" button and there was no
  NTP install path at all — so on a host reporting "(neither chrony nor ntp is
  installed)" there was nothing to click. `client/_api_timesync.py`, `webgui/actions.py`.
- **Update Hosts — "Defer to maintenance window".** Alongside *Install now*, you can
  now schedule the install for the selected hosts into a recurring maintenance window
  (security or all updates; weekly on a chosen day / daily, at a set time) instead of
  running it immediately. It creates a Schedule under the hood — same engine, same
  attribution — so it shows up and can be managed on the Schedules page.
  `webgui/frontend/src/views/Updates.jsx`.
- **Pause Enrollment kill-switch.** A one-click emergency brake that stops the
  controller accepting *any* new enrollments — for a runaway host that re-appears
  faster than you can delete it. Existing agents keep running; resume when the source
  is fixed. `Revoke Checked` (lock out the checked agents but keep their records) and
  robust bulk Disenroll / Force Delete round out the runaway-fleet toolkit.
- **`disenroll_agent.sh` and `migrate_agent.sh` in the agent bundle.** Every host now
  ships a self-contained disenroll script (cleanly remove the agent from the host) and
  a migrate script (re-point the agent at a new controller IP after a failover /
  address change) — no console round-trip required. `backend/agent_bundle.py`.
- **The console now shows the deployed build version.** Controller Configuration adds a
  "Current build: v<version> · <commit> (<branch>)" line so an admin can confirm at a
  glance which build is actually running (the released version from `version.py`,
  alongside the existing running-directory / commit deployment guard). `GET /version`
  now includes `version`.

### Changed
- **SSH host connections are being phased out** in favour of the managed agent. Existing
  SSH host records are now clearly marked and **deletable** — individually or with a
  single **"Remove all SSH hosts"** bulk cleanup — and the system-administration tool
  pickers no longer offer SSH-only hosts. `webgui/frontend/src/views/Connect.jsx`,
  `backend/remote_routes.py`.
- **Firewall tool — each installer now sits with its own backend** (ufw / firewalld /
  nftables) instead of a separate "Install a Firewall" group, so installing and managing
  a backend live together. `webgui/actions.py`.
- **Busy tool pages auto-tab** instead of scrolling one long vertical column — a tool
  with several titled action groups is split into tabs automatically.
  `webgui/frontend/src/views/ToolPage.jsx`.
- **SSH Auth Policy uses explicit intent buttons** (apply the chosen policy directly)
  instead of a checkbox-then-apply toggle that made the pending vs applied state
  ambiguous.
- **User & Group Administration — redesigned account list, built for fleet scale.**
  The middle pane is now keyed on the **distinct account** (not the host), so it stays
  bounded to a few hundred usernames even across thousands of hosts. Each account row
  shows a monogram, `uid · shell`, status chips (sudo / locked / system / live
  session), and a **coverage count** (present on N of M synced hosts); accounts group
  into *On every synced host / Partial coverage / System*. Presence expands to a
  **per-environment coverage drill-down** with a one-click **"create on the N missing
  hosts"** remediation. Summary tiles (accounts / privileged / locked / partial) are
  **clickable filters**. `webgui/frontend/src/views/UserGroupPage.jsx`.
- **Enrolled Hosts footer polish.** The destructive-action row no longer reads as a wall
  of solid-red buttons: only the primary action (Disenroll) is solid, Force Delete /
  Revoke use a secondary outline-danger style, and the selection-scoped actions are
  disabled until at least one host is checked. The Pause/Resume toggle drops its
  unreliable ⏸/▶ glyphs. `webgui/frontend/src/views/HostEnrollment.jsx`.
- **The agent's interactive terminal defaults to `bash`,** not the systemd unit's
  `/bin/sh`, so the shell matches what an admin gets over SSH.

### Fixed
- **Duplicate enrollment when a host reported an empty hostname.** A host that
  registered without a resolvable hostname (seen with some bastion-fronted agents) could
  create a second record on every re-enroll instead of superseding the stale one; the
  reconciler now adopts the existing record by IP when the incoming hostname is empty.
  `backend/app.py`.
- **Runaway host enrollment.** Hardened the enroll path against a mis-configured source
  spawning an unbounded stream of records — stable host-id derivation plus the mass-
  revoke / robust bulk operations above.
- **openSUSE / SLES user management ("View Status by Host" etc.) crashed with
  `__init__() got an unexpected keyword argument 'capture_output'`.** openSUSE Leap /
  SLES 15 ship Python 3.6, where `subprocess.run`'s `capture_output=` / `text=` (added
  in 3.7) don't exist; replaced both with `stdout=PIPE, stderr=PIPE,
  universal_newlines=True` (works on 3.6+), with a regression test. `client/_api_users.py`,
  `host_agent/agent.py`.
- **Updater now gives a clear remedy when tracked files are locally modified** instead of
  failing the `git pull` with a bare conflict.
- Ported four shared console fixes from the Enterprise build to Community.

### Security
- **Hardened web-facing defaults.** `Secure` cookie flag on by default, a CSRF
  Origin/Referer backstop on state-changing routes, `/openapi.json` restricted to
  loopback, and clamped list-endpoint limits. `webgui/server.py`.
- **fstab-line injection via mount options (fixed).** NFS/CIFS mount options were
  `shlex`-quoted for the mount command but written literally into an `/etc/fstab`
  line on persist — a newline could append an attacker-controlled fstab entry
  mounted at every boot as root. Options are now rejected if they contain
  whitespace or newlines. `client/_api_filesystem_mount.py`.
- **`_validate_path` rejects CR/LF as well as NUL,** closing a header/line-injection
  vector through path parameters.
- Additional QA-sweep hardening (input validation, timer-name traversal, scaling
  limits) and an infra/reliability batch (logging, robustness) across the command
  paths.

Findings from a pre-release enterprise security & UX audit:
- **Command injection via an admin username → root on SSH-managed hosts (fixed, HIGH).**
  The "user does not exist" status echo in the SSH exec/terminal builders interpolated
  the raw admin username; a username holding `$(…)`/backticks (double-quoted echo) or a
  single quote (single-quoted echo) executed as the SSH login user (root) on hosts where
  the admin had no local account — letting a non-superuser sysadmin escalate to root and
  defeat per-user RBAC. Admin usernames are now charset-validated at ingest (letters,
  digits, `. _ @ -`, no leading `-`) and the echoes emit the username as a shlex-quoted
  word. `backend/models/portal_models.py`, `backend/remote_routes.py`.
- **fstab-line injection via fstype / mount options / NFS export path / CIFS share
  (fixed).** The earlier options fix didn't cover these adjacent fields, which are
  written verbatim into a persisted `/etc/fstab` line; a newline could append an
  attacker-controlled boot-time mount. All now reject CR/LF/NUL (and fstype is a single
  token). `client/_api_filesystem_mount.py`.
- **Session & bearer tokens are now hashed at rest.** Admin login tokens and portal
  session tokens are stored only as their SHA-256, so a leaked database snapshot no
  longer yields directly-replayable live sessions. (Upgrade note: existing sessions must
  re-login once.) `backend/db.py`.
- **Local-package upload is now size-bounded** (Content-Length pre-check + capped read →
  413), matching the file-transfer path, so an authenticated operator can't OOM the
  console with an oversized upload. `webgui/server.py`.
- Removed a stray NUL byte from a console source file that tripped text tooling.
  `webgui/frontend/src/views/ToolPage.jsx`.

Console reliability &amp; hardening (audit follow-through):
- **The web console no longer wedges on a hung request or an expired session.**
  Every API call now has a request timeout (a stuck controller call aborts instead
  of pinning a spinner forever), a single 401 handler drops the stale session and
  returns to the login screen with a clear message, and a top-level error boundary
  turns a render-time error into a recoverable panel instead of a white screen.
  `webgui/frontend/src/api.js`, `App.jsx`, `components/ErrorBoundary.jsx`, `main.jsx`.
- **Login brute-force lockout is now durable.** The admin-login throttle is stored in
  the database, so an accumulated lockout survives a controller restart / crash-loop
  instead of resetting with the process; the console's per-IP login throttle is now
  mutation-locked. `backend/db.py`, `backend/app.py`, `webgui/server.py`.
- **Python dependencies are pinned** for reproducible builds (the frontend was already
  lock-pinned); the agent's `requests` stays a compatible range for host-Python
  portability. `requirements.txt`, `webgui/requirements.txt`, `host_agent/requirements.txt`.
- **The controller bind address is configurable** via `SYSIBLE_CONTROLLER_BIND` so an
  operator can listen on a single management/agent NIC instead of all interfaces;
  firewalling `:9000` remains the primary control (documented in SECURITY.md).
  `install_sysible.sh`, `SECURITY.md`.
- **Re-enrolling a still-online host gives a clearer, actionable 409.** A live host
  can't be re-enrolled in place — a fresh enroll token echoes the caller's requested
  host_id, so allowing it would let a token holder overwrite a live host's secret and
  hijack it. The message now says what to do: wait for the host to go offline (a genuine
  restart re-enrolls fine once its record goes stale, ~5 min), or Force Delete its record
  first, then enroll fresh. `backend/app.py`.

## 3.0.1 — 2026-07-10

A large security-hardening and reliability release on top of 3.0.0, plus new
fleet-management conveniences. Every managed-host command path, the
agent↔controller protocol, and the web console were re-audited from scratch;
the highlights below group the results by theme. No breaking changes — upgrade
in place with `sysible_controller update`.

### Security

Command execution & injection
- **Grub command injection → root on the managed host (fixed).** `cmd_set_grub_default`
  concatenated the raw menu entry into an echo, so an entry like `0'; <cmd>; echo '`
  ran `<cmd>` as root. The value is now `shlex`-quoted via `printf` and never enters
  the shell string.
- **SSH argument injection → controller root code execution (fixed).** A stored SSH
  host's `user`/`ip` flowed into the `ssh` command line; a value like
  `-oProxyCommand=…` was parsed by ssh as an option and ran on the controller as root
  (and could be triggered automatically by the read-only fleet sweeps). Host
  `user`/`username`/`ip`/`name` are now charset-validated at ingest and the ssh argv
  carries an explicit `--` before the destination.
- **Defense-in-depth on the unit/mount builders.** The systemd service/timer builders
  reject newline breakout of the unit-file heredoc and `/`/`..` in a unit name; the
  mount builder rejects newlines in a mount point (no injected `/etc/fstab` entry);
  and an agent-reported `ip` is charset-validated at ingest.

Authentication, sessions & authorization
- **Forced first-login password change is enforced server-side** — every write/dispatch
  route (and the interactive terminal WebSocket) returns 403 until the temporary
  password is rotated, and a username-only credential change no longer lifts the gate.
  Previously this was only a frontend modal an operator could skip.
- **The console session is revalidated against the live account.** A demoted or removed
  admin's signed cookie used to keep BFF-gated powers until the 12h token expiry. Each
  request now re-checks the admin token against the controller (new `GET /admin/whoami`,
  TTL-cached to one call/minute/session) and drops the session the moment the token is
  revoked (`SYSIBLE_SESSION_REVALIDATE_TTL`).
- **Admin-login throttle is per-username, and login is constant-time.** The lockout was
  keyed on the caller's IP — which for every console login is the single BFF, so ten
  failures locked out *all* admins; it's now per-account. A decoy PBKDF2 verify runs
  when the username doesn't exist (admin and portal logins), closing a
  username-enumeration timing oracle.
- **Pure-SSH file upload/download now requires superuser** on the controller (was
  API-key-only), matching the BFF's separation of duties for the SFTP-as-root path.
- **Logout revokes the controller token**, not just the stateless cookie, so a captured
  token can't linger to expiry. `/api/me` fails closed to `auditor` on a missing role.

Secrets, keys & TLS
- **TLS pinning is fail-closed.** A missing pin file on an `https://` controller used to
  silently fall back to system-CA verification (any CA in the store could MITM the
  agent/BFF→controller channel); it now refuses to connect
  (`SYSIBLE_ALLOW_SYSTEM_CA=1` to opt into the system trust store).
- **Secrets are created `0600` atomically** (`O_EXCL|O_NOFOLLOW`) — the sudo-store key,
  the admin API key, and the cookie-signing secret — closing a world-readable umask
  race a local user could exploit to read the key or forge admin cookies.
- **The install-time default admin password is passed via the environment, not argv**,
  so it's no longer visible to any local user through `ps`/`/proc/<pid>/cmdline`.
- **The agent no longer puts its secret in a URL.** The interactive-terminal long-poll
  sent the agent secret as a query parameter, recording a live credential in the
  controller access log every ~25s; it now uses the `X-Agent-Secret` header.

Enrollment & host identity
- **`host_id` is charset-validated at enrollment** (alphanumeric plus `._-`, reserved
  sentinels refused) — an injection-shaped or `*`-style id is rejected outright.
- **Enrollment adoption can no longer bypass revocation or hijack a host.** The
  re-enroll "supersede" path matched on the unauthenticated request-body IP and could
  resurrect an admin-revoked host or seize an offline host's identity. Adoption is now
  narrow (same hostname **and** IP, never a revoked record, never a live host), and a
  revoked host must be explicitly Restored from the console.
- **Alert webhook delivery is pinned to the verified IP** — the SSRF guard checked the
  resolved host was public but `urlopen` re-resolved it (DNS rebinding); the request now
  connects to the exact IP that passed the check (TLS cert/SNI still validated against
  the hostname).

### New features & capabilities
- **The controller enrolls itself as a managed host.** On first start it installs a
  privileged local agent pointed at loopback and enrolls itself, so it appears in the
  fleet like any other box — patch it, run scripts on it, open a terminal into it.
  Idempotent, best-effort (never fails the controller start), opt-out via
  `SYSIBLE_NO_SELF_ENROLL=1`; run on demand with `sudo sysible_controller self-enroll`.
- **Restore a revoked host in place** — a Restore button (and `POST /agents/{id}/restore`)
  un-revokes without a destructive re-enroll, keeping the agent secret so a
  still-installed agent resumes immediately.
- **Force Delete now permanently removes a zombie host** by also purging the enrollment
  token bound to it, so a still-running agent can't re-enroll onto the same id with the
  old token. `tools/unenroll_agent` gained the same purge, plus `--ip`/`--name` matching,
  DB auto-detection, and `--dry-run`.
- **Deployment guard.** The controller now reports which directory and git commit the
  live process is running from (`GET /version`, Settings → Controller Configuration),
  and shows a red "restart needed" banner when the on-disk code has moved since start —
  ending the silent "I pulled but nothing changed" trap.
- **On-demand certificate management** — a "Regenerate self-signed certificate" action
  reissues for the current address and restarts atomically; a "Download agent bundle"
  / "Regenerate agent bundle" button re-mints a fresh bundle (new single-use token)
  after a hostname/IP change.
- **URL-addressable navigation** — every console view has a URL (`?view=<key>`), so you
  can ⌘/Ctrl-click, middle-click, open in a new tab, and bookmark/share a view.
- **Run a script across the hosts you choose** — clarified in Sysible Connect and added
  as a "Run a script" panel in Quick System Actions (runs on the checked hosts).
- **Standalone New Environment button**, and an **offline documentation download**
  (a self-contained HTML manual, `GET /api/docs/download`).

### Reliability & fixes
- **Host updates no longer fail on far-behind hosts** — the agent command cap moved from
  5 to 30 minutes (`SYSIBLE_AGENT_CMD_TIMEOUT`) so a real `dnf`/`apt`/`zypper` upgrade
  isn't SIGKILLed mid-transaction; the console's install poll matches it. "Refresh
  metadata & rescan" allows 180s for a slow mirror; zypper security updates no longer
  skip license-gated patches.
- **Dashboard host counts are consistent and stable.** Enrolled/online/offline now come
  from the instant agent inventory (heartbeat `last_seen`) instead of the slow
  fleet-health probe sweep, so a host no longer "falls off" to 0 while a sweep runs, and
  the Dashboard tile agrees with Host Enrollment. Revoked hosts are excluded from the
  counts, the donut, and the fleet-action set.
- **Quarantined hosts fail fast** instead of hanging: installing updates on an
  integrity-quarantined host reports the reason immediately (was a silent 15-minute
  spin), and quarantined/offline hosts no longer stall the posture/patch/fleet-query
  sweeps.
- **Re-enrolling a host no longer auto-quarantines it**, and re-enroll no longer creates
  a duplicate "zombie" record at the same IP (nor wipes a sibling host's SSH record on
  cleanup).
- **"Set Environment" no longer times out** (`read timeout=15`): hot enroll/disenroll DB
  writes now release the single SQLite WAL writer even on error, and the console's write
  timeout was raised above the controller's DB busy-timeout so a contended assignment
  commits instead of spuriously failing.
- **Self-update is robust.** The periodic update-check uses read-only `git ls-remote`
  (writes no refs, leaves no locks); `sysible_controller update` clears stale `*.lock`
  files and auto-repairs a corrupted ref store instead of dead-ending on "reference
  already exists"; and `/update-status` bounds its remote check so agent counts stay
  fresh (no phantom agent after removal). "Update agents" excludes revoked hosts.
- **Regenerating the self-signed cert no longer leaves the controller down.** The
  restart is scheduled through a detached `systemd-run` timer outside the service's own
  cgroup, so it survives the teardown (root cause of the post-regen
  `HTTPSConnectionPool` failure). Changing the controller address now only flags the
  cert stale rather than breaking every pinned client until a manual restart.
- **Terminal to an offline agent fails fast** with a clear reason instead of a dead
  cursor for minutes; abandoned agent-terminal sessions no longer leak controller memory.
- **"Upload & install a local package" works on agent-managed hosts** (was SFTP-only,
  unreachable for outbound-only agents). Self-service username change works without also
  changing the password. Host Enrollment refreshes are lighter (portal data loads lazily
  on its tab) and a transient post-action timeout no longer paints a false error.
- **Web console display fixes** surfaced by a full click-path audit: the Webserver Portal
  tab's "last login" and Active Sessions timestamps render correctly; Environmental
  Policies "Save Policy Defaults" actually persists (the editor round-trips the nested
  policy shape instead of silently resetting to defaults); Performance charts don't skew
  their edge buckets when zoomed; Lock/Unlock/Delete user confirm the action instead of a
  bare "exit 0"; self-disenroll serializes against enrollment.

### Performance
- **Posture / fleet-health sweeps are much faster** — the integrity `find /` walks are
  capped at 8s (was 20s, `SYSIBLE_POSTURE_FIND_TMO`) and sweeps run with more concurrency
  (up to 32, `SYSIBLE_SWEEP_CONCURRENCY`), so a fleet finishes in far fewer waves. The
  ~5s host-list stall from a reverse-DNS lookup on `GET /agents` is gone (DNS-free +
  cached).

### Platform & compatibility
- **openSUSE / SLES fixes across the host command builders.** Sudo policy detects the
  `wheel` group (there is no `sudo` group on SUSE/RHEL, so the policy was silently
  ineffective); kernel listing, sshd, and firewalld paths/dependencies were corrected;
  firewalld only diagnoses the missing-`gi` case on the actual error; and the installer
  seeds `python3-gobject` where needed.
- **TLS material is written atomically** (cert/key/trust) so an interrupted install can't
  leave a half-written trust bundle.

### Testing
- New regression coverage for the security work: SSH host-injection rejection, the
  self-enroll bundle, `/admin/whoami` + per-username throttle, and the `unenroll_agent`
  force-removal path.


## 3.0.0 — 2026-07-07

First official tagged release. Highlights of the hardening and cleanup that
went into it:

### Security
- Removed a client-controlled audit/authorization bypass: an operator could set
  `log=false` on an SSH exec to skip both the read-only-auditor block and the
  audit record. The auditor block is now unconditional.
- Enrollment-token replay hardening: a leaked token can no longer take over a
  still-live host or resurrect an administrator-revoked one.
- Agent integrity: a host that sealed a measurement baseline and then stops
  reporting is quarantined (evasion-by-omission closed); the integrity state
  store is now locked and written atomically at `0600`.
- Agent-channel payloads (metrics / snapshot / measurements / task result / PTY
  output) are size-capped to bound controller memory against a hostile agent.
- Audit-log command text is scrubbed of well-known secret-bearing arguments
  (`--password`, `--token`, `Authorization: Bearer`, `KEY=value`, …).
- An agent-reported IP can no longer delete or repoint another host's SSH
  record; the collision is surfaced instead of silently clobbering data.
- Interactive terminals are audited (open/close) and bound to the operator who
  opened them.
- The "must change password at next login" flag is now enforced in the console
  (previously ignored).

### Reliability & operations
- Fixed a web-console startup crash (module-level use of `threading` before its
  import).
- Serialized the heartbeat-path JSON stores (`agent_ssh_state`, `hosts.json`,
  integrity) to end lost-update races under concurrent heartbeats.
- The hottest DB writers release their SQLite connection even on exception, so a
  leaked WAL reservation can't compound lock contention.
- Fleet-health is cached and shared across concurrent dashboard loads instead of
  re-probing every host per load.
- A failed agent task now reports back immediately instead of hanging until a
  15-minute reclaim rewrites it as a fabricated timeout.
- Guardrails against foot-guns: the last superuser can't be deleted; an
  "all hosts" reboot/power-off skips the controller's own node; `destroy`'s DB
  backup is written `0600`; deleting an environment that still has hosts is
  refused; duplicate environment names return a clear error.
- Enrolled Hosts gains a **Force Delete** action: drops a zombie host (a broken
  agent build that keeps heartbeating but can't cleanly disenroll) from the
  console immediately, skipping the graceful agent teardown that would otherwise
  stall. Deleting the record also locks the agent out on its next heartbeat.
- Replacing the TLS certificate now warns and confirms (it breaks pinned agents
  until the trust bundle is redistributed).
- Agent-update no longer false-quarantines a host; applying an SSH change can
  reload `sshd`.
- **Agent install works on SUSE and other minimal images.** `run_agent.sh` now
  installs Python 3 if absent, prefers the distro `python3-requests` package
  (no PyPI/compiler needed, sidesteps PEP 668), only falls back to pip and only
  passes `--break-system-packages` when that pip supports it, and hard-fails with
  clear per-distro guidance if `requests` still can't be installed — instead of
  the old pip-only path that aborted on SUSE. The systemd unit is also pointed at
  the python3 that was actually found rather than a hardcoded `/usr/bin/python3`.

### Usability
- **Host-enrolled notification.** When a new host enrolls, the console pops a
  toast (from any page) and records the enrollment in the Live Activity feed.
- Community Edition: all host/administrator seat caps removed; a small
  "Community Edition" badge replaces the counts.
- Nav reordered to follow the fleet workflow; file-transfer panel and browse
  buttons made consistent; de-cramped Enrolled Hosts rows.
- Schedule builder validates its fields (no more `*/0`/`NaN` cron) and notes
  that jobs run in the target host's local timezone.
- Accessibility and consistency pass: focus rings, colour contrast, missing
  confirmations and loading indicators.

### Testing & tooling
- Added an exhaustive API test-suite (72 tests) covering authentication,
  RBAC/permissions, input validation, SQL/XSS injection, size caps, duplicate
  requests, and rate limiting. Run with `pytest`.
- Added a SessionStart hook that provisions the test environment for Claude Code
  on the web.

### Housekeeping
- Removed the legacy backward-compat shim scripts `sysible`, `start_sysible.sh`,
  and `stop_sysible.sh` (thin redirects to the `sysible_controller` CLI). Use
  `sysible_controller {start|stop|…}` — the systemd services and installer are
  unaffected.

### Documentation
- README and SECURITY updated for the current feature set, plus a
  "Known limitations & operational notes" section (TLS trust-bundle refresh,
  SIEM forwarding for durable audit, single-node SQLite write ceiling,
  bearer enrollment tokens, host-local schedule timezones).
