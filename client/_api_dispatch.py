"""SYSTEM ADMINISTRATION (dual-host helpers) + System Health & Logs builders -
split out of client/api.py to keep individual file sizes manageable.
Imported via `from client._api_dispatch import *` at the bottom of client/api.py.

The System Administration page needs to run the same action against
a mix of agent-enrolled hosts (async task queue - see FLEET USER
MANAGEMENT) and SSH-enrolled hosts (synchronous one-shot exec - see
REMOTE HOSTS) from a single button click. These helpers merge both
host kinds into one list and hide the two different dispatch
mechanisms behind one call, so a page only has to branch on the
*result shape* it gets back ("sync": True/False), not re-derive
host-kind branching at every call site.
"""
import json
import shlex

from client.api import get_agents
from client._api_users import (
    list_hosts, exec_remote, queue_command_on_hosts, get_result_by_task,
    parse_task_output, build_user_sync_command, sync_hosts,
)


def merge_duplicate_host_entries(entries):
    """Collapse an agent entry and an SSH entry that share the exact
    same hostname into a single display row.

    Without this, the same physical machine enrolled both ways (agent +
    SSH) shows up as two separate, near-identical rows - same name,
    same environment, differing only in Type/Address - which reads as
    a confusing duplicate rather than one host reachable two ways.
    Shared by every page that lists hosts via list_merged_hosts()
    below (System Health & Logs, User & Group Administration, Service
    Management) - originally written for, and still also used directly
    by, Remote Host Administration's own host table."""
    by_label = {}
    order = []

    for e in entries:
        by_label.setdefault(e["label"], []).append(e)
        if e["label"] not in order:
            order.append(e["label"])

    merged = []

    for label in order:
        group = by_label[label]
        agent_entry = next((e for e in group if e["kind"] == "agent"), None)
        ssh_entry = next((e for e in group if e["kind"] == "ssh"), None)

        if agent_entry and ssh_entry:
            merged.append({
                "kind": "merged",
                "id": label,
                "label": label,
                "type_text": "Agent + SSH",
                "address": f"agent: {agent_entry['address']}   |   ssh: {ssh_entry['address']}",
                "environment": agent_entry.get("environment") or ssh_entry.get("environment") or "",
                "agent_entry": agent_entry,
                "ssh_entry": ssh_entry,
            })
            for extra in group:
                if extra is not agent_entry and extra is not ssh_entry:
                    merged.append(extra)
        else:
            merged.extend(group)

    return merged


def _underlying_entry(entry):
    """A "merged" entry (merge_duplicate_host_entries() above) has both
    an agent and an SSH connection for the same physical host - for
    command execution in the System Administration tools, prefer the
    AGENT. The agent runs as root, so privileged actions (apt, systemctl,
    writing under /etc, ...) work; the SSH connection logs in as an
    ordinary user with no sudo, so the same commands fail with
    "Permission denied". (The interactive terminal in Remote Host
    Administration still picks SSH itself - it has its own connection
    picker and does not go through here.) Falls back to SSH only if the
    agent side is somehow missing. Entries that aren't merged pass
    through untouched."""
    if entry["kind"] == "merged":
        return entry["agent_entry"] or entry["ssh_entry"]
    return entry


def list_merged_hosts(agent_only=True):
    """Agent hosts + SSH hosts as one list of dicts: {"kind": "agent"|
    "ssh"|"merged", "id", "label", "type_text", "address",
    "environment"} (a "merged" entry additionally carries "agent_entry"/
    "ssh_entry" - see merge_duplicate_host_entries()) - the same shape
    Remote Administration builds internally for its own host list,
    exposed here so other pages (System Administration) can target
    hosts with duplicates already collapsed, without re-implementing the
    merge.

    agent_only (default True): drop SSH-only hosts, leaving just agent
    and agent+SSH (merged) hosts. The System Administration tools run
    privileged fleet actions that only work through the root-level agent,
    so a host with no agent has no business appearing there - including
    it just produces "Permission denied" failures (the SSH login has no
    sudo). Remote Host Administration, which is where SSH connections are
    actually managed, passes agent_only=False to still see every host."""
    entries = []

    try:
        agents = get_agents()
    except Exception:
        agents = []

    for a in agents:
        host_id = a.get("host_id")
        entries.append({
            "kind": "agent",
            "id": host_id,
            "label": a.get("hostname") or host_id,
            "type_text": "Agent",
            "address": a.get("ip") or host_id,
            "environment": a.get("environment") or "",
            # Controller's SSH-terminal auto-enroll status for this agent
            # host (see backend/remote_routes.py): "enabled" | "pending" |
            # "sshd_missing" | "error" | None. Lets Remote Administration
            # tell the operator when a host can't get a real terminal
            # because no SSH server is running on it.
            "ssh_terminal_state": a.get("ssh_terminal_state"),
            # Host forbids passwordless sudo: dispatch supplies the operator's
            # sudo password (the agent then uses `sudo -S`). See run_on_entry.
            "requires_sudo_password": bool(a.get("requires_sudo_password")),
        })

    try:
        ssh_hosts = list_hosts()
    except Exception:
        ssh_hosts = {}

    for name, h in (ssh_hosts or {}).items():
        entries.append({
            "kind": "ssh",
            "id": name,
            "label": name,
            "type_text": "SSH",
            "address": f"{h.get('user', 'root')}@{h.get('ip', '?')}",
            "environment": h.get("environment") or "",
        })

    merged = merge_duplicate_host_entries(entries)
    merged = _dedupe_same_ip(merged)
    if agent_only:
        merged = [e for e in merged if e["kind"] != "ssh"]
    return merged


def _entry_ip(entry):
    """The IP that identifies the physical machine behind an entry. SSH
    addresses arrive as 'user@ip'; a merged host carries both sub-entries."""
    def ip_of(addr):
        return (addr or "").split("@")[-1].strip()
    if entry.get("kind") == "merged":
        a = entry.get("agent_entry") or {}
        s = entry.get("ssh_entry") or {}
        return ip_of(a.get("address")) or ip_of(s.get("address"))
    return ip_of(entry.get("address"))


def _dedupe_same_ip(entries):
    """Collapse entries that resolve to the SAME physical machine (same IP)
    into one, keeping the richest connection (merged > agent > ssh). Without
    this, a box enrolled twice under different names - e.g. an agent host plus
    a stray manual SSH record at the same IP - shows up as two separate hosts.
    Entries with no usable IP pass through untouched (deduped by name already).
    Order is preserved, with the surviving entry taking the first position its
    IP appeared at."""
    rank = {"merged": 3, "agent": 2, "ssh": 1}
    best = {}
    for e in entries:
        ip = _entry_ip(e)
        if not ip:
            continue
        cur = best.get(ip)
        if cur is None or rank.get(e.get("kind"), 0) > rank.get(cur.get("kind"), 0):
            best[ip] = e

    result = []
    seen = set()
    for e in entries:
        ip = _entry_ip(e)
        if not ip:
            result.append(e)
        elif ip not in seen:
            seen.add(ip)
            result.append(best[ip])
    return result


