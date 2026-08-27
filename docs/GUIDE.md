# Sysible Controller — User Guide

*A point-and-click, fleet-wide operations console for Linux — agent or SSH, no DSL
to learn.*

This is a **walkthrough**: get a controller up, put your first host under
management, do the day-to-day work, and switch deployment modes when you need to.
For the full capability list see the [README](../README.md); for containers see
[DOCKER.md](../DOCKER.md); for backup/restore see
[BACKUP_RESTORE.md](BACKUP_RESTORE.md).

---

## Contents

1. [What it is](#1-what-it-is)
2. [The pieces and their ports](#2-the-pieces-and-their-ports)
3. [Install and first sign-in](#3-install-and-first-sign-in)
4. [Deployment modes (and switching between them)](#4-deployment-modes-and-switching-between-them)
5. [Add your first host](#5-add-your-first-host)
6. [Environments and sudo policy](#6-environments-and-sudo-policy)
7. [Daily work](#7-daily-work)
8. [Terminals and file transfer (Sysible Connect)](#8-terminals-and-file-transfer-sysible-connect)
9. [Using it with SLEP](#9-using-it-with-slep)
10. [The `sysible_controller` CLI](#10-the-sysible_controller-cli)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What it is

One controller on one Linux machine gives you a single console over an entire
fleet — users/groups, health, services, packages, networking, firewall, storage,
scheduled jobs, and live terminals — whether each host runs the **Sysible agent**
or is reached over **direct SSH**. Every action runs **as the administrator who
triggered it** (not a faceless root daemon), so the host's own sudo policy and
audit trail stay meaningful. No DSL, no control repo, no apply step — every action
is a button.

---

## 2. The pieces and their ports

| Piece | Port | What it is |
|---|---|---|
| **Backend API** | `9000` | The control plane. Agents check in here; SLEP and the CLI talk to it; it holds inventory, credentials, and the task queue. Gated by the machine **API key**. |
| **Web console** | `8800` | The browser UI (`sysible-webgui`) — dashboard, tools, terminals. Talks to the backend over HTTPS; the API key stays server-side. |
| **Webserver Portal** | `8090` | *Optional, off by default.* A self-service page for **host operators** (not admins) to fetch the agent bundle or exchange files — no shell/admin access needed. |

The datastore is **SQLite** (CE) in the data directory / volume. All three are
HTTPS with a self-signed cert generated at first start; agents pin that cert.

---

## 3. Install and first sign-in

**Standalone (systemd):**
```bash
sudo ./install_sysible.sh
sudo sysible_controller start           # backend on :9000
sudo sysible_controller webgui start    # console on :8800
```

**Container:**
```bash
sudo ./start-container.sh               # builds from the current checkout, sets the LAN address
# or: docker compose up -d --build      # see DOCKER.md
```

Then open `https://<host>:8800/` and complete the **first-run screen** to create
your administrator account (you choose the password). Your browser warns once
about the self-signed cert — proceed and it's remembered. To drop the name-mismatch
warning, put your host's IP/DNS in the cert (`SYSIBLE_CONTROLLER_HOSTNAMES`, or
`start-container.sh` does it for you).

On first start the controller also **enrolls itself** as a managed host, so it
shows up in the fleet like any other box (opt out with `SYSIBLE_NO_SELF_ENROLL=1`).

---

## 4. Deployment modes (and switching between them)

The controller runs **either** as standalone systemd services **or** as
containers — never both at once, because they compete for the same ports
(`:9000`, `:8800`, `:8090`).

| | Standalone | Container |
|---|---|---|
| Install | `install_sysible.sh` → `/opt/sysible` | `start-container.sh` / compose |
| Runs | `sysible-backend` + `sysible-webgui` (systemd) | `controller` + `webgui` containers |
| Update | `sudo sysible_controller update` | re-run `start-container.sh` (rebuilds) |
| Code lives in | `/opt/sysible` (rsynced snapshot, no `.git`) | the image (rebuilt from your checkout) |

> **Standalone gotcha:** the service runs the code in `/opt/sysible`, which the
> installer **rsyncs** from your checkout. A `git pull` in your checkout does *not*
> change `/opt/sysible` — run `sudo sysible_controller update` (or re-run the
> installer) so the snapshot is refreshed, then it restarts to load it.

### Switching container → standalone (or the reverse)

This is the one that bites: **retire the old side first, or two controllers fight
over `:9000`** and clients hit whichever wins (often the wrong one).

**Going standalone → stop the containers:**
```bash
# find the old stack, bring it down (leave unrelated containers alone)
docker ps
sudo docker compose -f <compose-dir>/docker-compose.yml down
# then start standalone
sudo sysible_controller start && sudo sysible_controller webgui start
```

**Going container → stop the standalone services:**
```bash
sudo systemctl disable --now sysible-backend sysible-webgui
sudo ./start-container.sh <lan-ip>
```

**Verify one controller owns the port** (the LAN IP, not just loopback):
```bash
sudo ss -ltnp 'sport = :9000'     # should show exactly one process
```
A classic symptom of a missed switch: `curl https://127.0.0.1:9000/...` works but
`curl https://<lan-ip>:9000/...` returns something stale — a leftover container is
intercepting LAN-IP traffic via its published port while loopback reaches the other
process. **One controller per port.**

---

## 5. Add your first host

Two transports, mixed freely in one fleet:

### Agent (recommended for hosts you own)
*Host Enrollment → build a bundle*, or copy the **`curl` one-liner** that
downloads + installs the agent on a headless host in one shot (via the Portal).
The agent is a small systemd daemon that heartbeats out to the controller and
polls for work — so it works through NAT/firewalls with **no inbound SSH**. Runs as
root by default; `./run_agent.sh --unprivileged` instead runs it as a locked
`sysible` account with passwordless sudo (actions go through a sudo audit trail).

Bundles carry a **single-use enrollment token**, so a leaked bundle can't silently
enroll a second host. Enrolling the **same machine twice is prevented** (dedupe by
IP).

### Direct SSH (for appliances / no-daemon hosts)
*Sysible Connect → SSH-enroll a new host.* Give the controller the password once;
it generates and installs its own key pair, then drives the host over SSH from then
on (including a live terminal). The password is discarded after the first connect.

Both paths feed the exact same fleet-wide tools — day to day you don't think about
which transport a host uses.

---

## 6. Environments and sudo policy

**Environments** (Production, Staging, Dev, …) group hosts. On the **Host
Enrollment** page, select several hosts and assign them at once; create/remove
environments (an environment can't be removed while hosts are still in it). New
hosts inherit their environment's defaults.

**Sudo policy** is per host (or defaulted per environment): **passwordless
(`NOPASSWD`)** or **password-required** ("become") sudo. For password-sudo hosts, a
superuser can grant an administrator the **Send sudo password** action (Settings →
Administrators) so their stored password is typed at the prompt. Privileged steps
are tried unprivileged first and escalated only when the OS reports a privilege
error.

---

## 7. Daily work

The **System Administration** panel is eighteen point-and-click tools, each acting
across checked hosts with per-host result tabs:

- **Users & Groups** — create/lock/delete, sudo & group membership, password aging.
- **System Health, Logs & Recovery** — disk/memory/CPU, failed services, log
  search, process kill/renice, GRUB/kernel recovery.
- **Services** — start/stop/enable, troubleshoot failed units, create new ones.
- **Host Software** — detect the package manager (`dnf`/`yum`/`zypper`/`apt`),
  install/remove/update, upload a local `.deb`/`.rpm` and install fleet-wide.
- **Cron & Timers**, **Networking**, **Firewall & Security**, **Storage**, and more.

The **Dashboard/Fleet** view rolls up live **health** (OK/Warning/Critical) and a
read-only **compliance & posture** scan per environment, worst-first and expandable
to hosts; **Performance** gives environment-first time-series charts fed by the
agent's heartbeat. **Find any action by name** with the dashboard search box.

---

## 8. Terminals and file transfer (Sysible Connect)

*Sysible Connect* is the unified host list with grouped actions. Check hosts and
**Open Terminal** (or double-click one) — each pops out into its own window, with
multiple concurrent sessions per host, opened **as your administrator user** (green
prompt; red for root):

- **Agent host** — the shell is hosted by the **agent** (a local PTY streamed back
  over its outbound channel), so it works even when the controller can't reach the
  host directly.
- **Pure-SSH host** — a real SSH PTY.

Each terminal has file transfer (through the agent as your account, or SFTP for
SSH hosts — superuser-only), find-in-output, save output, Send Ctrl+C, and font
size. **Fleet Actions** run a script or reboot/power-off across the fleet (whole-
fleet reboot/power-off requires typing the word to confirm).

---

## 9. Using it with SLEP

The **Sysible Linux Engineering Platform (SLEP)** provisions VMs and then **enrolls
them into this controller**. In SLEP, connect a Controller (its backend API at
`https://<host>:9000`, by password or API key — SLEP trusts the self-signed cert on
first use), then Enroll: SLEP downloads a one-time **agent bundle** from
`/remote/agent-bundle` and installs it on each VM, which self-enrolls here. The VMs
then appear in your fleet like any other agent host. (This requires a controller
build that serves `/remote/agent-bundle` — keep the deployment current per §4.)

---

## 10. The `sysible_controller` CLI

```
sysible_controller {start|stop|restart|update|status|logs|webgui|reset-admin|self-enroll|disenroll|destroy}
```

- `update` — pull the recorded source checkout, redeploy to `/opt/sysible`,
  restart (standalone one-command update).
- `status` / `logs` — service state + console health probe / tail the backend.
- `reset-admin` — recover console access if you're locked out.
- `self-enroll` — (re)add the controller host itself to the fleet.

---

## 11. Troubleshooting

**Console won't load / locked out.** `sudo sysible_controller status` (is
`sysible-backend` + `sysible-webgui` up?), `logs` to see why; `reset-admin` to
recover the admin account.

**A host won't enroll (agent).** Confirm the host can reach the controller's
**`:9000`** outbound, and that enrollment isn't paused / the token isn't spent
(bundles are single-use — fetch a fresh one per host).

**SLEP enrollment 404 (`no agent-bundle route`).** The running controller doesn't
serve `/remote/agent-bundle` — it's an older build, or the deployment wasn't
refreshed. Update it (§4). If containerized, rebuild the image from current code;
if standalone, `sysible_controller update` then restart.

**SLEP enrollment: self-signed cert error.** Expected once — SLEP trusts it on
first use. If it persists, the controller may be unreachable at that address, or
two services answer the port (§4, one controller per port).

**Changes didn't take effect.** Standalone runs the `/opt/sysible` snapshot, not
your checkout — `sysible_controller update`. Containers run the image — rebuild
(`start-container.sh`). Either way, **restart** so the process reloads.

**Reached as two hosts / port conflict after a mode switch.** See §4 — retire the
old side and confirm one process owns `:9000`.

---

*See also: [DOCKER.md](../DOCKER.md) (containers), [BACKUP_RESTORE.md](BACKUP_RESTORE.md),
and [`webgui/README.md`](../webgui/README.md) (console architecture / TLS).*
