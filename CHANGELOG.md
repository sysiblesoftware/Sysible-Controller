# Changelog

All notable changes to the Sysible Controller are recorded here.

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
