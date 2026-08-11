# Roadmap → Fortune 100 production readiness

An engineering task list of hardening work **I (Claude) can do in this codebase**,
to run overnight and in downtime. Scope is code, tests, harnesses, config, and
docs — the things achievable inside the repos.

**Deliberately OUT of scope** (external, tracked by you elsewhere): third-party
penetration test, SOC 2 / ISO 27001 / FedRAMP attestation, external load
certification. Nothing below depends on them.

Editions: **EE** = `sysible-controller-ee` (Postgres-exclusive), **CE** =
`Sysible-Controller` (SQLite). Tag on each task shows where it applies.

---

## How an autonomous run should work

Each overnight/downtime session does **one** unit of work, end to end:

1. Open this file. Pick the **highest-priority unchecked `[ ]` task** (P0 before
   P1 before P2; within a tier, top-down). Skip any task marked `[~]` (in
   progress by another run) or `[blocked]`.
2. Mark it `[~]` with a timestamp, commit that mark first (claims the task).
3. Implement it **fully**: code + tests. Run the test suite (`pytest`) and the
   frontend build where relevant. Do not commit red.
4. Commit to the **designated dev branch** (never `main`/`master`), with a clear
   message. For EE work, use the EE repo/branch; for CE, the CE branch.
5. Check the box `[x]`, add a one-line result under **Progress log**, commit.
6. If blocked (needs a decision, external input, or is bigger than one session),
   mark `[blocked]` with a note and move to the next task. Never leave a task
   half-done without a note.

Guardrails: branch-only, tests-green-before-commit, no destructive ops on real
data, no secrets in commits, keep EE/CE changes in lockstep when the code is
shared.

---

## P0 — blockers for F100 production

### A. App-tier horizontal scale & HA (EE)
Today the API runs as a single uvicorn process (no `--workers`); HA exists only
at the Postgres layer. This is the #1 architectural blocker.

- [ ] **A1. Process-local state inventory (EE).** Find every module-level cache,
  in-memory counter/lock, and background loop that assumes one process (grep for
  the "one cache covers it" / single-process assumptions). *Done when:* a
  committed `docs/SCALE_STATE_INVENTORY.md` lists each item + whether it's safe
  under N workers/replicas.
- [ ] **A2. Externalize shared state (EE).** Move the unsafe items from A1 into
  Postgres (or an optional Redis) so they're correct across workers. Login
  throttle is already "durable" — verify it; the agent-identity cache is flagged
  single-process — fix it. *Done when:* each item externalized or proven safe,
  with a test that simulates two workers hitting the same state.
- [ ] **A3. Singleton-safe background jobs (EE).** Guard schedulers/alert loops
  with a Postgres advisory (leader) lock so schedules/alerts don't double-fire
  under multiple instances. *Done when:* advisory-lock guard + test that two
  instances don't double-run a job.
- [ ] **A4. LB-ready lifecycle (EE+CE).** Add `/healthz` (liveness) and `/readyz`
  (readiness: DB reachable, migrations current) and graceful SIGTERM drain.
  *Done when:* endpoints exist, drain works, tests cover both.
- [ ] **A5. Multi-worker/replica support (EE).** Make `uvicorn --workers N` /
  multiple replicas behind an LB work (remove any sticky-session assumption).
  *Done when:* smoke test passes at workers=4; deploy notes updated.

### B. Reliability & correctness soak
Drive the defect rate toward zero — the lab bug rate is the clearest "not ready"
signal.

- [ ] **B1. Full cross-distro tool audit (EE+CE).** Extend the earlier
  firewall/pacman-class audit to **every** tool × {apt,dnf,zypper,pacman} ×
  error paths. *Done when:* each tool has a matrix test or documented check; all
  found bugs fixed.
- [ ] **B2. Privilege-escalation matrix (EE+CE).** Enumerate every package
  manager / privileged-op not-root phrasing per distro and assert the agent's
  `_looks_like_privilege_error` + SSH regex catch them all (the pacman bug
  class). *Done when:* a data-driven test covers each distro/pkg-mgr.
- [ ] **B3. Dispatch error-path hardening (EE+CE).** Partial fleet failures,
  agent going offline mid-task, huge/binary/unicode output, timeouts. *Done
  when:* tests + fixes for each.
