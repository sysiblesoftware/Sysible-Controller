# Changelog

All notable changes to the Sysible Controller are recorded here.

## Unreleased

### Fixed
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