def run_on_entry(entry, command: str, kind: str = "command", description: str = None,
                 become_password: str = None):
    """Run `command` on one merged-host entry (as produced by
    list_merged_hosts()). SSH executes synchronously over exec_remote()
    - the result is ready immediately. Agent dispatch is async - only a
    task_id comes back, and the caller must poll poll_entry_result()
    until it resolves. Always returns a dict with a "sync" flag so
    callers can branch on which case they got:
      {"sync": True,  "stdout", "stderr", "code", "error"}   (ssh, done)
      {"sync": False, "task_id", "error"}                    (agent, pending)

    `description` is the human label recorded in the controller's activity
    feed (e.g. "Set password for user-tester"); when omitted the controller
    falls back to a summary of the command itself.
    """
    entry = _underlying_entry(entry)

    # Resolve the sudo (become) password.
    #
    # If the caller already supplied one - e.g. the web console resolves it from
    # its controller-side store and passes it in - ALWAYS honour it; never
    # discard it. Only when none was supplied do we fall back to the desktop's
    # workstation-local store, and only for a host flagged password-sudo.
    #
    # The previous code did `else: become_password = None`, which silently
    # zeroed ANY caller-supplied password whenever this entry's flag was
    # missing/stale - and SSH (and merged-resolved-to-SSH) entries never carried
    # the flag. That's exactly what broke the web console: the password was
    # resolved correctly upstream, then thrown away here. Passing a password to a
    # host that doesn't need it is harmless - the agent's `sudo -S` just ignores
    # it under NOPASSWD - so honouring an explicit password is always safe.
    if not become_password and entry.get("requires_sudo_password"):
        try:
            from client import become_credentials
            become_password = become_credentials.get_password(entry.get("label", ""))
        except Exception:
            become_password = None

    if entry.get("requires_sudo_password"):
        # Fail fast with a clear instruction instead of dispatching a command
        # that will just bounce off `sudo` for lack of a password. This host is
        # marked "sudo requires a password" but none is stored for the
        # logged-in admin, so there's nothing for the agent to elevate with.
        if not become_password:
            msg = (
                f"'{entry.get('label', 'this host')}' is set to require a sudo password, "
                "but you haven't stored yours yet. Click “Sudo Password” in the "
                "dashboard header to set it, then try again."
            )
            return {"sync": True, "stdout": "", "stderr": msg, "code": None, "error": msg}

    if entry["kind"] == "ssh":
        try:
            result = exec_remote(entry["id"], command, description=description,
                                 become_password=become_password)
            return {
                "sync": True,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "code": result.get("code"),
                "error": None,
            }
        except Exception as e:
            return {"sync": True, "stdout": "", "stderr": "", "code": None, "error": str(e)}

    task_ids = queue_command_on_hosts([entry["id"]], command, kind=kind,
                                      description=description, become_password=become_password)
    task_id = task_ids.get(entry["id"])
    return {
        "sync": False,
        "task_id": task_id,
        "error": None if task_id is not None else "failed to queue task",
    }


