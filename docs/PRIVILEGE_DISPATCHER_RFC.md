# RFC: Command-allowlist privilege dispatcher for the Sysible agent

**Status:** prototype (branch `claude/priv-dispatcher-rfc`) — not for merge as-is.
**Goal:** confine the agent's root access to a vetted, argument-validated verb
set instead of blanket passwordless sudo, so neither a compromised agent nor a
compromised controller can obtain arbitrary root on a managed host.

## The model (canonical)

```
 controller ──signed task (verb + args)──▶ sysible-agent (runs as 'sysible')
   │                                              │
   │  authoritative audit:                        ├─ non-root read  → run as 'sysible' directly
   │  who/what/when, unspoofable                  └─ anything root  → sudo -n sysible-priv op --op <verb> ...
   │  (signed login token)                                              │
   ▼                                                                    ▼
 system of record                                         /etc/sudoers.d/sysible-agent:
                                                          sysible ALL=(ALL) NOPASSWD: /opt/sysible-agent/priv/sysible-priv
```

Four decisions, taken together:

1. **One common identity.** The agent runs as a dedicated, locked `sysible`
   system account (not root, not per-admin). Non-root read commands run as
   `sysible` directly. This drops the operational cost of needing every admin
   to exist on every host.
2. **The controller holds the authoritative audit.** Every action is attributed
   to the initiating admin via their signed login token — unspoofable by the
   client. This is the system of record for "who did what."
3. **All root goes through one dispatcher.** `sysible`'s sudo policy permits
   exactly one program, `sysible-priv`, which runs only vetted verbs with
   validated arguments. This is where the security actually lives.
