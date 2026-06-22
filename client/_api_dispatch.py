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
    if agent_only:
        merged = [e for e in merged if e["kind"] != "ssh"]
    return merged


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
