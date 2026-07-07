# RFC archive

Design/decision documents for approaches that were explored and **not adopted
as-is**, preserved here so the reasoning survives even after the prototype
branches are deleted. These describe roads considered, not the shipped product —
treat them as historical context, not documentation of current behaviour.

## `PRIVILEGE_DISPATCHER_RFC.md` + `AGENT_SECURITY_RFC_OVERVIEW.md`

From the `claude/priv-dispatcher-rfc` prototype branch (now deleted). The RFC
proposed moving the agent off "audited root daemon" (root, or `sysible` with
`NOPASSWD: ALL`, escalating via `sudo bash -c "<arbitrary shell>"`) onto a
**confined privilege dispatcher**: a single root-owned binary that is the only
thing the agent may `sudo`, running a vetted, argv-only verb set with every
argument validated, plus a common locked `sysible` user and a one-line sudoers
entry.

**Outcome — not adopted:** the dispatcher / common-user privilege model was not
merged. The shipped agent keeps the per-user run-as model (`runuser` as the
triggering administrator) with the controller as the authoritative audit anchor.
Verb-ifying the entire privileged surface (~40–55 verbs) was the bulk of the work
and the posture only actually changes once the sudoers line is flipped, which was
judged too large/risky a change for the value versus the run-as model already in
place.

**What *did* ship from this line of thinking** (built separately, not from this
branch): agent integrity self-measurement with controller-side quarantine, and
agent-secret revocation / hard lock-out. The dispatcher itself, the common-user
model, and the (since-removed) Qt desktop-GUI surfaces did not.

Kept for the design write-up, the honest limits section, and the privileged-verb
inventory, in case the confinement approach is revisited for the Enterprise
edition.
