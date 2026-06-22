"""PROCESS MANAGEMENT + SERVICE MANAGEMENT dual-host command builders -
split out of client/api.py to keep individual file sizes manageable.
Imported via `from client._api_process_service import *` at the bottom
of client/api.py.

Same rules as the rest of this file: plain POSIX sh, shlex.quote() (or
explicit int/enum validation) on anything interpolated, and an
unambiguous message instead of silent empty output.
"""
import shlex


def _validate_pid(pid) -> str:
    """Pure validation, not just quoting - a kill/renice target has to
    be a single positive integer. Without this, an empty or negative
    value could turn `kill -TERM <pid>` into a bare `kill -TERM`
    (no-op) or, worse, `kill -TERM -1`-style process-group/broadcast
    targeting, which is the opposite of "act on the one PID I typed"."""
    try:
        n = int(str(pid).strip())
    except (TypeError, ValueError):
        raise ValueError("PID must be a positive integer")
    if n <= 0:
        raise ValueError("PID must be a positive integer")
    return str(n)


_ALLOWED_SIGNALS = {"TERM", "KILL", "HUP", "INT", "QUIT", "USR1", "USR2", "STOP", "CONT"}


def cmd_list_processes(sort_by: str = "cpu", top_n: int = 30) -> str:
    top_n = max(1, int(top_n))
    mem = (sort_by or "").strip().lower() == "mem"
    key = "%mem" if mem else "%cpu"
    title = f"Top {top_n} processes by {'memory' if mem else 'CPU'} usage"
    # Lead with a title so the bare ps table isn't headerless.
    return (
        f"echo '== {title} =='; echo; "
        f"ps -eo pid,ppid,user,%cpu,%mem,stat,etime,comm --sort=-{key} | head -n {top_n + 1}"
    )


def cmd_investigate_high_load() -> str:
    """One combined dump of the signals you'd otherwise check one at a
    time when a host feels sluggish: load average against core count,
    the top CPU and memory consumers, and a single iowait sample."""
    return (
        "cores=$(nproc 2>/dev/null || echo 1); "
        "load1=$(uptime | awk -F'load average' '{print $2}' | tr -d ':' | awk -F',' '{print $1}' | tr -d ' '); "
        "load_ratio=$(awk -v l=\"$load1\" -v c=\"$cores\" 'BEGIN{ if (c==\"\"||c==0) c=1; if (l==\"\") l=0; printf \"%.2f\", l/c }'); "
        "load_status=OK; "
        "if awk -v r=\"$load_ratio\" 'BEGIN{exit !(r>=2)}'; then load_status=CRITICAL; "
        "elif awk -v r=\"$load_ratio\" 'BEGIN{exit !(r>=1)}'; then load_status=WARNING; fi; "
        "echo \"Load average: $load_status  (load $load1 across $cores core(s), ratio $load_ratio)\" && echo "
        "&& echo '== Load / Uptime ==' && uptime && echo "
        "&& echo '== CPU core count ==' && (nproc 2>/dev/null || echo 'unknown') && echo "
        "&& echo '== Top 15 by CPU ==' "
        "&& ps -eo pid,ppid,user,%cpu,%mem,stat,etime,comm --sort=-%cpu | head -n 16 && echo "
        "&& echo '== Top 15 by Memory ==' "
        "&& ps -eo pid,ppid,user,%cpu,%mem,stat,etime,comm --sort=-%mem | head -n 16 && echo "
        "&& echo '== I/O wait (vmstat, one 1s sample) ==' "
        "&& (vmstat 1 2 2>/dev/null | tail -n 1 || echo 'vmstat not available on this host')"
    )


