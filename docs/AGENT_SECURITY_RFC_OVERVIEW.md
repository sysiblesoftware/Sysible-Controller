# Agent security hardening — RFC branch overview

**Branch:** `claude/priv-dispatcher-rfc` (off `dev`) · 6 commits · 11 files · +1048/−5
**Status:** RFC-complete prototype — proves the design end to end; **not for merge as-is.**
**Detail:** [`PRIVILEGE_DISPATCHER_RFC.md`](PRIVILEGE_DISPATCHER_RFC.md) (full design, inventory, honest limits, migration).

## The problem this addresses

Today the agent runs as root (or as `sysible` with `NOPASSWD: ALL`) and escalates
via `sudo bash -c "<arbitrary shell>"`. That makes the agent **root-equivalent**:
a compromised agent — or a compromised controller dispatching to it — owns every
host. The per-user `runuser` model gives accountability but, with broad sudo, not
containment. So the real posture today is *"audited root daemon."*

## What this branch turns it into

*"An agent confined to a vetted verb set, continuously measured, and revocable —
with the controller as the authoritative audit + authorization anchor."*

The arc, each leg tested:

| Leg | What | Where | Tested |
|---|---|---|---|
| **Confine** | One root-owned dispatcher is the *only* thing the agent may sudo; it runs vetted verbs (writes + reads), argv-only, every arg validated | `host_agent/sysible_priv.py` | ✅ unknown verbs / path traversal / injection refused |
| **Model** | Common locked `sysible` user; non-root reads run as it; all root via the dispatcher; **one-line sudoers**; controller holds the authoritative signed-token audit. Per-user `runuser` demoted to optional defense-in-depth | RFC + `agent.py` | ✅ design |
| **Prove** | A real action (`service.restart`) runs builder → `kind="op"` task → `run_op()` → dispatcher → `systemctl`, **no `bash -c`** | `agent.py`, `client/_api_users.py` | ✅ end-to-end chain |
| **Detect** | Agent self-measures its files into the heartbeat; controller compares to a sealed baseline and **quarantines** (no tasks) on mismatch | `agent_integrity.py` (both) | ✅ tamper + version skew quarantine; rebaseline clears |
| **Lock out** | Revoke the agent secret → every authenticated request 403s until re-enroll; superuser API + Qt GUI button + red list flags | `db.py`, `app.py`, `host_enrollment_page.py` | ✅ revoke locks out, re-enroll restores |
| **Harden the anchor** | Because the controller is now the sole trust+audit anchor: token signing, tamper-evident logs, mTLS, per-verb/host scoping | RFC section | ⬜ documented, not built |

## Honest gaps (prototype → production)

- **Builders don't emit verbs yet** — only the `service.restart` slice is wired.
  Verb-ifying the privileged subset (~40–55 verbs across ~13 families, inventory
  in the RFC) is the bulk of the real work.
- **Sudoers/bundle not flipped** — the dispatcher is feature-flagged (`SYSIBLE_PRIV`);
  `agent_bundle.py` still ships `NOPASSWD: ALL`. Flipping it to the single-binary
  line is the step where the posture *actually* changes.
- **Integrity sealing is trust-on-first-use** — production should seal the
  baseline from the exact bytes the controller ships in the bundle.
- **No Tier-2 (TPM) attestation** — Tier 1 is forgeable by host root; only TPM
  defends against that. Opt-in, out of scope here.
- **Controller hardening** is documented, not implemented — and it matters more
  now that the common-user model leans on the controller.
- **Web-console parity** — revoke + integrity flags exist in the Qt GUI only.

## Everything is non-breaking

The dispatcher and per-user routing are behind `SYSIBLE_PRIV` (unset = today's
behaviour). Integrity is additive (optional `measurements`, guarded import,
`evaluate()`/heartbeat never raise; TOFU seal is non-disruptive). Revocation is a
new flag + endpoint. `kind="op"` rides the existing task model. So `dev` behaviour
is unchanged unless these are explicitly enabled.

## How to review / re-run the checks

- Read [`PRIVILEGE_DISPATCHER_RFC.md`](PRIVILEGE_DISPATCHER_RFC.md) for the design + honest limits.
- Dispatcher validation, op end-to-end, integrity flow, and revoke flow each have
  a runnable check in the commit messages (`git log origin/dev..HEAD`).
- `python -m py_compile` passes for all touched Python.

## Decisions for you

1. **Adopt the common-user-first model?** (vs keeping per-user `runuser` primary.)
2. **Which verb families first**, and is `queue_op_on_hosts` the migration shape you want?
3. **Integrity enforcement default** — quarantine-only (current) vs opt-in auto-revoke.
4. **Controller-hardening priority** — it's the load-bearing trust anchor now.

## Recommended path

1. Land the **controller hardening** basics first (it's the anchor everything leans on).
2. Verb-ify **one full family** (`service.*`) and flip its builders to `queue_op_on_hosts` — a complete vertical you can run on a real host.
3. Switch `agent_bundle.py` to the single-binary sudoers behind a deploy flag; pilot on a test host.
4. Move integrity sealing to **bundle-sealed** manifests; add web-console parity.
5. Expand verbs family by family; keep `NOPASSWD: ALL` only until coverage is complete.
