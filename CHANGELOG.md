# Changelog

All notable changes to the Sysible Controller are recorded here.

## Unreleased

### Security
- **SSH argument injection → controller root code execution (fixed).** A stored
  SSH host's `user`/`ip` flow into the `ssh` command line as the `user@ip`
  destination; a value like `-oProxyCommand=…` was parsed by ssh as an option and
  ran on the controller as root (and could be triggered automatically by the
  read-only fleet sweeps). Host `user`/`username`/`ip`/`name` are now charset-
  validated at ingest (no leading `-`, no shell metacharacters) and the ssh argv
  carries an explicit `--` before the destination.
- **Sudo-store key no longer has a world-readable creation window.** The Fernet
  key that decrypts stored sudo passwords and the admin token cookie was written
  at the umask default and `chmod`ed afterward; it's now created `0600` atomically
  (`O_EXCL|O_NOFOLLOW`).
- **Forced first-login password change is now enforced server-side.** The
  "you must change your temporary password" state was only a frontend modal; an
  operator could skip it and drive the API directly on the known initial
  credential. Every write/dispatch route (`require_operator`) now returns 403
  until the password is actually rotated (the change-credentials route stays
  reachable to clear it).
- **The forced-password gate now also covers the interactive terminal.** The
  terminal WebSocket does its own inline auth and skipped the `must_change_password`
  check, so an operator on a temporary credential could open a full shell (the most
  privileged action) without rotating it. The WS now applies the same gate, and a
  username-only credential change no longer lifts the gate without a password reset.
- **The agent no longer puts its secret in a URL.** The interactive-terminal
  long-poll sent the agent secret as a query parameter, so uvicorn's access log
  recorded a live agent credential every ~25s for the whole session. It now uses
  the `X-Agent-Secret` header like every other agent call.
- **TLS pinning is fail-closed.** On an `https://` controller a missing pin file
  used to fall back to system-CA verification, silently dropping pinning (any CA in
  the store could MITM the agent/BFF→controller channel). It now refuses to connect
  without the pin; set `SYSIBLE_ALLOW_SYSTEM_CA=1` to deliberately use the system
  trust store.
- **Admin API key and cookie-signing secret are created `0600` atomically.** Both
  were written then `chmod`ed, leaving a world-readable umask window a local user
  could race to read the API key or forge admin session cookies. Now created with
  `O_EXCL|O_NOFOLLOW` like the sudo-store key.
- **The console session is revalidated against the live account.** The signed
  session cookie froze identity/role at login, so a demoted or removed admin kept
  BFF-gated powers (fleet reads, superuser-only portal-file control) until the 12h
  token expiry. Each request now re-checks the underlying admin token against the
  controller (new `GET /admin/whoami`), TTL-cached to one call/minute/session, and
  drops the session the moment the token is revoked (`SYSIBLE_SESSION_REVALIDATE_TTL`).
- **Admin-login throttle is keyed per username, and login is constant-time.** The
  controller lockout was keyed on the caller's IP — which for every console login
  is the single BFF, so ten failures locked out *all* admins. It's now per-account.
  A decoy PBKDF2 verify also runs when the username doesn't exist (admin and portal
  logins), closing a username-enumeration timing oracle.
- **Alert webhook delivery is pinned to the verified IP.** The SSRF guard resolved
  the webhook host and checked it was public, but `urlopen` re-resolved it — a
  hostile DNS server could answer the check public and the request internal (DNS
  rebinding). The request now connects to the exact IP that passed the check (TLS
  cert/SNI still validated against the hostname).
- **Defense-in-depth on the unit/mount builders.** The systemd service/timer
  builders reject newline breakout of the unit-file heredoc and `/`/`..` in a unit
  name (no redirecting the write); the mount builder rejects newlines in a mount
  point (no extra `/etc/fstab` line); and an agent-reported `ip` is charset-validated
  at ingest. All were already contained (argv-quoted, behind `--`, parameterized
  SQL) — these close the gaps at the source.

### Fixed
- **Webserver Portal tab timestamps render correctly.** A full click-path audit of
  every console button found two display-only response-shape mismatches: the
  "last login" line showed `[object Object]` (the status returns a
  `{timestamp,…}` object, not a scalar) and every Active Session's "Logged In"
  column was blank (the row's timestamp column is `created`, which the cell didn't
  read). Both now render the real time. The audit found no broken buttons
  elsewhere — every other control traces end-to-end.