- [ ] **B4. Coverage on bug-prone modules (EE).** Add tests around restore
  feedback, D3lorean, host-list display (where we found bugs this week). *Done
  when:* measurable coverage increase on those files.

### D. DR, backup, upgrade/rollback safety
- [ ] **D1. Backup/restore round-trip test (EE+CE).** `tools/backup.py` →
  `tools/restore.py` into a clean DB; assert parity. *Done when:* an automated
  round-trip test passes.
- [ ] **D2. Reversible migrations + pre-upgrade backup (EE+CE).** Versioned,
  down-able migrations; controller update auto-snapshots first. *Done when:*
  migration framework + a rollback test + auto-backup on upgrade.
- [ ] **D3. Controller↔agent version-skew safety (EE+CE).** Old agent + new
  controller and vice versa must degrade gracefully (this week's "old agent
  ignores new task kind" class). *Done when:* compatibility tests across a
  version gap.

### F. Security self-hardening (code-level)
- [ ] **F1. OWASP ASVS self-review (EE+CE).** Code-level pass over authn/session,
  access control, input validation, SSRF, injection, crypto-at-rest, error
  handling. *Done when:* `docs/ASVS_SELF_REVIEW.md` + fixes for findings.
- [ ] **F2. Authorization test sweep (EE+CE).** Every mutating endpoint asserts
  the correct role gate (superuser/operator/auditor) **and** environment
  scoping. *Done when:* a data-driven test enumerates endpoints × roles.
- [ ] **F3. Secret-handling audit (EE+CE).** At-rest encryption; no secrets in
  logs/URLs/argv/env (some already redacted — verify exhaustively). *Done when:*
  audit doc + fixes + a test asserting no secret leakage paths.

---

## P1 — needed for scale/operability, not day-one blockers

### C. Scale / load testing
- [ ] **C1. Agent-simulator harness (EE).** Spins up N virtual agents (enroll,
  check-in, run tasks) to load a local controller. *Done when:* harness drives
  1k+ simulated agents.
- [ ] **C2. Baseline perf report (EE).** Run C1, capture throughput/latency/DB/
  memory limits, file bottlenecks as new tasks here. *Done when:*
  `docs/PERF_BASELINE.md` committed.
- [ ] **C3. Fix top bottlenecks (EE).** *Done when:* a stated target (e.g. 5k
  agents under X latency) met or the ceiling documented.

### E. Observability
- [ ] **E1. Structured JSON logging + request/correlation IDs (EE+CE).** *Done
  when:* logs structured, secrets redacted, test.
- [ ] **E2. Prometheus `/metrics` (EE+CE).** Request rates/latency, task-queue
  depth, agent counts, DB pool stats. *Done when:* endpoint + core metrics +
  test.
- [ ] **E3. Controller self-health surfacing (EE).** Internal health (DB, queue,
  loops) visible + alertable. *Done when:* surfaced in UI/API.

### G. Supply-chain hygiene (config I can do; scans run in your CI)
- [ ] **G1. Pin dependencies with hashes (EE+CE).** Reproducible installs;
  frontend lockfile integrity. *Done when:* pinned + a clean install verifies.
- [ ] **G2. SBOM generation (EE+CE).** CycloneDX SBOM as a build step. *Done
  when:* SBOM produced by the build.
- [ ] **G3. CI wiring for dep-vuln + secret scanning (EE+CE).** Provide the
  config; the scans themselves run in your pipeline. *Done when:* CI config
  committed. *(Running/acting on results is on your side.)*

### H. RBAC & audit completeness
- [ ] **H1. Audit-on-mutation coverage (EE+CE).** Every state-changing action
  emits a tamper-evident audit entry. *Done when:* a coverage test asserts it
  for all actions.
- [ ] **H2. RBAC least-privilege review (EE).** *Done when:* report + fixes for
  any over-broad grants.
- [ ] **H3. Retention enforcement test (EE+CE).** Prove the DATA_RETENTION policy
  actually prunes. *Done when:* test.

---

## P2 — polish & documentation

- [ ] **I1. Ops runbook (EE+CE).** Deploy, scale, upgrade, backup/restore,
  incident response.
- [ ] **I2. F100 hardening/deployment guide (EE+CE).** The recommended
  production profile end to end.
- [ ] **I3. Backlog burn-down.** Keep clearing `BACKLOG.md` items as they appear.

---

## Progress log

_(newest first — each autonomous run appends one line: date · task · result · commit)_