def poll_entry_result(entry, task_id):
    """For an agent entry previously dispatched via run_on_entry() -
    returns the same {"stdout","stderr","code","error"} shape once the
    agent has reported back, or None if it's still pending."""
    entry = _underlying_entry(entry)
    raw = get_result_by_task(entry["id"], task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    return {
        "stdout": output.get("stdout", ""),
        "stderr": output.get("stderr", ""),
        "code": output.get("returncode"),
        "error": None,
    }


def sync_entry_users(entry):
    """Mirrors sync_hosts()/get_sync_result() (agent task-queue) but for
    a single merged entry of either kind. SSH executes synchronously, so
    the parsed {"users","groups","sessions"} dict is ready right away;
    agent dispatch only returns a task_id - poll with
    poll_entry_sync_result()."""
    entry = _underlying_entry(entry)

    if entry["kind"] == "ssh":
        try:
            # log=False: a background user-list read, not an operator action.
            result = exec_remote(entry["id"], build_user_sync_command(), log=False)
        except Exception as e:
            return {"sync": True, "data": None, "error": str(e)}

        if result.get("code") != 0:
            return {
                "sync": True,
                "data": None,
                "error": result.get("stderr") or f"sync exited {result.get('code')}",
            }

        try:
            return {"sync": True, "data": json.loads(result["stdout"]), "error": None}
        except (TypeError, ValueError, KeyError):
            return {"sync": True, "data": None, "error": "could not parse sync output"}

    task_ids = sync_hosts([entry["id"]])
    task_id = task_ids.get(entry["id"])
    return {
        "sync": False,
        "task_id": task_id,
        "error": None if task_id is not None else "failed to queue sync",
    }


def poll_entry_sync_result(entry, task_id):
    """Agent-side counterpart to sync_entry_users()'s synchronous SSH
    branch - returns {"data": {...}, "error": None} once the agent has
    reported back, or None if the result hasn't arrived yet."""
    entry = _underlying_entry(entry)
    raw = get_result_by_task(entry["id"], task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    if output.get("returncode") != 0:
        return {"data": None, "error": output.get("stderr") or "sync failed"}

    try:
        return {"data": json.loads(output["stdout"]), "error": None}
    except (TypeError, ValueError, KeyError):
        return {"data": None, "error": "could not parse sync output"}


# ---------------------------------------------------------
# System Health & Logs (read-only command builders - safe to run on
# either host kind via run_on_entry() above; nothing here ever embeds
# a secret, so there's no password-hashing concern like the FLEET USER
# MANAGEMENT write actions above).
# ---------------------------------------------------------
def cmd_disk_usage() -> str:
    """Human-readable disk usage with the noise filtered out. Drops pseudo
    and read-only filesystems (tmpfs, devtmpfs, overlay, and squashfs/snap
    loop images, optical iso9660/udf) and removable-media mounts under
    /media, /run/media or /cdrom - none of that is real fixed storage and
    it just buries the actual volumes. The System Health page rewrites the
    remaining `df -hT` rows into plain sentences (the header is kept intact
    so that rewrite still matches)."""
    return (
        "df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs "
        "-x iso9660 -x udf 2>/dev/null "
        "| awk 'NR==1 || $7 !~ /^\\/(media|run\\/media|cdrom|snap)(\\/|$)/'"
    )


def cmd_memory_cpu_snapshot() -> str:
    """Leads with a computed "Memory usage: NN% (OK/WARNING/CRITICAL)"
    line, same idea as cmd_health_check()'s disk/load scoring."""
    return (
        "mem_pct=$(LANG=C free -m 2>/dev/null | awk 'NR==2 && $2>0 {printf \"%.0f\", $3/$2*100}'); "
        "mem_status=OK; "
        "if [ -n \"$mem_pct\" ]; then "
        "if [ \"$mem_pct\" -ge 90 ] 2>/dev/null; then mem_status=CRITICAL; "
        "elif [ \"$mem_pct\" -ge 75 ] 2>/dev/null; then mem_status=WARNING; fi; "
        "fi; "
        "echo \"Memory usage: ${mem_pct:-unknown}% ($mem_status)\" && echo "
        "&& echo '== Memory ==' && free -h && echo "
        "&& echo '== Load / Uptime ==' && uptime && echo "
        "&& echo '== Top CPU Processes ==' "
        "&& ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 11"
    )


def cmd_find_large_files(path: str = "/", top_n: int = 20) -> str:
    path = path.strip() if path and path.strip() else "/"
    top_n = max(1, int(top_n))
    return (
        f"find {shlex.quote(path)} -xdev -type f -printf '%s %p\\n' 2>/dev/null "
        f"| sort -rn | head -n {top_n} "
        "| awk '{printf \"%.1f MB  %s\\n\", $1/1024/1024, $2}'"
    )


def cmd_failed_services() -> str:
    """Empty output from `systemctl --failed` is ambiguous - say which
    case it actually is instead of returning nothing."""
    return (
        "if ! command -v systemctl >/dev/null 2>&1; then "
        "echo 'systemctl not available on this host'; "
        "else "
        "out=$(systemctl --failed --no-legend 2>/dev/null); "
        "if [ -z \"$out\" ]; then echo 'No failed services.'; else echo \"$out\"; fi; "
        "fi"
    )


def cmd_uptime() -> str:
    return "uptime"


# ---- Fleet power / agent control (used by Sysible Connect buttons) ----
# These are privileged; under RBAC they run as the operator's matching
# local user and elevate via that host's sudo policy (the agent retries
# under sudo on a privilege error). `shutdown` schedules with init and
# returns right away, so the host can report the result before it goes
# down.
def cmd_reboot_host() -> str:
    # No 2>&1: a privilege failure must stay on stderr so the agent's
    # run-as-user path recognizes it and retries under the host's sudo.
    return "shutdown -r +0 || systemctl reboot"


def cmd_poweroff_host() -> str:
    return "shutdown -P +0 || systemctl poweroff"


def cmd_restart_agent() -> str:
    """Restart the Sysible agent service. Launched detached via systemd-run
    so it survives the agent process (its own parent) being stopped, and
    the host reconnects on the new agent's first heartbeat. Only meaningful
    on agent hosts; SSH-only hosts have no such service and will report
    that."""
    return (
        "systemd-run --collect --unit=sysible-agent-restart systemctl restart sysible-agent "
        "&& echo 'Agent restart dispatched (detached); this host will reconnect shortly.'"
    )


def cmd_metrics_snapshot() -> str:
    """Compact, machine-parseable one-line health snapshot for the dashboard
    fleet overview. Prints a single SYSMETRICS|k=v|... line so the controller
    can parse per-host disk/mem/load/failed + a verdict without scraping the
    verbose health-check text. Plain POSIX sh, cross-distro."""
    return (
        # Worst disk use% among real volumes (skip pseudo/removable/image FS).
        "disk=$(df -P 2>/dev/null | awk 'NR>1 && $1!~/^(tmpfs|devtmpfs|overlay|squashfs)$/ "
        "&& $6!~/^(\\/dev|\\/proc|\\/sys|\\/run|\\/snap|\\/media|\\/cdrom)/ "
        "{gsub(/%/,\"\",$5); if($5+0>m){m=$5+0; mt=$6}} END{print m+0\"|\"(mt?mt:\"/\")}'); "
        "diskpct=${disk%%|*}; diskmnt=${disk#*|}; "
        # Memory used% from /proc/meminfo.
        "mem=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if(t>0)printf \"%d\",(t-a)*100/t; else print 0}' /proc/meminfo 2>/dev/null); "
        "load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null); "
        "cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1); "
        "failed=$(systemctl --failed --no-legend 2>/dev/null | wc -l | tr -d ' '); [ -z \"$failed\" ] && failed=0; "
        # Names of the failed/crashed units (first few) so the card can say WHICH.
        "units=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | head -4 | paste -sd, - 2>/dev/null); [ -z \"$units\" ] && units=-; "
        # Overall systemd state: 'degraded' means a unit failed (incl. at boot),
        # 'maintenance'/'starting' are also not-healthy. 'running' is good.
        "sysd=$(systemctl is-system-running 2>/dev/null); [ -z \"$sysd\" ] && sysd=unknown; "
        # OOM kills / oom-killer events in the kernel ring buffer (agent is root).
        "oom=$(dmesg -t 2>/dev/null | grep -ciE 'out of memory|killed process|oom-killer'); [ -z \"$oom\" ] && oom=0; "
        "up=$(awk '{print int($1)}' /proc/uptime 2>/dev/null); "
        # Verdict: any warning signal -> WARNING; the worst ones -> CRITICAL.
        "v=OK; "
        "[ \"${diskpct:-0}\" -ge 80 ] 2>/dev/null && v=WARNING; "
        "[ \"${failed:-0}\" -ge 1 ] 2>/dev/null && v=WARNING; "
        "[ \"$sysd\" = degraded ] && v=WARNING; "
        "[ \"${oom:-0}\" -ge 1 ] 2>/dev/null && v=WARNING; "
        "[ \"${diskpct:-0}\" -ge 90 ] 2>/dev/null && v=CRITICAL; "
        "[ \"${failed:-0}\" -ge 3 ] 2>/dev/null && v=CRITICAL; "
        "printf 'SYSMETRICS|verdict=%s|disk=%s|mount=%s|mem=%s|load1=%s|cores=%s|failed=%s|uptime=%s|sysd=%s|units=%s|oom=%s\\n' "
        "\"$v\" \"${diskpct:-0}\" \"${diskmnt:-/}\" \"${mem:-0}\" \"${load1:-0}\" \"${cores:-1}\" \"${failed:-0}\" \"${up:-0}\" \"${sysd:-unknown}\" \"${units:--}\" \"${oom:-0}\""
    )


# ---------------------------------------------------------------------------
# Host posture / compliance snapshot (Phase 1 — read-only, on-demand)
# ---------------------------------------------------------------------------
#
# One read-only command, run on the host *as root* (tokenless, like
# cmd_metrics_snapshot / fleet-health), that gathers a broad set of security &
# compliance posture signals and prints them as a stream of
#
#     POSTURE|<category>.<key>=<value>
#
# lines (key=value rather than JSON — far more robust to build in portable
# shell; the controller's _parse_posture groups them by the dotted prefix).
# Every probe is best-effort and `command -v`-guarded: a missing tool yields
# "n/a" (or the field is simply omitted), never an error. Nothing here writes,
# changes state, or needs a host account, so the read-only auditor can run it.
#
# Phase 1 is Tier-A only: read-only system checks. NOT patch/CVE scoring, NOT
# external scanners (CIS/STIG/OpenSCAP/Lynis), NOT vendor/cloud/EDR/backup.
_POSTURE_SH = r"""
p(){ printf 'POSTURE|%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\r\n\t' '   ' | cut -c1-400)"; }
TMO=""; command -v timeout >/dev/null 2>&1 && TMO="timeout 20"

# --- Operating system -------------------------------------------------------
. /etc/os-release 2>/dev/null
p os.distro "${ID:-unknown}"
p os.name "${PRETTY_NAME:-$(uname -s) $(uname -r)}"
p os.version "${VERSION_ID:-}"
p os.kernel "$(uname -r)"
p os.arch "$(uname -m)"
up=$(awk '{print int($1)}' /proc/uptime 2>/dev/null); p os.uptime_s "${up:-0}"
bt=$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null); [ -n "$bt" ] && p os.boot_epoch "$bt"
if command -v subscription-manager >/dev/null 2>&1; then
  p sub.rhsm "$(subscription-manager status 2>/dev/null | awk -F': ' '/Overall Status/{print $2; exit}')"
fi
if command -v pro >/dev/null 2>&1; then
  p sub.ubuntu_pro "$(pro status 2>/dev/null | awk -F': ' '/account/{print $2; exit}')"
fi

# --- Reboot required --------------------------------------------------------
rr=0
[ -f /var/run/reboot-required ] && rr=1
if command -v needs-restarting >/dev/null 2>&1; then needs-restarting -r >/dev/null 2>&1 || rr=1; fi
if command -v zypper >/dev/null 2>&1; then zypper ps 2>/dev/null | grep -qi 'reboot' && rr=1; fi
p reboot.required "$rr"

# --- Mandatory access control / kernel hardening ----------------------------
if command -v getenforce >/dev/null 2>&1; then p mac.selinux "$(getenforce 2>/dev/null)"; else p mac.selinux "n/a"; fi
if command -v aa-status >/dev/null 2>&1; then
  if aa-status --enabled 2>/dev/null; then p mac.apparmor "enabled"; else p mac.apparmor "disabled"; fi
else p mac.apparmor "n/a"; fi
p sec.fips "$(cat /proc/sys/crypto/fips_enabled 2>/dev/null || echo n/a)"
p sec.aslr "$(cat /proc/sys/kernel/randomize_va_space 2>/dev/null || echo n/a)"
if command -v mokutil >/dev/null 2>&1; then p sec.secureboot "$(mokutil --sb-state 2>/dev/null | head -1)"; else p sec.secureboot "n/a"; fi
if systemctl is-active auditd >/dev/null 2>&1; then p sec.auditd active; else p sec.auditd inactive; fi
if systemctl is-active fail2ban >/dev/null 2>&1; then p sec.fail2ban active
elif command -v fail2ban-client >/dev/null 2>&1; then p sec.fail2ban installed
else p sec.fail2ban absent; fi
if [ -d /etc/modprobe.d ] && grep -rqsiE 'install[[:space:]]+usb-storage[[:space:]]+/bin/(true|false)|blacklist[[:space:]]+usb-storage' /etc/modprobe.d 2>/dev/null; then
  p sec.usb_storage blocked; else p sec.usb_storage allowed; fi

# --- Firewall ---------------------------------------------------------------
fw=none; fwa=0
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then fw=firewalld; fwa=1
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then fw=ufw; fwa=1
elif command -v nft >/dev/null 2>&1 && [ -n "$(nft list ruleset 2>/dev/null)" ]; then fw=nftables; fwa=1
elif command -v iptables >/dev/null 2>&1 && iptables -S 2>/dev/null | grep -qE '^-A'; then fw=iptables; fwa=1
fi
p fw.backend "$fw"; p fw.active "$fwa"

# --- Users / accounts -------------------------------------------------------
p users.uid0 "$(awk -F: '$3==0{print $1}' /etc/passwd 2>/dev/null | paste -sd, -)"
p users.uid0_count "$(awk -F: '$3==0' /etc/passwd 2>/dev/null | wc -l | tr -d ' ')"
p users.empty_pw "$(awk -F: '($2==""){print $1}' /etc/shadow 2>/dev/null | paste -sd, -)"
p users.empty_pw_count "$(awk -F: '($2==""){c++} END{print c+0}' /etc/shadow 2>/dev/null)"
p users.dup_uid "$(awk -F: '{print $3}' /etc/passwd 2>/dev/null | sort | uniq -d | paste -sd, -)"
p users.dup_gid "$(awk -F: '{print $3}' /etc/group 2>/dev/null | sort | uniq -d | paste -sd, -)"
p users.admins "$(getent group sudo wheel 2>/dev/null | awk -F: '{print $4}' | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"
p users.pw_max_days "$(awk '/^PASS_MAX_DAYS/{print $2}' /etc/login.defs 2>/dev/null | head -1)"
p users.pw_min_len "$(awk '/^PASS_MIN_LEN/{print $2}' /etc/login.defs 2>/dev/null | head -1)"
p users.locked_count "$(awk -F: '($2 ~ /^[!*]/){c++} END{print c+0}' /etc/shadow 2>/dev/null)"
p users.svc_login_shells "$(awk -F: '($3>0 && $3<1000 && $7 !~ /(nologin|false|sync|halt|shutdown)$/){print $1}' /etc/passwd 2>/dev/null | paste -sd, -)"
if grep -rqsE 'pam_pwquality|pam_cracklib' /etc/pam.d 2>/dev/null; then p users.pw_complexity configured; else p users.pw_complexity none; fi

# --- SSH (effective config via sshd -T, falls back to file) -----------------
ST=$(sshd -T 2>/dev/null); [ -z "$ST" ] && ST=$(grep -vE '^\s*#|^\s*$' /etc/ssh/sshd_config 2>/dev/null)
sg(){ printf '%s\n' "$ST" | awk -v k="$1" 'BEGIN{IGNORECASE=1} tolower($1)==k{$1="";sub(/^[ \t]+/,"");print;exit}'; }
if [ -n "$ST" ]; then
  p ssh.permit_root_login "$(sg permitrootlogin)"
  p ssh.password_auth "$(sg passwordauthentication)"
  p ssh.pubkey_auth "$(sg pubkeyauthentication)"
  p ssh.max_auth_tries "$(sg maxauthtries)"
  p ssh.idle_timeout "$(sg clientaliveinterval)"
  p ssh.banner "$(sg banner)"
  p ssh.allow_users "$(sg allowusers)"
  p ssh.allow_groups "$(sg allowgroups)"
  p ssh.x11_forwarding "$(sg x11forwarding)"
  p ssh.weak_ciphers "$(sg ciphers | tr ', ' '\n\n' | grep -iE 'cbc|arcfour|3des' | sort -u | paste -sd, -)"
  p ssh.weak_macs "$(sg macs | tr ', ' '\n\n' | grep -iE 'md5|sha1|-96' | sort -u | paste -sd, -)"
  p ssh.weak_kex "$(sg kexalgorithms | tr ', ' '\n\n' | grep -iE 'sha1$|group1-|group-exchange-sha1' | sort -u | paste -sd, -)"
fi
command -v ssh >/dev/null 2>&1 && p ssh.version "$(ssh -V 2>&1 | head -1)"

# --- Filesystem -------------------------------------------------------------
p fs.disk_pct "$(df -P 2>/dev/null | awk 'NR>1 && $1 !~ /^(tmpfs|devtmpfs|overlay|squashfs|udev)$/ && $6 !~ /^(\/dev|\/proc|\/sys|\/run|\/snap|\/var\/lib\/docker)/ {gsub(/%/,"",$5); if($5+0>m)m=$5+0} END{print m+0}')"
p fs.inode_pct "$(df -Pi 2>/dev/null | awk 'NR>1 && $1 !~ /^(tmpfs|devtmpfs|overlay|squashfs|udev)$/ {gsub(/%/,"",$5); if($5+0>m)m=$5+0} END{print m+0}')"
for mp in / /tmp /var /home /dev/shm /boot; do
  opts=$(awk -v m="$mp" '$2==m{print $4; exit}' /proc/mounts 2>/dev/null)
  key=$(printf '%s' "$mp" | sed 's#^/$#root#; s#^/##; s#/#_#g')
  [ -n "$opts" ] && p "mount.$key" "$opts"
done
sg2=$($TMO find / -xdev -type f \( -perm -4000 -o -perm -2000 \) 2>/dev/null | wc -l 2>/dev/null | tr -d ' '); p fs.suid_sgid_count "${sg2:-na}"
ww=$($TMO find / -xdev -type f -perm -0002 2>/dev/null | wc -l 2>/dev/null | tr -d ' '); p fs.world_writable_count "${ww:-na}"
no=$($TMO find / -xdev \( -nouser -o -nogroup \) 2>/dev/null | wc -l 2>/dev/null | tr -d ' '); p fs.unowned_count "${no:-na}"

# --- Time synchronization ---------------------------------------------------
if command -v timedatectl >/dev/null 2>&1; then
  p time.synced "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
  p time.ntp_service "$(timedatectl show -p NTP --value 2>/dev/null)"
  p time.timezone "$(timedatectl show -p Timezone --value 2>/dev/null)"
fi
if command -v chronyc >/dev/null 2>&1; then
  p time.source chrony
  p time.offset "$(chronyc tracking 2>/dev/null | awk -F': ' '/Last offset/{print $2; exit}')"
elif systemctl is-active systemd-timesyncd >/dev/null 2>&1; then p time.source systemd-timesyncd
elif command -v ntpq >/dev/null 2>&1; then p time.source ntpd; fi

# --- Logging ----------------------------------------------------------------
if systemctl is-active rsyslog >/dev/null 2>&1; then p log.rsyslog active; else p log.rsyslog inactive; fi
if systemctl is-active systemd-journald >/dev/null 2>&1; then p log.journald active; else p log.journald inactive; fi
if grep -rqsE '^[^#]*@@?[A-Za-z0-9.]' /etc/rsyslog.conf /etc/rsyslog.d 2>/dev/null; then p log.remote_forward 1; else p log.remote_forward 0; fi
if command -v logrotate >/dev/null 2>&1; then p log.logrotate present; else p log.logrotate absent; fi
p log.var_log_mb "$(du -sm /var/log 2>/dev/null | awk '{print $1}')"

# --- Networking -------------------------------------------------------------
if command -v ss >/dev/null 2>&1; then
  p net.listen_count "$(ss -tulnH 2>/dev/null | wc -l | tr -d ' ')"
  p net.listen_ports "$(ss -tulnH 2>/dev/null | awk '{print $5}' | sed 's/.*://' | grep -E '^[0-9]+$' | sort -un | paste -sd, - | cut -c1-300)"
fi
p net.dns "$(awk '/^nameserver/{print $2}' /etc/resolv.conf 2>/dev/null | paste -sd, -)"
p net.gateway "$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')"
p net.ip_forward "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)"
p net.ipv6_disabled "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)"
p net.hostname "$(hostname 2>/dev/null)"

# --- TLS certificates (bounded scan of common dirs) -------------------------
if command -v openssl >/dev/null 2>&1; then
  cn=0; c30=0; cself=0; near=
  for f in $($TMO find /etc/ssl /etc/pki /etc/letsencrypt /etc/nginx /etc/apache2 /etc/httpd -type f \( -name '*.crt' -o -name '*.pem' -o -name '*.cer' \) 2>/dev/null | head -60); do
    end=$(openssl x509 -enddate -noout -in "$f" 2>/dev/null | cut -d= -f2)
    [ -z "$end" ] && continue
    es=$(date -d "$end" +%s 2>/dev/null) || continue
    [ -z "$es" ] && continue
    days=$(( (es - $(date +%s)) / 86400 ))
    cn=$((cn+1))
    [ "$days" -lt 30 ] 2>/dev/null && c30=$((c30+1))
    { [ -z "$near" ] || [ "$days" -lt "$near" ] 2>/dev/null; } && near=$days
    sub=$(openssl x509 -noout -subject -in "$f" 2>/dev/null); iss=$(openssl x509 -noout -issuer -in "$f" 2>/dev/null)
    [ "${sub#subject}" = "${iss#issuer}" ] && cself=$((cself+1))
  done
  p cert.count "$cn"; p cert.expiring_30d "$c30"; p cert.self_signed "$cself"
  [ -n "$near" ] && p cert.nearest_days "$near"
fi

# --- Services ---------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  p svc.failed_count "$(systemctl --failed --no-legend 2>/dev/null | wc -l | tr -d ' ')"
  p svc.failed "$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | head -10 | paste -sd, -)"
fi
p svc.zombies "$(ps -eo stat 2>/dev/null | grep -c '^Z')"

# --- Hardware ---------------------------------------------------------------
p hw.mem_pct "$(awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{if(t>0)printf "%d",(t-a)*100/t}' /proc/meminfo 2>/dev/null)"
p hw.swap_pct "$(awk '/^SwapTotal:/{t=$2}/^SwapFree:/{f=$2}END{if(t>0)printf "%d",(t-f)*100/t; else print 0}' /proc/meminfo 2>/dev/null)"
p hw.cores "$(nproc 2>/dev/null || echo 1)"
[ -r /proc/mdstat ] && p hw.raid "$(awk '/^md[0-9]/{print $1}' /proc/mdstat 2>/dev/null | paste -sd, -)"
if command -v smartctl >/dev/null 2>&1 && command -v lsblk >/dev/null 2>&1; then
  shl=ok
  for d in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}'); do
    if $TMO smartctl -H "/dev/$d" 2>/dev/null | grep -iE 'overall-health|SMART Health' | grep -iqv 'PASSED'; then shl=failing; fi
  done
  p hw.smart "$shl"
fi

# --- Virtualization ---------------------------------------------------------
if command -v systemd-detect-virt >/dev/null 2>&1; then p virt.type "$(systemd-detect-virt 2>/dev/null)"; else p virt.type "n/a"; fi
if systemctl is-active qemu-guest-agent >/dev/null 2>&1; then p virt.guest_agent qemu-guest-agent
elif systemctl is-active vmtoolsd >/dev/null 2>&1; then p virt.guest_agent open-vm-tools; fi

# --- Containers -------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  p cont.docker "$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,)"
  p cont.docker_running "$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
  p cont.docker_privileged "$(docker ps -q 2>/dev/null | xargs -r docker inspect -f '{{.HostConfig.Privileged}}' 2>/dev/null | grep -c true)"
fi
if command -v podman >/dev/null 2>&1; then
  p cont.podman "$(podman --version 2>/dev/null | awk '{print $3}')"
  p cont.podman_running "$(podman ps -q 2>/dev/null | wc -l | tr -d ' ')"
fi

# --- AD / Identity ----------------------------------------------------------
if command -v realm >/dev/null 2>&1; then p ad.domain "$(realm list --name-only 2>/dev/null | head -1)"; fi
if systemctl is-active sssd >/dev/null 2>&1; then p ad.sssd active; else p ad.sssd inactive; fi
command -v klist >/dev/null 2>&1 && p ad.kerberos present

# --- Performance ------------------------------------------------------------
p perf.load1 "$(awk '{print $1}' /proc/loadavg 2>/dev/null)"
p perf.oom "$(dmesg -t 2>/dev/null | grep -ciE 'out of memory|killed process|oom-killer')"

# --- Miscellaneous ----------------------------------------------------------
p misc.last_login "$(last -1 -w 2>/dev/null | head -1 | cut -c1-120)"
p misc.cron_jobs "$(ls /etc/cron.d 2>/dev/null | wc -l | tr -d ' ')"
command -v systemctl >/dev/null 2>&1 && p misc.timers "$(systemctl list-timers --no-legend 2>/dev/null | wc -l | tr -d ' ')"

# Whether this ran as root. Many checks above (shadow, sshd -T, SUID scans)
# only see the truth as root; on an unprivileged SSH host they read blank and
# would otherwise look falsely "clean", so the controller marks such a host's
# posture as limited rather than trusting it.
p meta.privileged "$([ "$(id -u 2>/dev/null)" = 0 ] && echo 1 || echo 0)"
printf 'POSTURE|meta.done=1\n'
"""


def cmd_posture_snapshot() -> str:
    """Read-only host posture/compliance snapshot. Run as root (tokenless),
    like cmd_metrics_snapshot/fleet-health. Emits a stream of
    `POSTURE|<category>.<key>=<value>` lines covering OS, security/MAC,
    firewall, users, SSH, filesystem, time sync, logging, networking, TLS
    certs, services, hardware, virtualization, containers, and AD/identity.
    Best-effort and non-mutating throughout — see _POSTURE_SH."""
    return _POSTURE_SH


def cmd_health_check() -> str:
    """A single combined command that has the *host itself* score a few
    signals (disk usage, failed systemd units, load average relative to
    core count) against fixed thresholds and prints a leading "HEALTH:
    OK/WARNING/CRITICAL" line. Plain POSIX sh (no [[ ]], no bashisms)
    since this runs unmodified through both dispatch paths.

    Disk scoring ignores install media, removable mounts, and
    image-backed filesystems (squashfs/iso9660/udf, anything under
    /media, /run/media, /cdrom, or /snap) before checking thresholds -
    those are read-only by design and "100% full" is normal for them,
    not a sign of a failing disk.
    """
    return r"""
disk_detail=$(df -hPT 2>/dev/null | awk 'NR>1 && $7!="" {
    fstype=$2; mnt=$7; pct=$6;
    if (fstype=="squashfs" || fstype=="iso9660" || fstype=="udf" || fstype=="overlay") next;
    if (mnt ~ /^\/(media|run\/media|cdrom|snap)(\/|$)/) next;
    gsub("%","",pct); pct=pct+0;
    status="ok"; if (pct>=90) status="critical"; else if (pct>=75) status="warning";
    printf "%-8s %4s%%  %s\n", status, pct, mnt
}')
disk_status="OK"
if echo "$disk_detail" | grep -q "^critical"; then disk_status="CRITICAL"
elif echo "$disk_detail" | grep -q "^warning"; then disk_status="WARNING"
fi

failed_count=$(systemctl --failed --no-legend 2>/dev/null | wc -l | tr -d ' ')
[ -z "$failed_count" ] && failed_count=0
failed_status="OK"
if [ "$failed_count" -ge 3 ] 2>/dev/null; then failed_status="CRITICAL"
elif [ "$failed_count" -ge 1 ] 2>/dev/null; then failed_status="WARNING"
fi

cores=$(nproc 2>/dev/null || echo 1)
load1=$(uptime | awk -F'load average' '{print $2}' | tr -d ':' | awk -F',' '{print $1}' | tr -d ' ')
load_ratio=$(awk -v l="$load1" -v c="$cores" 'BEGIN{ if (c=="" || c==0) c=1; if (l=="") l=0; printf "%.2f", l/c }')
load_status="OK"
if awk -v r="$load_ratio" 'BEGIN{exit !(r>=2)}'; then load_status="CRITICAL"
elif awk -v r="$load_ratio" 'BEGIN{exit !(r>=1)}'; then load_status="WARNING"
fi

overall="OK"
if [ "$disk_status" = "CRITICAL" ] || [ "$failed_status" = "CRITICAL" ] || [ "$load_status" = "CRITICAL" ]; then
    overall="CRITICAL"
elif [ "$disk_status" = "WARNING" ] || [ "$failed_status" = "WARNING" ] || [ "$load_status" = "WARNING" ]; then
    overall="WARNING"
fi

echo "HEALTH: $overall"
echo
echo "Reasons:"
echo "  Disk usage:      $disk_status"
echo "$disk_detail" | sed 's/^/    /'
echo "  Failed services: $failed_status  ($failed_count failed unit(s))"
echo "  Load average:    $load_status  (load $load1 across $cores core(s), ratio $load_ratio)"
echo
echo "-- Raw signals --"
echo "Disk usage (real volumes):"
df -hT 2>/dev/null
echo
echo "systemctl --failed:"
systemctl --failed --no-legend 2>/dev/null || echo '(systemctl not available on this host)'
echo
echo "uptime:"
uptime
""".strip()


def cmd_search_log(pattern: str = "", lines: int = 200) -> str:
    lines = max(1, int(lines))

    if pattern and pattern.strip():
        p = shlex.quote(pattern.strip())
        return (
            f"journalctl -n {lines} --no-pager 2>/dev/null | grep -i {p} "
            f"|| grep -i {p} /var/log/syslog 2>/dev/null "
            f"|| grep -i {p} /var/log/messages 2>/dev/null "
            "|| echo 'No matching log lines found (or no readable log source on this host).'"
        )

    return (
        f"journalctl -n {lines} --no-pager 2>/dev/null "
        f"|| tail -n {lines} /var/log/syslog 2>/dev/null "
        f"|| tail -n {lines} /var/log/messages 2>/dev/null "
        "|| echo 'No readable log source found on this host.'"
    )


# ---------------------------------------------------------
# Logging and Troubleshooting (System Health & Logs, continued) -
# same rules as the rest of this section: plain POSIX sh, shlex.quote()
# on anything interpolated, and an explicit message instead of silent
# empty output whenever a tool/log source might be missing.
# ---------------------------------------------------------
def cmd_review_system_logs(lines: int = 200) -> str:
    """A quicker skim than Search/Tail Logs above - leads with how many
    error/warning-level lines this boot has logged so far, then shows
    the most recent N lines, so a glance at the tab catches a problem
    without reading the whole thing."""
    lines = max(1, int(lines))
    return (
        "if command -v journalctl >/dev/null 2>&1; then "
        "err=$(journalctl -b -p err --no-pager 2>/dev/null | wc -l | tr -d ' '); "
        "warnplus=$(journalctl -b -p warning --no-pager 2>/dev/null | wc -l | tr -d ' '); "
        "warn=$((warnplus - err)); "
        "echo \"This boot so far: $err error-level line(s), $warn warning-level line(s).\"; echo; "
        f"echo '== Most recent {lines} log lines =='; "
        f"journalctl -n {lines} --no-pager 2>&1; "
        "else "
        "echo '(journalctl not available - falling back to flat log files)'; echo; "
        f"tail -n {lines} /var/log/syslog 2>/dev/null "
        f"|| tail -n {lines} /var/log/messages 2>/dev/null "
        "|| echo 'No readable log source found on this host.'; "
        "fi"
    )


_JOURNAL_PRIORITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def cmd_analyze_journal_logs(priority: str = "", lines: int = 200) -> str:
    """Journal-specific drill-down, scoped to the current boot (-b) -
    distinct from Review System Logs above, which tails the live log
    regardless of boot or priority. An optional priority floor (e.g.
    "warning" = warning and anything more severe) narrows it to just
    the entries worth investigating."""
    lines = max(1, int(lines))
    priority = (priority or "").strip().lower()
    if priority and priority not in _JOURNAL_PRIORITIES:
        raise ValueError(f"Priority must be one of: {', '.join(_JOURNAL_PRIORITIES)}")

    no_journal_msg = "journalctl is not available on this host (no systemd journal)."

    if not priority:
        return (
            "if ! command -v journalctl >/dev/null 2>&1; then "
            f"echo {shlex.quote(no_journal_msg)}; exit 0; fi; "
            f"journalctl -b -n {lines} --no-pager 2>&1"
        )

    p = shlex.quote(priority)
    empty_msg = shlex.quote(f"No journal entries at priority '{priority}' or higher for this boot.")
    return (
        "if ! command -v journalctl >/dev/null 2>&1; then "
        f"echo {shlex.quote(no_journal_msg)}; exit 0; fi; "
        f"out=$(journalctl -b -p {p} -n {lines} --no-pager 2>&1); "
        f"if [ -z \"$out\" ]; then echo {empty_msg}; else echo \"$out\"; fi"
    )


def cmd_investigate_boot_failures() -> str:
    """Combined boot diagnostic: which boots systemd knows about (so an
    unclean shutdown/crash is visible at a glance), how long the most
    recent boot took and what held it up the longest, and any unit
    that's currently sitting in the failed list."""
    return r"""
echo '== Recent boots (journalctl --list-boots) =='
if command -v journalctl >/dev/null 2>&1; then
    journalctl --list-boots --no-pager 2>&1
else
    echo 'journalctl is not available on this host.'
fi
echo
echo '== Boot time breakdown (systemd-analyze) =='
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze 2>&1
    echo
    echo '== Slowest units to start (systemd-analyze blame, top 20) =='
    systemd-analyze blame --no-pager 2>&1 | head -n 20
    echo
    echo '== Critical chain (what the boot waited on) =='
    systemd-analyze critical-chain --no-pager 2>&1
else
    echo 'systemd-analyze is not available on this host.'
fi
echo
echo '== Units currently in the failed list =='
if command -v systemctl >/dev/null 2>&1; then
    out=$(systemctl --failed --no-legend 2>/dev/null)
    if [ -z "$out" ]; then echo 'No failed units.'; else echo "$out"; fi
else
    echo 'systemctl not available on this host.'
fi
""".strip()


def cmd_trace_application_errors(unit: str = "", lines: int = 200) -> str:
    """Scoped to one service's journal when a unit is given - grepped
    for error/exception/stack-trace vocabulary even if logged at a
    lower priority, since application code often logs its own errors
    at "info"; otherwise scans the whole journal for the same
    vocabulary."""
    lines = max(1, int(lines))
    grep_pattern = shlex.quote("(error|exception|traceback|fatal|panic)")
    unit = (unit or "").strip()
    no_journal_msg = shlex.quote("journalctl is not available on this host.")

    if unit:
        u = shlex.quote(unit)
        empty_msg = shlex.quote(
            f"No application-error-like lines found for '{unit}' in the last {lines} journal lines."
        )
        return (
            f"if ! command -v journalctl >/dev/null 2>&1; then echo {no_journal_msg}; exit 0; fi; "
            f"out=$(journalctl -u {u} -n {lines} --no-pager 2>&1 | grep -iE {grep_pattern}); "
            f"if [ -z \"$out\" ]; then echo {empty_msg}; else echo \"$out\"; fi"
        )

    empty_msg = shlex.quote(f"No application-error-like lines found in the last {lines} journal lines.")
    return (
        f"if ! command -v journalctl >/dev/null 2>&1; then echo {no_journal_msg}; exit 0; fi; "
        f"out=$(journalctl -n {lines} --no-pager 2>&1 | grep -iE {grep_pattern}); "
        f"if [ -z \"$out\" ]; then echo {empty_msg}; else echo \"$out\"; fi"
    )


def cmd_monitor_kernel_messages(lines: int = 200) -> str:
    """Kernel ring buffer with human-readable timestamps where dmesg
    supports it, falling back to the journal's kernel-tagged entries
    on hosts where dmesg needs privileges this user doesn't have."""
    lines = max(1, int(lines))
    return (
        "if command -v dmesg >/dev/null 2>&1 && dmesg -T >/dev/null 2>&1; then "
        f"dmesg -T 2>/dev/null | tail -n {lines}; "
        "elif command -v journalctl >/dev/null 2>&1; then "
        f"journalctl -k -n {lines} --no-pager 2>&1; "
        "else "
        "echo 'Neither dmesg nor journalctl -k is available/permitted on this host.'; "
        "fi"
    )


def cmd_collect_support_info() -> str:
    """A quick, read-only support-info snapshot to paste into a ticket
    without waiting on a full sos report - identity/OS/kernel, uptime
    and load, memory and disk, network basics, and failed services.
    For a complete, vendor-format diagnostic archive instead, see
    Generate sos Report below."""
    return r"""
echo '== Identity =='
echo "Hostname: $(hostname 2>/dev/null)"
if [ -r /etc/os-release ]; then . /etc/os-release; echo "OS: ${PRETTY_NAME:-unknown}"; fi
echo "Kernel: $(uname -srm 2>/dev/null)"
echo "Date: $(date 2>/dev/null)"
echo
echo '== Uptime / Load =='
uptime
echo
echo '== Memory =='
free -h 2>/dev/null || echo 'free not available'
echo
echo '== Disk =='
df -hT 2>/dev/null
echo
echo '== Network interfaces =='
(ip -brief addr 2>/dev/null || ifconfig 2>/dev/null || echo 'no ip/ifconfig available')
echo
echo '== Failed services =='
if command -v systemctl >/dev/null 2>&1; then
    out=$(systemctl --failed --no-legend 2>/dev/null)
    if [ -z "$out" ]; then echo 'No failed services.'; else echo "$out"; fi
else
    echo 'systemctl not available.'
fi
""".strip()


_SOS_MISSING_MSG = (
    "sos is not installed on this host (package: 'sos' on RHEL/Fedora/openSUSE, "
    "'sosreport' on Debian/Ubuntu). Install it first, then try again."
)


def cmd_generate_sos_report() -> str:
    """Runs the distro's sos/sosreport tool in unattended batch mode and
    prints where it wrote the finished archive. This deliberately does
    NOT auto-install the tool if it's missing - unlike a read-only
    report, installing software is a deliberate action this button
    didn't ask for, so it just says what's missing instead."""
    return (
        "if command -v sos >/dev/null 2>&1; then "
        "sos report --batch 2>&1; "
        "elif command -v sosreport >/dev/null 2>&1; then "
        "sosreport --batch 2>&1; "
        "else "
        f"echo {shlex.quote(_SOS_MISSING_MSG)} >&2; exit 1; "
        "fi"
    )


def cmd_install_sos() -> str:
    """Install the sos / sosreport package across the common package
    managers, as root via the agent. apt's package is 'sos' on current
    releases and 'sosreport' on older ones, so try both."""
    return (
        "set -e; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "export DEBIAN_FRONTEND=noninteractive; apt-get update; "
        "apt-get install -y sos || apt-get install -y sosreport; "
        "elif command -v dnf >/dev/null 2>&1; then dnf install -y sos; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y sos; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install sos; "
        "else echo 'No supported package manager found (apt/dnf/yum/zypper).' >&2; exit 1; fi; "
        "echo; echo 'sos installed - you can now run Generate sos Report.'"
    )


def cmd_install_auditd() -> str:
    """Install the Linux audit daemon (auditd on Debian/Ubuntu, audit on
    RHEL/Fedora/openSUSE/Arch) and enable it, as root via the agent, so
    Review Audit Logs has logs to read."""
    return (
        "set -e; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y auditd; "
        "elif command -v dnf >/dev/null 2>&1; then dnf install -y audit; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y audit; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install audit; "
        "elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm audit; "
        "else echo 'No supported package manager found (apt/dnf/yum/zypper/pacman).' >&2; exit 1; fi; "
        "systemctl enable --now auditd 2>/dev/null || true; "
        "echo; echo 'auditd installed and started.'"
    )


def cmd_investigate_crashes() -> str:
    """Three different "something crashed" signals in one pass: kernel-
    level oops/panic/segfault lines from dmesg, OOM-killer events (a
    crash that looks application-side but is actually the kernel
    killing a process for memory), and systemd-coredump's own record of
    core dumps, wherever each source is available."""
    return r"""
echo '== Kernel oops / panic / segfault (dmesg) =='
if command -v dmesg >/dev/null 2>&1; then
    out=$(dmesg -T 2>/dev/null | grep -iE 'panic|oops|segfault|general protection fault')
    if [ -z "$out" ]; then echo 'No kernel panic/oops/segfault lines found.'; else echo "$out"; fi
else
    echo 'dmesg not available on this host.'
fi
echo
echo '== OOM killer events =='
if command -v journalctl >/dev/null 2>&1; then
    out=$(journalctl -k --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process')
    if [ -z "$out" ]; then echo 'No OOM-killer events found in the kernel journal.'; else echo "$out"; fi
else
    echo 'journalctl not available on this host.'
fi
echo
echo '== Recorded core dumps (coredumpctl) =='
if command -v coredumpctl >/dev/null 2>&1; then
    coredumpctl list --no-pager 2>&1 || echo 'No core dumps recorded.'
else
    echo 'coredumpctl not available on this host.'
fi
""".strip()


def cmd_troubleshoot_memory_issues() -> str:
    """Leads with the same computed usage-percent status line as
    Memory && CPU Snapshot above, then layers on the memory-specific
    signals that snapshot doesn't show: the top memory consumers, one
    swap-activity sample, and any OOM-killer history."""
    return r"""
mem_pct=$(LANG=C free -m 2>/dev/null | awk 'NR==2 && $2>0 {printf "%.0f", $3/$2*100}')
mem_status=OK
if [ -n "$mem_pct" ]; then
    if [ "$mem_pct" -ge 90 ] 2>/dev/null; then mem_status=CRITICAL
    elif [ "$mem_pct" -ge 75 ] 2>/dev/null; then mem_status=WARNING
    fi
fi
echo "Memory usage: ${mem_pct:-unknown}% ($mem_status)"
echo
echo '== Memory / Swap =='
free -h
echo
echo '== Top 15 memory consumers =='
ps -eo pid,ppid,user,%mem,%cpu,comm --sort=-%mem | head -n 16
echo
echo '== Swap activity (vmstat, one 1s sample) =='
vmstat 1 2 2>/dev/null | tail -n 1 || echo 'vmstat not available on this host'
echo
echo '== OOM-killer history (journal, kernel) =='
if command -v journalctl >/dev/null 2>&1; then
    out=$(journalctl -k --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process')
    if [ -z "$out" ]; then echo 'No OOM-killer events found.'; else echo "$out"; fi
else
    echo 'journalctl not available on this host.'
fi
""".strip()


def cmd_analyze_cpu_bottlenecks() -> str:
    """CPU-specific companion to Investigate High Load above: the same
    load/core-ratio scoring, then per-core utilization where mpstat is
    available, the run-queue/context-switch/interrupt counters from
    vmstat, and the top CPU consumers."""
    return r"""
cores=$(nproc 2>/dev/null || echo 1)
load1=$(uptime | awk -F'load average' '{print $2}' | tr -d ':' | awk -F',' '{print $1}' | tr -d ' ')
load_ratio=$(awk -v l="$load1" -v c="$cores" 'BEGIN{ if (c=="" || c==0) c=1; if (l=="") l=0; printf "%.2f", l/c }')
load_status=OK
if awk -v r="$load_ratio" 'BEGIN{exit !(r>=2)}'; then load_status=CRITICAL
elif awk -v r="$load_ratio" 'BEGIN{exit !(r>=1)}'; then load_status=WARNING
fi
echo "CPU load: $load_status  (load $load1 across $cores core(s), ratio $load_ratio)"
echo
echo '== Per-core utilization (mpstat, one 1s sample) =='
if command -v mpstat >/dev/null 2>&1; then
    mpstat -P ALL 1 1 2>/dev/null
else
    echo 'mpstat not available (package: sysstat) - see the load average above instead.'
fi
echo
echo '== Run queue / context switches / interrupts (vmstat, one 1s sample) =='
vmstat 1 2 2>/dev/null | tail -n 2 || echo 'vmstat not available on this host'
echo
echo '== Top 15 by CPU =='
ps -eo pid,ppid,user,%cpu,%mem,stat,etime,comm --sort=-%cpu | head -n 16
""".strip()


def cmd_review_audit_logs(lines: int = 200) -> str:
    """auditd's log usually needs root to read directly, so this prefers
    ausearch (which goes through the audit dispatcher) and only falls
    back to reading the flat file if ausearch isn't present."""
    lines = max(1, int(lines))
    no_audit_msg = shlex.quote(
        "No readable audit log found (auditd may not be installed, or this user "
        "cannot read /var/log/audit/audit.log)."
    )
    return (
        "if command -v ausearch >/dev/null 2>&1; then "
        f"ausearch -i 2>&1 | tail -n {lines}; "
        "elif [ -r /var/log/audit/audit.log ]; then "
        f"tail -n {lines} /var/log/audit/audit.log 2>&1; "
        "else "
        f"echo {no_audit_msg}; "
        "fi"
    )