- **Environmental Policies "Save Policy Defaults" now actually persists your
  edits.** The editor read and wrote a flat field shape while the controller
  stores/returns a nested one (`password`/`lockout`/`sudo`/`umask`), so loading
  always fell back to hardcoded defaults and every Save quietly reset the policy
  to defaults regardless of what you typed. The form now round-trips the nested
  shape.
- **Self-disenroll now serializes against enrollment** (`_ENROLL_LOCK`), matching
  the admin Force-Delete path, so a zombie agent re-enrolling with the same token
  can't interleave and recreate the record right after the delete.
- **Performance charts no longer skew their edge buckets when zoomed.** Samples
  outside the visible time window were clamped onto the first/last bucket instead
  of being skipped, distorting the boundary averages.
- **Lock / Unlock / Delete user now confirm what happened** instead of showing a
  bare "exit 0." The `usermod -L`/`-U` and `userdel` commands emit nothing on
  success, so the console couldn't tell you the account was actually locked; they
  now append a clear confirmation message (value-free, so the username is never
  interpolated into the echo).
- **Host updates no longer fail on hosts that are far behind.** The agent capped
  every command at 5 minutes, so a real `dnf`/`apt`/`zypper` upgrade on a
  months-behind host was SIGKILLed mid-transaction and reported "failed" (and
  could leave a half-configured package state) — exactly the hosts most in need.
  The cap is now 30 minutes (`SYSIBLE_AGENT_CMD_TIMEOUT`) and the console's
  install poll matches it (`SYSIBLE_FLEET_INSTALL_TIMEOUT`).
- **"Refresh metadata & rescan" no longer times out.** A live update rescan runs
  `dnf makecache` / `apt-get update` / `zypper refresh` first, which alone can
  exceed the old 60s probe deadline on a slow mirror; the live path now allows
  180s (`SYSIBLE_UPDATE_PROBE_TIMEOUT`).
- **zypper "install security updates" no longer silently skips license-gated
  patches** — added `--auto-agree-with-licenses`.
- **A quarantined host no longer stalls the whole posture / patch / fleet-query
  sweep.** All three now fail that host fast (like the install path already did),
  and fleet-query also skips offline agents instead of spinning 60s each.
- **A revoked host can be Restored from the console.** The controller always had
  `POST /agents/{id}/restore` (un-revoke in place, keep the secret, no re-enroll),
  but it had no BFF route, API method, or button — a revoked host's only path back
  was disenroll + full re-enroll. There's now a **Restore** button in Host
  Enrollment.
- **"Upload & install a local package" works on agent-managed hosts.** It
  unconditionally used SFTP, which can't reach an outbound-only/NAT'd agent host
  (upload failed). It now pushes through the agent (SFTP reserved for pure-SSH
  hosts, superuser-only, matching file upload), with a clear message when a
  package exceeds the agent transfer limit.
- **Self-service username change works without also changing the password.** The
  "(optional)" New username / New password fields rejected an empty password, so a
  username-only rename was impossible; either (or both) can now be changed.
- **A failed fleet-health probe no longer shows a host as green/OK.** A host that
  heartbeats but whose metrics probe fails/times out (or is quarantined) is now
  flagged in "Needs attention" instead of grading as healthy on zeroed metrics.
- **Security-posture chips no longer vanish when the health sweep is empty.** They
  now key off the posture sweep's own liveness, so findings stay visible when the
  fleet is degraded.
- **Revoked hosts no longer skew the fleet donut / environment rollup.** They were
  excluded from the top-strip counts but still filled the donut as permanent
  OFFLINE rows; they're now excluded from the fleet-action host set too (still
  shown in Host Enrollment with Restore/Force-Delete).
- **Disenroll refreshes the host list even when a teardown warning is shown** (the
  removed hosts used to linger, making a successful disenroll look like a failure);
  the **"Needs patching"** tile refreshes on the dashboard cadence instead of
  freezing at page-load; the service/package **List** button lists the currently
  checked host instead of the previously listed one.
- **Terminal to an offline agent fails fast** with a clear reason instead of
  showing "Connected." then a dead cursor for ~3 minutes; abandoned agent-terminal
  sessions no longer leak in controller memory.
- **`/api/me` fails closed to `auditor`** on a missing role (was fail-open to
  superuser), matching the login handler's documented hardening.