def cmd_zombie_processes() -> str:
    """Zombies (STAT containing Z) have already exited - their only
    remaining footprint is the process-table entry, which is freed
    once the parent (PPID) reaps it."""
    return r"""
zombies=$(ps -eo pid,ppid,user,stat,comm 2>/dev/null | awk 'NR==1 || $4 ~ /Z/')
count=$(printf '%s\n' "$zombies" | tail -n +2 | grep -c .)
if [ -z "$count" ] || [ "$count" -eq 0 ] 2>/dev/null; then
    echo 'No zombie processes found.'
else
    echo "$zombies"
    echo
    echo "Found $count zombie process(es). A zombie's resources are released once its"
    echo "parent reaps it (calls wait()) - if these persist, the parent PID (PPID) above"
    echo "is the one to investigate or restart."
fi
""".strip()


def cmd_kill_process(pid, signal: str = "TERM") -> str:
    pid = _validate_pid(pid)
    sig = (signal or "TERM").strip().upper()
    if sig not in _ALLOWED_SIGNALS:
        raise ValueError(f"Unsupported signal: {signal}")
    return (
        f"if ! kill -0 {pid} 2>/dev/null; then echo 'No process with PID {pid} on this host.'; "
        f"else "
        f"echo 'Before:'; ps -o pid,ppid,user,%cpu,%mem,comm -p {pid} 2>/dev/null | tail -n +2; "
        f"kill -{sig} {pid} && echo 'Sent SIG{sig} to PID {pid}.' || echo 'kill -{sig} {pid} failed.'; "
        f"sleep 1; "
        f"if kill -0 {pid} 2>/dev/null; then echo 'PID {pid} is still running after SIG{sig}.'; "
        f"else echo 'PID {pid} is no longer running.'; fi; "
        f"fi"
    )


def cmd_renice_process(pid, niceness: int = 0) -> str:
    pid = _validate_pid(pid)
    try:
        niceness = int(niceness)
    except (TypeError, ValueError):
        raise ValueError("Niceness must be an integer")
    if niceness < -20 or niceness > 19:
        raise ValueError("Niceness must be between -20 and 19")
    return (
        f"if ! kill -0 {pid} 2>/dev/null; then echo 'No process with PID {pid} on this host.'; "
        f"else "
        f"echo 'Current priority:'; ps -o pid,ni,comm -p {pid} 2>/dev/null | tail -n +2; "
        f"renice {niceness} -p {pid} >/dev/null 2>&1 "
        f"&& echo 'Set niceness of PID {pid} to {niceness}.' "
        f"|| echo 'renice failed (negative niceness usually needs root/sudo on the agent or SSH user).'; "
        f"echo 'New priority:'; ps -o pid,ni,comm -p {pid} 2>/dev/null | tail -n +2; "
        f"fi"
    )


def cmd_restart_process(pid) -> str:
    """Restarts a hung application by capturing its full command line
    and working directory from /proc, stopping it (SIGTERM, escalating
    to SIGKILL after 5s if it won't die), then relaunching the same
    command line detached in the background. Linux-only (needs /proc)."""
    pid = _validate_pid(pid)
    return rf"""
if ! kill -0 {pid} 2>/dev/null; then
    echo 'No process with PID {pid} on this host.'
    exit 0
fi
if [ ! -r /proc/{pid}/cmdline ]; then
    echo 'Cannot read /proc/{pid}/cmdline - restarting a process needs a Linux host with /proc.'
    exit 0
fi
cmdline=$(tr '\0' ' ' < /proc/{pid}/cmdline | sed 's/[[:space:]]*$//')
if [ -z "$cmdline" ]; then
    echo 'Could not read the command line for PID {pid} (kernel thread, or it already exited?). Not restarting.'
    exit 0
fi
cwd=$(readlink /proc/{pid}/cwd 2>/dev/null)
[ -z "$cwd" ] && cwd="$HOME"
echo "Captured command: $cmdline"
echo "Captured working dir: $cwd"
kill -TERM {pid} 2>/dev/null
i=0
while [ "$i" -lt 5 ] && kill -0 {pid} 2>/dev/null; do
    sleep 1
    i=$((i + 1))
done
if kill -0 {pid} 2>/dev/null; then
    echo 'PID {pid} did not exit after SIGTERM - sending SIGKILL...'
    kill -KILL {pid} 2>/dev/null
    sleep 1
fi
echo 'Old process stopped. Relaunching...'
(cd "$cwd" 2>/dev/null || cd "$HOME"; nohup sh -c "$cmdline" >/dev/null 2>&1 &)
sleep 1
firstword=$(printf '%s' "$cmdline" | awk '{{print $1}}')
newpid=$(pgrep -f "$firstword" 2>/dev/null | tail -n 1)
if [ -n "$newpid" ]; then
    echo "Relaunched. New PID (best guess): $newpid"
else
    echo "Relaunch command issued; could not confirm a new PID automatically. Check the process list."
fi
""".strip()


