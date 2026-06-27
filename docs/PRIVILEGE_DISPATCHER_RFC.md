# RFC: Command-allowlist privilege dispatcher for the Sysible agent

**Status:** prototype (branch `claude/priv-dispatcher-rfc`) — not for merge as-is.
**Goal:** replace the agent's blanket `NOPASSWD: ALL` sudo grant with a single
sudo-allowlisted dispatcher, so a compromised agent can invoke only vetted,
argument-validated operations instead of arbitrary root shell.

## Problem

Today the unprivileged agent account is granted:

```
sysible ALL=(ALL) NOPASSWD: ALL
```

and the agent escalates by running `sudo bash -c "<string>"`. Because the
command builders emit **compound shell pipelines** (e.g.
`df -hT ... | awk '...'`, `mem_pct=$(...); if ...; echo ... && ps ...`,
`find / ... | sort | head | awk`), there is no fixed `binary + args` to
allowlist — `bash -c` is the command, and allowlisting it is allowlisting
everything. So the dispatcher is an **architecture change to the escalation
path**, not a sudoers edit.

## Inventory (what actually needs root)

Most of the 326 builder functions are **read-only and run unprivileged** —
`_run_as_user` tries every command as the target user first and only escalates
on a privilege error. So the allowlist only needs to cover the subset that
truly escalates. Sweeping the builder layer for privileged primitives, the
surface collapses to **~13 families / ~40–55 verbs**, because the high counts
are the same operation across backends:

| Family | Primitives seen (occurrences) | Verbs |
|---|---|---|
| Packages / repos | zypper 106, apt-get 73, dnf 63, yum 58, rpm/dpkg/snap | `pkg.{install,remove,update}`, `repo.*` |
| Mount / fstab | mount 116 (much read-only `findmnt`) | `fs.{mount,unmount}`, `fstab.*` |
| Services | systemctl 97 (much read-only `status`) | `service.{start,stop,restart,enable,disable,mask}` |
| Firewall | iptables 28, firewall-cmd 24, nft 10, ufw 5 | `firewall.*` (one verb set, N backends) |
| Network | nmcli 35, ip route | `net.*` |
| Storage / LVM | parted 27, lvcreate/vgcreate/pvcreate, mkfs, cryptsetup | `storage.*` |
| Users / groups | passwd 9, usermod 7, chage 5, useradd/userdel/groupadd | `user.*`, `group.*` |
| Time / host | timedatectl 10, hostnamectl 3, localectl | `system.*` |
| Power | reboot 17, shutdown 10, poweroff 2 | `power.*` |
| Cron | crontab 19 | `cron.*` |
| SELinux / kernel / grub / sudoers | semanage 9, sysctl 7, setsebool, update-grub, visudo | `selinux.*`, `sysctl.set`, `grub.update` |

**The hard tail:** generic file mutations used as building blocks —
`chmod 17, mkdir 18, cp 16, sed -i 10, mv 9, chown 4, rm -rf 3, tee, ln`.
These take arbitrary paths/content and resist a clean verb; they map to a
**path-allowlisted `file.write` / `file.chmod`** instead (see below).

## Design

A single root-owned program, `sysible-priv`, is the **only** thing the agent
account may run under sudo:

```
sysible ALL=(ALL) NOPASSWD: /opt/sysible-agent/priv/sysible-priv
```

It exposes two subcommands:

- **`runas --user U --mode {plain|elevate} -- <shell>`** — drop to user `U`
  and run the shell string **as that user**. This is the read / user-level
  path (the bulk of commands). It is *not* a root boundary: whatever `U` can
  do via their own sudo, they can still do. The boundary for this path remains
  **each user's local sudo policy** — we've only centralised it so the agent
  no longer needs `sudo runuser`/`sudo bash` of its own.
- **`op --op <verb> [--arg k=v ...]`** — run one vetted root primitive from a
  fixed table, **argv-only (never a shell)**, with every argument validated
  first. Unknown verb ⇒ refused. This is the only way to reach root through
  the dispatcher.

Secrets (a user's become password, file contents) arrive on **STDIN**, never
argv/env, so they never appear in `ps`/logs.

The security boundary becomes: (1) the agent can sudo *only* this binary;
(2) root primitives are confined to the verb table; (3) per-verb validators
(`v_unit`, `v_user`, `v_pkgs`, `v_write_path`, …) gate every argument.

## What changes

| File | Change |
|---|---|
| `host_agent/sysible_priv.py` | **New.** The dispatcher (this prototype seeds one verb per family + the `runas` path). |
| `host_agent/agent.py` | When `SYSIBLE_PRIV` is set, `_run_as_user` routes through the dispatcher (`_run_via_dispatcher`) instead of `sudo runuser`/`sudo bash`. Unset ⇒ today's behaviour, unchanged. |
| `backend/agent_bundle.py` | Ship `sysible-priv` into `/opt/sysible-agent/priv/` (root:root, 0755); replace the `NOPASSWD: ALL` sudoers line with the single-binary allowlist; set `SYSIBLE_PRIV` in the unit env. *(Not done in this branch — documented here to keep the prototype non-breaking.)* |
| command builders (`_api_*`, `webgui/actions.py`) | The **privileged** subset emits `{op, args}` verbs instead of inline shell. Read-only pipelines stay as shell strings (run unprivileged via `runas`). |

## Honest limits (so this is real, not theatre)

1. **The validators *are* the boundary.** Any verb that shoves a
   user-controlled string into a shell rebuilds the hole. Argv-only, strict
   validation, no `shell=True` — enforced in `sysible_priv.py`.
2. **`runas` still runs arbitrary shell as the target user.** A compromised
   agent can still become any local user via the dispatcher. So this only pays
   off if **per-user sudo policies move in tandem** (constrained, ideally
   password-sudo). Tightening the agent's sudoers while every admin account has
   `NOPASSWD: ALL` just relocates the open door — the two halves are one
   boundary.
3. **It is a genuine refactor**, not a patch: agent + installer + the
   privileged slice of the builders, plus this audited dispatcher and tests.

## Phased migration

1. **Inventory** which actions escalate (the privilege-error path already
   marks them — instrument it empirically). *(Started: see table above.)*
2. **Build `sysible-priv`** with the `runas` drop + an initial verb table.
   *(Prototype in this branch; argv-only, validated, unit-tested.)*
3. **Route** the privileged subset through `op`; leave read pipelines as
   `runas`.
4. **Tighten sudoers** to the single binary — *this* is the moment the posture
   changes.
5. **Constrain per-user sudo** in lockstep, or document it as a deployment
   requirement.

## Prototype status (this branch)

- `sysible_priv.py`: working `runas` + a representative verb per family
  (`service.*`, `user.create`, `user.setpassword`, `pkg.{install,remove}`,
  `power.*`, path-allowlisted `file.write`). Argv-only; validators unit-tested
  (unknown verbs, path traversal, and shell-injection in unit/package names
  are all refused).
- `agent.py`: `_run_via_dispatcher` + a feature flag (`SYSIBLE_PRIV`). Default
  unset ⇒ no behaviour change.
- **Not** wired into `agent_bundle.py`/sudoers, and the builders do **not** yet
  emit verbs — those are the bulk of the remaining work, scoped above.