- **Dashboard "Hosts enrolled" no longer drops to 0 while the fleet-health sweep
  runs.** The top-strip counts (enrolled / online / offline) were derived from the
  fleet-health *probe* sweep result, so a slow or integrity-quarantined host being
  probed could drag the sweep out for minutes and the enrolled count would read 0
  until it finished — a host appeared to "fall off" and come back. The counts now
  come from the instant agent inventory (heartbeat `last_seen`): enrolled is a live
  DB fact, online/offline use the same 20s staleness rule, and a quarantined host
  (which still heartbeats) stays counted. The probe sweep now powers only the
  detailed health donut / metrics / triage.
- **Installing updates on a quarantined host fails fast instead of hanging 15
  minutes.** A dispatch to an integrity-quarantined host queues a task the
  controller never hands out (soft lockout), so the console's per-host poll spun
  the full 900s deadline showing a silent spinner. The install now detects the
  quarantine up front and reports it immediately with an actionable reason
  (rebaseline/resume the host).
- **Host-list refresh no longer stalls ~5s (Set-Environment "slow to show up").**
  The controller-self label added to `GET /agents` computed the controller's own
  identity with `socket.getfqdn()` — a blocking **reverse-DNS** lookup that hangs
  for seconds on a LAN without reverse records — on *every* host-list refresh.
  It's now DNS-free (local hostname + NIC addresses only) and cached, so refreshes
  are instant again. A controller enrolled as a host still matches by IP.

### Added
- **The controller enrolls itself as a managed host.** On first start
  (`sysible_controller start`) the controller now installs a privileged local
  agent pointed at loopback and enrolls itself, so it appears in the fleet like
  any other box — patch it, run scripts on it, open a terminal into it. It's
  idempotent (skips if already enrolled), best-effort (never fails the
  controller start), and opt-out via `SYSIBLE_NO_SELF_ENROLL=1`. Run it on demand
  with `sudo sysible_controller self-enroll`. Backed by a new loopback bundle
  endpoint (`GET /self-enroll-bundle`) that works even before any LAN
  hostname/IP is configured.

### Performance
- **Posture / fleet-health sweeps are much faster.** The posture scan's three
  full-filesystem `find /` integrity walks were capped at 20s each — the dominant
  per-host cost, so a sweep felt slow even with a handful of hosts. The cap is now
  8s (overridable via `SYSIBLE_POSTURE_FIND_TMO`), and the sweeps run with more
  concurrency (up to 32, `SYSIBLE_SWEEP_CONCURRENCY`) so a 20+ host fleet finishes
  in far fewer waves.

### Web console
- **Left-rail nav items are real links now** — each view has a URL (`?view=<key>`),
  so you can right-click → open in a new tab, ⌘/Ctrl-click, middle-click, and
  bookmark/share a view. Plain click still navigates instantly in-app. Opening
  `?view=…` in a fresh tab lands on that view.
- **All host counts share one live source.** The Dashboard "Hosts enrolled" tile
  used `edition.host_count`, which is fetched once at login and never refreshed —
  so a just-disenrolled host lingered as "1 enrolled" while Online/Offline (from
  the live fleet-health sweep) correctly showed 0. It now uses the same live
  sweep, so enrolled/online/offline always agree with each other and with Host
  Enrollment.
- **Regenerate agent bundle** — the Settings button now re-mints a fresh bundle
  for the current controller address (with a new single-use enrollment token) and
  downloads it, with a confirmation, instead of a passive download link.

### Fixed
- **Re-enrolling a host no longer auto-quarantines it.** The agent integrity
  baseline is keyed by host_id and deliberately survives disenroll — but a
  re-enroll reuses the same host_id with a freshly-installed agent whose
  self-measurement legitimately differs (the enrollment bundle patches the
  controller address into `agent.py` at build time, and the agent may be a newer
  build). The next heartbeat then compared the new measurement against the STALE
  baseline and quarantined a perfectly healthy host. Enrollment now drops the old
  baseline so the next heartbeat re-seals from the current measurement (exactly
  what an admin Rebaseline/Restore does), and both disenroll paths forget the
  baseline too, so nothing lingers to quarantine a future re-enroll.
- **"Set Environment" no longer times out with `read timeout=15` (and the label
  updates promptly).** Two causes: (1) several hot enroll/disenroll DB writes
  (`set_agent_environment`, `delete_agent`, `consume_enroll_token`) used a bare
  connection with no `try/finally`, so a transient "database is locked" leaked the
  single SQLite WAL writer and stalled every later write on the single-process
  controller — they now release the connection even if a write raises. (2) The
  console's write timeout (15s) was *shorter* than the controller's DB
  `busy_timeout` (30s), so a legitimately-contended assignment surfaced as a
  spurious timeout before it committed; the idempotent environment write now
  allows 35s. The unassigned→environment label was only ever "slow" because the
  write was stalling — fixing the write fixes the label.