# ---------------------------------------------------------
# SERVICE MANAGEMENT (dual-host command builders for the Service
# Management page - systemd start/stop/enable/etc, plus creating a
# brand-new unit and configuring its dependencies.
# ---------------------------------------------------------
def _service_unit(name: str) -> str:
    """Normalizes a user-typed service name to its full unit name
    ("nginx" -> "nginx.service")."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Service name cannot be empty")
    return name if name.endswith(".service") else f"{name}.service"


def cmd_list_usernames() -> str:
    """All local usernames on the host, one per line - used to populate the
    "Run as user" dropdown when creating a service. getent covers NSS
    sources (LDAP/SSSD) too, with a flat /etc/passwd fallback."""
    return (
        "getent passwd 2>/dev/null | cut -d: -f1 "
        "|| awk -F: '{print $1}' /etc/passwd 2>/dev/null "
        "|| echo 'could not read user list'"
    )


def cmd_list_services() -> str:
    return (
        "systemctl list-unit-files --type=service --no-legend 2>/dev/null "
        "| awk '{print $1}' | sed 's/\\.service$//' | sort "
        "|| echo 'systemctl not available on this host'"
    )


def cmd_service_status(name: str) -> str:
    return f"systemctl status {shlex.quote(_service_unit(name))} --no-pager -l 2>&1; true"


def _service_action_cmd(verb: str, name: str, past: str) -> str:
    """`systemctl <verb> <unit>` is silent on success, which makes the GUI
    look like nothing happened. Wrap it so it always reports the outcome
    and the unit's resulting state (active / enabled), and exits with the
    real return code so the GUI can colour it success/failure. `verb` and
    `past` are fixed English words chosen here, never user input."""
    q = shlex.quote(_service_unit(name))
    return (
        f"u={q}; "
        f'systemctl {verb} "$u"; rc=$?; '
        f'if [ "$rc" -eq 0 ]; then echo "{past} $u."; '
        f'else echo "Failed to {verb} $u (exit $rc)." >&2; fi; '
        f'echo "Now: active=$(systemctl is-active "$u" 2>/dev/null), '
        f'enabled=$(systemctl is-enabled "$u" 2>/dev/null)"; '
        f'exit "$rc"'
    )


def cmd_service_start(name: str) -> str:
    return _service_action_cmd("start", name, "Started")


def cmd_service_stop(name: str) -> str:
    return _service_action_cmd("stop", name, "Stopped")


def cmd_service_restart(name: str) -> str:
    return _service_action_cmd("restart", name, "Restarted")


def cmd_service_reload(name: str) -> str:
    return _service_action_cmd("reload", name, "Reloaded")


def cmd_service_enable(name: str) -> str:
    return _service_action_cmd("enable", name, "Enabled")


def cmd_service_disable(name: str) -> str:
    return _service_action_cmd("disable", name, "Disabled")


def cmd_service_logs(name: str, lines: int = 200) -> str:
    unit = shlex.quote(_service_unit(name))
    lines = max(1, int(lines))
    return (
        f"journalctl -u {unit} -n {lines} --no-pager 2>&1 "
        "|| echo 'journalctl not available on this host'"
    )


def cmd_troubleshoot_service(name: str) -> str:
    """One combined diagnostic dump for a struggling service - live
    status, whether it shows up in systemd's --failed list, and its
    last 100 log lines."""
    unit = shlex.quote(_service_unit(name))
    plain_name = _service_unit(name)
    return (
        f"echo '== systemctl status {plain_name} ==' && "
        f"systemctl status {unit} --no-pager -l 2>&1; "
        f"echo && echo '== Currently in the --failed list? ==' && "
        f"(systemctl --failed --no-legend 2>/dev/null | grep -F {unit} "
        "|| echo 'Not currently in the failed list.'); "
        f"echo && echo '== Last 100 log lines (journalctl -u {plain_name}) ==' && "
        f"journalctl -u {unit} -n 100 --no-pager 2>&1"
    )


def cmd_service_dependencies(name: str) -> str:
    """Read-only view of what this service depends on (and what
    depends on it)."""
    unit = shlex.quote(_service_unit(name))
    return (
        "echo '== Depends on (After/Requires/Wants) ==' && "
        f"systemctl list-dependencies {unit} --no-pager 2>&1; "
        "echo && echo '== Depended on by (reverse) ==' && "
        f"systemctl list-dependencies {unit} --reverse --no-pager 2>&1"
    )


def cmd_set_service_dependencies(name: str, after: str = "", requires: str = "", wants: str = "") -> str:
    """Adds an [Unit] After=/Requires=/Wants= drop-in override at
    /etc/systemd/system/<unit>.d/override.conf rather than editing the
    original unit file directly."""
    unit = _service_unit(name)
    body_lines = ["[Unit]"]
    if after.strip():
        body_lines.append(f"After={after.strip()}")
    if requires.strip():
        body_lines.append(f"Requires={requires.strip()}")
    if wants.strip():
        body_lines.append(f"Wants={wants.strip()}")
    if len(body_lines) == 1:
        raise ValueError("Specify at least one of After / Requires / Wants")

    override_dir = f"/etc/systemd/system/{unit}.d"
    override_path = f"{override_dir}/override.conf"
    body = "\n".join(body_lines)

    return (
        f"mkdir -p {shlex.quote(override_dir)} && "
        f"cat > {shlex.quote(override_path)} <<'SYSIBLE_EOF'\n"
        f"{body}\n"
        "SYSIBLE_EOF\n"
        "systemctl daemon-reload"
    )


def cmd_create_systemd_service(
    name: str,
    description: str = "",
    exec_start: str = "",
    working_directory: str = "",
    run_as_user: str = "root",
    restart_policy: str = "on-failure",
    after: str = "network.target",
    enable_now: bool = True,
) -> str:
    """Writes a brand-new unit file to /etc/systemd/system/<unit> and
    reloads systemd. With enable_now (the default), also enables +
    starts it immediately."""
    unit = _service_unit(name)
    if not exec_start.strip():
        raise ValueError("ExecStart command cannot be empty")

    body_lines = [
        "[Unit]",
        f"Description={description.strip() or name}",
    ]
    if after.strip():
        body_lines.append(f"After={after.strip()}")
    body_lines += [
        "",
        "[Service]",
        f"ExecStart={exec_start.strip()}",
        f"Restart={restart_policy.strip() or 'on-failure'}",
        f"User={run_as_user.strip() or 'root'}",
    ]
    if working_directory.strip():
        body_lines.append(f"WorkingDirectory={working_directory.strip()}")
    body_lines += [
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]

    body = "\n".join(body_lines)
    unit_path = f"/etc/systemd/system/{unit}"

    cmd = (
        f"cat > {shlex.quote(unit_path)} <<'SYSIBLE_EOF'\n"
        f"{body}\n"
        "SYSIBLE_EOF\n"
        "systemctl daemon-reload"
    )
    if enable_now:
        cmd += f" && systemctl enable --now {shlex.quote(unit)}"
    return cmd