4. **The host enforces its own ceiling.** Because root is reachable only through
   the verb table, even a *compromised controller* can't send `bash -c
   "<anything>"` and get root — it can only invoke verbs the host re-validates.
   Defense-in-depth against agent *and* controller compromise.

## Why the sudo policy is one line (and not a binary allowlist)

There are two ways to express "`sysible` may only do the controller's tasks."
Only one is safe:

**❌ Leaky — a hand-written binary allowlist:**
```
sysible ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/sbin/useradd, /usr/bin/dnf, ...
```
Looks locked down, isn't: `systemctl` can run an arbitrary unit, `useradd
--shell /bin/bash -G sudo` makes a root-capable account, `dnf` runs scriptlets,
etc. sudoers arg-matching is too weak to constrain these, and it can't handle
the compound read pipelines at all.

**✅ Enforceable — allowlist exactly the dispatcher:**
```
sysible ALL=(ALL) NOPASSWD: /opt/sysible-agent/priv/sysible-priv
```
- **sudoers** = "`sysible` may run *only* the dispatcher" (one line, trivially auditable).
- **dispatcher** = "...and only these vetted verbs, with these validated args"
  (the real policy, in reviewable code with tests — argv-only, no shell, path
  allowlists — which sudoers fundamentally cannot do).

The point is to move the policy from an unenforceable sudoers file into an
enforceable program.

## Inventory: the verb table is tractable

Most of the 326 builder functions are **read-only and run unprivileged**, so the
allowlist only covers the subset that needs root. Sweeping the builder layer,
the privileged surface collapses to **~13 families / ~40–55 verbs**, because the
high counts are the same operation across backends:

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
| Privileged reads | tail of `/var/log/*`, journalctl (system), protected configs | `log.read`, `journal.read`, `config.read` (named-source allowlists) |
| SELinux / kernel / grub / sudoers | semanage 9, sysctl 7, setsebool, update-grub, visudo | `selinux.*`, `sysctl.set`, `grub.update` |

**The hard tail:** generic file mutations (`chmod 17, mkdir 18, cp 16, sed -i 10,
mv 9, chown 4, rm -rf 3, tee, ln`) → a **path-allowlisted `file.write` /
`file.chmod`**, not a free path. And note **privileged reads** are arbitrary
shell today (`tail /var/log/secure`, `journalctl -u sshd`) — they must become
verbs too, scoped to named sources so a compromised controller can't
`log.read source=/etc/shadow`.

## The dispatcher

`sysible-priv` (root-owned, `0755`, not writable by `sysible`) exposes:

- **`op --op <verb> [--arg k=v ...]`** — the primary path. Looks the verb up in a
  fixed table and runs it argv-only (never a shell), validating every argument
  first. Secrets (passwords, file contents) arrive on **STDIN**, never argv/env.
  Covers privileged writes *and* reads. Unknown verb ⇒ refused.
- **`runas --user U --mode {plain|elevate} -- <shell>`** — the **optional**
  per-user mode (below). Off by default.

## Optional: per-user `runuser` (defense-in-depth mode)

The original per-user model — `runuser` into the triggering admin's local
account — is demoted to an **opt-in mode** for shops that want more than the
common-user baseline. When enabled it adds:

- **Host-side dual audit** — the host's own `auth.log`/sudo logs independently
  name the real admin, so accountability survives even if the controller's log
  is unavailable or untrusted.
- **Per-host least-privilege** — the host's own per-user sudo policy can cap what
  each admin may do, which the controller cannot override.

It costs an account-per-admin-per-host and only delivers containment if those
per-user sudo policies are actually constrained (if every admin has
`NOPASSWD: ALL`, it buys accountability, not containment). Hence: opt-in, not
the default.

## Controller as trust anchor — what to harden

The common-user model deliberately makes the controller the **single
authoritative audit + authorization anchor**. The dispatcher caps what any host
will *do*, but the controller decides *which* verbs get dispatched and holds the
only record of intent. So harden it as the crown jewel:

- **Admin auth & token signing** — the run-as token must be unforgeable and
  short-lived (it is: signed, 12h). Protect the signing material; rotate it.
- **Audit-log integrity** — make the activity log tamper-evident (append-only /
  off-box shipping), since with the common-user model it's the primary record.
- **Transport** — mTLS controller↔agent, not just server-auth TLS, so a host
  authenticates the controller and vice-versa; pin/rotate the agent secret.
- **Per-host / per-verb scoping** — let a token authorize only certain verbs or
  hosts, so a stolen token or rogue admin can't drive the whole fleet.
- **Controller host hardening** — it holds the API key, SSH keys for agentless
  hosts, and the dispatch channel (a fleet-wide RCE path by design). Treat its
  compromise as fleet compromise and defend accordingly.

## Agent integrity & quarantine

"Monitor the agent; if it's messed with, disable it." Same honesty as above:
**a process can't reliably attest to its own integrity on a host an attacker
controls** — with root, you can edit the agent, edit its checker, and forge a
clean report. So two tiers, and enforcement on the controller:

- **Tier 1 (achievable now):** detect accidental corruption, drift, version
  skew, swapped files, and tampering by a *non-root* actor or a compromised
  *non-root* agent process.
- **Tier 2 (hardware root of trust):** detect tampering by an attacker with
  root on the host — TPM remote attestation (measured boot / IMA-EVM quote the
  controller verifies). The only thing robust against host-root; opt-in.

**Prerequisite:** this is only meaningful *on top of the dispatcher*. Today the
agent is root-equivalent and can rewrite itself + its checker, so monitoring
would be theatre. Once the agent runs as the locked `sysible` user and can only
escalate through vetted verbs, it can't modify its own code — so a Tier-1
mismatch means something with real root touched the files.

**Layered design**

1. **Prevent.** Agent files root-owned, not writable by `sysible`, ideally
   `chattr +i`; systemd hardening (`ProtectSystem=strict`, `ReadOnlyPaths`).
   With the dispatcher, a compromised agent *process* can no longer rewrite its
   own code or escalate to do so.
2. **Detect (Tier 1).** The agent attaches a **self-measurement manifest**
   (sha256 of its files + version) to its existing heartbeat. The **controller**
   compares it to the host's **sealed baseline** and decides — raw measurements,
   controller judges, not agent-self-judged.
3. **Detect (Tier 2).** TPM quote in the heartbeat, controller-verified. Opt-in.
4. **Enforce — controller-side ("disable").** On mismatch the controller
   **quarantines** the host (keeps heartbeating, handed **no tasks**) and can
   **revoke its `agent_secret`** (locks it out until re-enrolled) and alert.
   Agent self-stop is a secondary honest-failure layer, not relied on against a
   root attacker.

**Enforcement belongs on the controller** — the trust anchor the attacker
doesn't control. "Disable" stops the host *acting through Sysible*; it can't
clean the box. A confirmed Tier-2 failure ⇒ quarantine → alert → **reimage**.

**Prototype (this branch):**
- `host_agent/agent_integrity.py` — `measure()`: hashes the agent's own files
  (`agent.py`, `agent_integrity.py`, `sysible-priv`) + version; pure, never
  raises. Attached to the heartbeat behind a guarded import (non-breaking).
- `backend/agent_integrity.py` — seals a per-host baseline **trust-on-first-use**,
  `compare()`/`evaluate()` flag mismatches, `is_quarantined()`, `rebaseline()`
  for legitimate upgrades. JSON side-store, mirroring `agent_ssh_state.json`.
- Wired additively: optional `measurements` on `HeartbeatRequest`; the heartbeat
  endpoint evaluates; `poll_agent_tasks` returns **no tasks** for a quarantined
  host. Tested end-to-end: clean passes, a changed file hash and a version skew
  both quarantine, and rebaseline clears it.
- **Production refinements (not in prototype):** seal the baseline from the exact
  files the controller *ships in the bundle* (stronger than trust-on-first-use);
  secret revocation + an admin "integrity failed / rebaseline" surface in the
  UI; clone/replay detection via monotonic heartbeat sequence numbers.

## Honest limits

1. **The validators are the boundary.** Any verb that puts a user-controlled
   string into a shell rebuilds the hole. Argv-only, strict validation, no
   `shell=True` — enforced in `sysible_priv.py`.
2. **The verb set is inherently powerful.** A fleet tool must create users,
   install packages, set passwords. So a compromised controller, even confined
   to the verbs, can still do real damage (`user.create` + `user.setpassword` ≈
   a backdoor account). The dispatcher turns "instant arbitrary root RCE on
   every host" into "the management verbs with validated args" — a big
   reduction, not total neutralization. Controller hardening still matters.
3. **It is a genuine refactor**, not a patch: agent + installer + the privileged
   slice of the builders, plus this audited dispatcher and tests.

## Phased migration

1. **Inventory** which actions escalate (the privilege-error path already marks
   them). *(Started: table above.)*
2. **Build `sysible-priv`** with the verb table (writes + reads) and the
   optional `runas` mode. *(Prototype in this branch; argv-only, validated,
   unit-tested.)*
3. **Route** the privileged subset through `op`; non-root reads run as `sysible`.
4. **Tighten sudoers** to the single binary — *this* is the moment the posture
   changes.
5. **Harden the controller** (section above) in lockstep, since it's now the
   sole trust anchor.
6. *(Optional)* offer per-user `runuser` mode for defense-in-depth deployments.

## Prototype status (this branch)

- `sysible_priv.py`: `op` with a representative verb per family — writes
  (`service.*`, `user.create`, `user.setpassword`, `pkg.{install,remove}`,
  `power.*`, path-allowlisted `file.write`) and reads (`log.read`,
  `journal.read`, named-source allowlists). Plus the optional `runas` mode.
  Argv-only; validators unit-tested (unknown verbs, path traversal, arbitrary
  log sources, and shell-injection in unit/package/journal args are all
  refused).
- `agent.py`: `_run_via_dispatcher` + a `SYSIBLE_PRIV` feature flag; default
  unset ⇒ no behaviour change.
- **Not** wired into `agent_bundle.py`/sudoers, and the builders do **not** yet
  emit verbs — that, plus the controller hardening, is the remaining work.
