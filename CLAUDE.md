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
never one build per tweak. (Full detail lives in `sysible-linux/CLAUDE.md`.)
