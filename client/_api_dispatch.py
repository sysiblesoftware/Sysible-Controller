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
    an agent and an SSH connection for the same physical host - prefer
    SSH (a real, synchronous connection, and the only option that
    supports an actual interactive terminal) the same way Remote Host
    Administration's terminal session picker does, falling back to
    agent only if SSH is somehow missing. Entries that aren't merged
    pass through untouched."""
    if entry["kind"] == "merged":
        return entry["ssh_entry"] or entry["agent_entry"]
    return entry


def list_merged_hosts():
    """Agent hosts + SSH hosts as one list of dicts: {"kind": "agent"|
    "ssh"|"merged", "id", "label", "type_text", "address",
    "environment"} (a "merged" entry additionally carries "agent_entry"/
    "ssh_entry" - see merge_duplicate_host_entries()) - the same shape
    Remote Administration builds internally for its own host list,
    exposed here so other pages (System Administration) can target
    both kinds, with duplicates already collapsed, without
    re-implementing the merge."""
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

    return merge_duplicate_host_entries(entries)


def run_on_entry(entry, command: str, kind: str = "command"):
    """Run `command` on one merged-host entry (as produced by
    list_merged_hosts()). SSH executes synchronously over exec_remote()
    - the result is ready immediately. Agent dispatch is async - only a
    task_id comes back, and the caller must poll poll_entry_result()
    until it resolves. Always returns a dict with a "sync" flag so
    callers can branch on which case they got:
      {"sync": True,  "stdout", "stderr", "code", "error"}   (ssh, done)
      {"sync": False, "task_id", "error"}                    (agent, pending)
    """
    entry = _underlying_entry(entry)

    if entry["kind"] == "ssh":
        try:
            result = exec_remote(entry["id"], command)
            return {
                "sync": True,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "code": result.get("code"),
                "error": None,
            }
        except Exception as e:
            return {"sync": True, "stdout": "", "stderr": "", "code": None, "error": str(e)}

    task_ids = queue_command_on_hosts([entry["id"]], command, kind=kind)
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
            result = exec_remote(entry["id"], build_user_sync_command())
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
    return "df -hT"


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

disk_excluded=$(df -hPT 2>/dev/null | awk 'NR>1 && $7!="" {
    fstype=$2; mnt=$7;
    if (fstype=="squashfs" || fstype=="iso9660" || fstype=="udf" || fstype=="overlay" || mnt ~ /^\/(media|run\/media|cdrom|snap)(\/|$)/)
        printf "%s (%s)\n", mnt, fstype
}')

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
if [ -n "$disk_excluded" ]; then
    echo "    (excluded - install media / removable / image mounts:)"
    echo "$disk_excluded" | sed 's/^/      /'
fi
echo "  Failed services: $failed_status  ($failed_count failed unit(s))"
echo "  Load average:    $load_status  (load $load1 across $cores core(s), ratio $load_ratio)"
echo
echo "-- Raw signals --"
echo "df -h (unfiltered - includes the excluded mounts above, for reference):"
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