- **Self-update no longer wedges on a stale git lock (root cause of "reference
  already exists").** The update-check endpoint used to run a live `git fetch` in
  the deployment repo; when that fetch was killed (client timeout, cancelled
  request, or a restart mid-fetch) it stranded `*.lock` files under `.git/refs`,
  and every subsequent ref update then failed with `cannot lock ref ... .lock:
  File exists`, surfacing to `sysible_controller update` as `reference already
  exists` and dead-ending self-update. Two fixes: the periodic update-check now
  uses `git ls-remote` (a read-only remote query that writes no refs and fetches
  no objects, so it can neither collide with the self-update pull nor leave
  locks); and `sysible_controller update`'s automatic ref-store repair now clears
  stale `*.lock` files first, so a repo already wedged by an older build heals on
  the next update.
- **Self-update auto-repairs a corrupted git ref store.** `sysible_controller
  update` could dead-end on `fetching ref refs/remotes/origin/... failed:
  reference already exists` — a loose remote-tracking ref colliding with
  packed-refs (typically left by a previously interrupted update), not a code
  problem. The updater now attempts a safe automatic repair (clears stale locks,
  then rebuilds only the origin/* remote-tracking cache — no local commits or
  working-tree changes are touched) and retries the pull, instead of failing
  outright.
- **Software-updates panel no longer shows a phantom agent after removal.** The
  `/update-status` check bundles the (instant) agent counts with a live remote
  update-check that could take up to 35s — past the console's 15s read timeout, at
  which point the console kept its *last* result and a just-disenrolled host kept
  showing as "1 agent." The check now uses a fast, bounded `git ls-remote`
  (`SYSIBLE_UPDATE_FETCH_TIMEOUT`, default 8s), so the endpoint returns quickly
  with fresh counts; a slow network merely degrades the controller-update check to
  "couldn't check."
- **"Update agents" no longer targets disenrolled/revoked hosts.** A revoked host
  keeps its DB row (for history / re-enroll) but its agent secret is revoked, so
  it can never poll a task — yet it still showed up as an outdated host to update
  and got an update task queued. `GET /update-status` and `POST /agents/update`
  now exclude revoked hosts from the outdated count, the fleet total, and the push.
- **Host Enrollment refreshes are lighter and faster.** Every action on the page
  used to refetch the Webserver Portal's login history, sessions, uploads, and
  downloads too — ~4 extra controller calls per action that only matter on the
  Portal tab. Those now load lazily when the Portal tab opens, and a transient
  timeout during a post-action refresh no longer paints a red error over an
  action that actually succeeded.
- **Changing the controller address no longer bricks the console's connection.**
  Saving a new hostname/IP used to immediately regenerate the self-signed cert
  and overwrite the pinned trust file, but the running server keeps serving the
  old cert until a restart — so every TLS client (the console over loopback,
  agents pinning `server.crt`) then failed verification with an
  `HTTPSConnectionPool`/SSL error until a manual restart. The address change now
  only flags the cert as stale; the **Regenerate self-signed cert** action
  reissues for the new address and restarts atomically, so the served cert and
  the pinned trust move together.

### Web console
- **New Environment button** in Host Enrollment: create an empty environment on
  its own, without first checking hosts. (Previously an environment could only be
  made as a side effect of assigning checked hosts via the "+ New environment…"
  option, so creating one standalone appeared to do nothing.)
- **Run a script across the hosts you choose.** The fleet script in Sysible
  Connect now makes it clear it runs on the *checked* hosts (or all if none are
  checked) — the label and button reflect the current selection — and the same
  capability is now available in **Quick System Actions** (a "Run a script"
  panel that runs on the checked hosts). The "Run Script" button also gives
  feedback ("Enter a script to run…") instead of being a dead, disabled button.
- **Download agent bundle** button (+ a step-by-step note) in Settings next to
  the controller address, so after a hostname/IP change you can grab a fresh
  bundle reflecting the new address and re-run it on hosts.
- **Offline documentation download**: a "Documentation" item in the left rail
  downloads the bundled, self-contained HTML manual (`GET /api/docs/download`,
  login-gated) so it can be read without network access.

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
