# Changelog

All notable changes to the Sysible Controller are recorded here.

## Unreleased

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
