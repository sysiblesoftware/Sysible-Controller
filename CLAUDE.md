# Working agreement

## 🚫 NEVER build an ISO unless explicitly told to — HARD RULE
Do **NOT** trigger a Sysible Linux ISO build under any circumstances unless the user, in
that same message, explicitly tells you to build / kick / cut / rebuild an ISO (e.g. running
the `iso.yml` workflow / `actions_run_trigger` / `workflow_dispatch`).

Committing and pushing changes is fine. **Building the ISO is not** — wait to be told. Do not
infer permission from "test it", "make a release", or the fact that you built one earlier.
If you think an ISO is needed, **ask first and stop**. When in doubt, do not build.

For boot/desktop cosmetics: lock the look with **mockups first** (render at the real
1024×768 boot resolution), get sign-off, and batch everything into ONE ISO at the end —
never one build per tweak. (Full detail lives in `sysible-workstation/CLAUDE.md`.)

## 🧊 An ISO build FREEZES the work — HARD RULE
The moment an ISO build is triggered, it builds **the commit that was on `main` at that
second**. Anything committed afterwards is not in it. So:

**Before triggering:** finish and push everything that belongs in the ISO. The build is the
*last* thing, not something to start while work is still in flight.

**After triggering, do not commit anything that goes into an ISO** — no app code, no
`live-build/` files, no hooks, no installer, no compose. Wait for the build to finish.

Why this is a rule and not a preference: a build takes 30–45 minutes, so work done during
it *looks* finished and is not in the artifact the user is waiting for. Exactly that
happened — a Server ISO was cut, then an updater credential fix and a SLEP feature were
committed, and the finished ISO contained neither. The user had to notice from the Actions
list. That is the user doing my job.

If something genuinely must change mid-build (a broken build, a security fix), **say so and
stop**. Tell the user the build is now stale, what is missing, and let them decide whether
to cancel, let it finish, or rebuild. Do not quietly keep committing and do not rebuild on
your own — the rule above still applies.

Tests and docs that cannot reach the ISO are the one exception, and only if you have checked
that they cannot. When unsure, treat it as code and wait.
