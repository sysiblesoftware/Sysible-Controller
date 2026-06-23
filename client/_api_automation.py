"""CRON & SYSTEMD TIMERS + HOST SOFTWARE MANAGEMENT dual-host command
builders - split out of client/api.py to keep individual file sizes
manageable. Imported via `from client._api_automation import *` at the
bottom of client/api.py.
"""
import re
import shlex


def cmd_list_cron_jobs() -> str:
    """Everything that can schedule a cron job on a typical Linux host:
    the connecting user's own crontab, /etc/crontab, and every file
    under /etc/cron.d."""
    return r"""
echo '== crontab -l (current user) =='
crontab -l 2>/dev/null || echo '(no crontab for this user, or crontab not available)'
echo
echo '== /etc/crontab =='
if [ -r /etc/crontab ]; then
    grep -vE '^[[:space:]]*(#|$)' /etc/crontab
else
    echo '(not present or not readable)'
fi
echo
echo '== /etc/cron.d/* =='
if [ -d /etc/cron.d ]; then
    for f in /etc/cron.d/*; do
        [ -f "$f" ] || continue
        echo "-- $f --"
        grep -vE '^[[:space:]]*(#|$)' "$f"
    done
else
    echo '(/etc/cron.d not present)'
fi
""".strip()


def cmd_add_cron_job(schedule: str, command: str, comment: str = "") -> str:
    """Appends one line to the connecting user's crontab."""
    schedule = (schedule or "").strip()
    command = (command or "").strip()
    if not schedule:
        raise ValueError("Schedule cannot be empty (e.g. '*/15 * * * *', or '@reboot')")
    if not command:
        raise ValueError("Command cannot be empty")
    if not schedule.startswith("@") and len(schedule.split()) != 5:
        raise ValueError(
            "Schedule must have exactly 5 fields (minute hour day month weekday), "
            "or be an '@'-style shortcut like @reboot/@daily"
        )

    line = f"{schedule} {command}"
    if comment.strip():
        line += f"  # {comment.strip()}"

    return (
        f"(crontab -l 2>/dev/null; echo {shlex.quote(line)}) | crontab - "
        "&& echo 'Cron job added.' "
        "|| echo 'Failed to add cron job (crontab not available on this host?).'"
    )


def cmd_remove_cron_job(match_text: str) -> str:
    """Removes every line in the connecting user's crontab containing
    match_text."""
    match_text = (match_text or "").strip()
    if not match_text:
        raise ValueError("Provide the exact line (or a unique snippet/comment) to remove")

    q = shlex.quote(match_text)
    return (
        f"count=$(crontab -l 2>/dev/null | grep -F {q} | wc -l | tr -d ' '); "
        f"if [ -z \"$count\" ] || [ \"$count\" -eq 0 ] 2>/dev/null; then "
        "echo 'No crontab line matched that text.'; "
        "else "
        f"crontab -l 2>/dev/null | grep -vF {q} | crontab - "
        f"&& echo \"Removed $count matching line(s).\" "
        "|| echo 'Failed to update crontab.'; "
        "fi"
    )


def _timer_unit(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Timer name cannot be empty")
    return name if name.endswith(".timer") else f"{name}.timer"


def cmd_list_timers() -> str:
    return (
        "systemctl list-timers --all --no-legend 2>/dev/null "
        "|| echo 'systemctl not available on this host'"
    )


def cmd_timer_status(name: str) -> str:
    return f"systemctl status {shlex.quote(_timer_unit(name))} --no-pager -l 2>&1; true"


def cmd_timer_start(name: str) -> str:
    return f"systemctl start {shlex.quote(_timer_unit(name))}"


def cmd_timer_stop(name: str) -> str:
    return f"systemctl stop {shlex.quote(_timer_unit(name))}"


def cmd_timer_enable(name: str) -> str:
    return f"systemctl enable {shlex.quote(_timer_unit(name))}"


def cmd_timer_disable(name: str) -> str:
    return f"systemctl disable {shlex.quote(_timer_unit(name))}"


def cmd_create_systemd_timer(
    name: str,
    exec_start: str,
    on_calendar: str = "",
    on_boot_sec: str = "",
    on_unit_active_sec: str = "",
    description: str = "",
    run_as_user: str = "root",
    enable_now: bool = True,
) -> str:
    """Writes a paired oneshot .service (the thing that actually runs
    exec_start) and .timer (the schedule) in one action. At least one
    of on_calendar / on_boot_sec / on_unit_active_sec must be set."""
    base = (name or "").strip()
    if base.endswith(".timer"):
        base = base[: -len(".timer")]
    if base.endswith(".service"):
        base = base[: -len(".service")]
    if not base:
        raise ValueError("Timer name cannot be empty")
    if not exec_start.strip():
        raise ValueError("ExecStart command cannot be empty")

    timer_lines = ["[Timer]"]
    have_schedule = False
    if on_calendar.strip():
        timer_lines.append(f"OnCalendar={on_calendar.strip()}")
        have_schedule = True
    if on_boot_sec.strip():
        timer_lines.append(f"OnBootSec={on_boot_sec.strip()}")
        have_schedule = True
    if on_unit_active_sec.strip():
        timer_lines.append(f"OnUnitActiveSec={on_unit_active_sec.strip()}")
        have_schedule = True
    if not have_schedule:
        raise ValueError("Set at least one of OnCalendar / OnBootSec / OnUnitActiveSec")
    timer_lines += ["", "[Install]", "WantedBy=timers.target"]

    service_lines = [
        "[Unit]",
        f"Description={description.strip() or base}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exec_start.strip()}",
        f"User={run_as_user.strip() or 'root'}",
    ]

    service_path = f"/etc/systemd/system/{base}.service"
    timer_path = f"/etc/systemd/system/{base}.timer"
    service_body = "\n".join(service_lines)
    timer_body = "\n".join(timer_lines)

    cmd = (
        f"cat > {shlex.quote(service_path)} <<'SYSIBLE_EOF'\n"
        f"{service_body}\n"
        "SYSIBLE_EOF\n"
        f"cat > {shlex.quote(timer_path)} <<'SYSIBLE_EOF'\n"
        f"{timer_body}\n"
        "SYSIBLE_EOF\n"
        "systemctl daemon-reload"
    )
    if enable_now:
        cmd += f" && systemctl enable --now {shlex.quote(base + '.timer')}"
    return cmd


def cmd_delete_timer(name: str, delete_service: bool = True) -> str:
    """Stops + disables the timer, removes its unit file, and (by
    default) removes the paired .service unit cmd_create_systemd_timer()
    wrote alongside it."""
    base = (name or "").strip()
    if base.endswith(".timer"):
        base = base[: -len(".timer")]
    if base.endswith(".service"):
        base = base[: -len(".service")]
    if not base:
        raise ValueError("Timer name cannot be empty")

    timer_unit = shlex.quote(f"{base}.timer")
    timer_path = shlex.quote(f"/etc/systemd/system/{base}.timer")

    cmd = (
        f"systemctl disable --now {timer_unit} 2>/dev/null; "
        f"rm -f {timer_path}"
    )
    if delete_service:
        service_path = shlex.quote(f"/etc/systemd/system/{base}.service")
        cmd += f"; rm -f {service_path}"
    cmd += "; systemctl daemon-reload && echo 'Timer removed.'"
    return cmd


# ---------------------------------------------------------
# HOST SOFTWARE MANAGEMENT - cross-distro package manager detection
# + package actions. Every command below detects which of dnf / yum /
# zypper / apt-get is actually present on the specific host it runs
# against at the moment it runs, so one click with a mixed-distro
# fleet checked does the right thing everywhere.
# ---------------------------------------------------------
def _pkgmgr_detect_fragment(var: str = "PKGMGR") -> str:
    """Shell fragment that sets $<var> to whichever of dnf / yum /
    zypper / apt-get is actually present on this host, preferring dnf
    over yum where a host has both."""
    return (
        f"if command -v dnf >/dev/null 2>&1; then {var}=dnf; "
        f"elif command -v yum >/dev/null 2>&1; then {var}=yum; "
        f"elif command -v zypper >/dev/null 2>&1; then {var}=zypper; "
        f"elif command -v apt-get >/dev/null 2>&1; then {var}=apt-get; "
        f"else echo 'No supported package manager found (looked for dnf, yum, zypper, apt-get).' >&2; exit 1; fi"
    )


def _pkgmgr_dispatch(rpm_cmd: str, zypper_cmd: str, apt_cmd: str) -> str:
    """Wraps the detection fragment above around three command
    templates and branches to whichever one matches."""
    detect = _pkgmgr_detect_fragment()
    return (
        f'{detect}; '
        f'if [ "$PKGMGR" = "dnf" ] || [ "$PKGMGR" = "yum" ]; then {rpm_cmd}; '
        f'elif [ "$PKGMGR" = "zypper" ]; then {zypper_cmd}; '
        f'else {apt_cmd}; fi'
    )


def _pkg_quote_list(text: str) -> str:
    """Splits whitespace-separated package names and shell-quotes each
    one individually."""
    return " ".join(shlex.quote(t) for t in text.split())


_SAFE_REPO_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_repo_alias(alias: str) -> str:
    alias = (alias or "").strip()
    if not alias:
        raise ValueError("Alias / Repository ID is required.")
    if not _SAFE_REPO_ALIAS_RE.match(alias):
        raise ValueError(
            "Alias / Repository ID may only contain letters, numbers, "
            "dots, dashes, and underscores."
        )
    return alias


def cmd_detect_host_environment() -> str:
    """Read-only: reports the OS distro (from /etc/os-release) and
    which package manager Host Software Management / Repository
    Management will use on this host."""
    detect = _pkgmgr_detect_fragment()
    return (
        f"{detect}; "
        "if [ -r /etc/os-release ]; then "
        ". /etc/os-release; "
        'echo "OS: ${PRETTY_NAME:-$NAME $VERSION}"; '
        "else echo 'OS: unknown (no /etc/os-release on this host)'; fi; "
        'echo "Package manager: $PKGMGR"'
    )


def cmd_install_packages(names: str) -> str:
    pkgs = _pkg_quote_list(names)
    if not pkgs:
        raise ValueError("Specify at least one package name.")
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" install -y {pkgs}',
        zypper_cmd=f'zypper --non-interactive install {pkgs}',
        apt_cmd=f'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs}',
    )


def cmd_install_local_package(remote_path: str) -> str:
    """Install a package FILE already sitting on the host (uploaded to
    `remote_path`, e.g. /tmp/foo.deb or /tmp/bar.rpm) via the host's package
    manager, so dependencies resolve from its repos. apt-get/dnf/yum take a
    local path directly; zypper needs --allow-unsigned-rpm for an unsigned
    file."""
    import shlex as _shlex
    remote_path = (remote_path or "").strip()
    if not remote_path:
        raise ValueError("No package file path given.")
    q = _shlex.quote(remote_path)
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" install -y {q}',
        zypper_cmd=f'zypper --non-interactive install --allow-unsigned-rpm {q}',
        apt_cmd=f'DEBIAN_FRONTEND=noninteractive apt-get install -y {q}',
    )


def cmd_remove_packages(names: str) -> str:
    pkgs = _pkg_quote_list(names)
    if not pkgs:
        raise ValueError("Specify at least one package name.")
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" remove -y {pkgs}',
        zypper_cmd=f'zypper --non-interactive remove {pkgs}',
        apt_cmd=f'DEBIAN_FRONTEND=noninteractive apt-get remove -y {pkgs}',
    )


def cmd_update_packages(names: str = "") -> str:
    """Upgrades the named package(s), or every installed package if
    left blank."""
    pkgs = _pkg_quote_list(names)
    if pkgs:
        return _pkgmgr_dispatch(
            rpm_cmd=f'"$PKGMGR" update -y {pkgs}',
            zypper_cmd=f'zypper --non-interactive update {pkgs}',
            apt_cmd=f'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y {pkgs}',
        )
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" upgrade -y',
        zypper_cmd='zypper --non-interactive update',
        apt_cmd='apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y',
    )


def cmd_query_package(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Specify a package name.")
    pkg = shlex.quote(name)
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" info {pkg} 2>/dev/null || rpm -qi {pkg}',
        zypper_cmd=f'zypper info {pkg}',
        apt_cmd=f'apt-cache show {pkg} 2>/dev/null || dpkg -s {pkg} 2>/dev/null || echo "Package not found: {name}"',
    )


def cmd_verify_package(name: str) -> str:
    """Checks an installed package's files against the package
    manager's records. RPM-based hosts use `rpm -V` directly.
    Debian/Ubuntu uses debsums, which isn't installed by default."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Specify a package name.")
    pkg = shlex.quote(name)

    rpm_verify = (
        f'out=$(rpm -V {pkg} 2>&1); '
        f'if [ -z "$out" ]; then echo "OK - {name}: no discrepancies found."; '
        f'else echo "$out"; fi'
    )

    return _pkgmgr_dispatch(
        rpm_cmd=rpm_verify,
        zypper_cmd=rpm_verify,
        apt_cmd=(
            'if ! command -v debsums >/dev/null 2>&1; then '
            'echo "debsums is not installed on this host - install it first '
            '(Install Packages: debsums), then re-run Verify Package Integrity."; '
            f'else out=$(debsums {pkg} 2>&1 | grep -v "OK$"); '
            f'if [ -z "$out" ]; then echo "OK - {name}: no discrepancies found."; '
            'else echo "$out"; fi; fi'
        ),
    )


def cmd_clean_package_cache() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" clean all',
        zypper_cmd='zypper clean --all',
        apt_cmd='apt-get clean',
    )


def cmd_search_packages(term: str) -> str:
    """Search the host's configured repositories for available packages
    matching `term` (name or summary), so you can find what to install
    without knowing the exact package name. Normalizes dnf/yum, zypper,
    and apt-cache search output to one "<name> <summary>" line per match
    (package name first, so the GUI can pull it out for an Install), and
    caps the result so a broad term doesn't return thousands of rows."""
    term = (term or "").strip()
    if not term:
        raise ValueError("Type something to search for first.")
    t = shlex.quote(term)
    detect = _pkgmgr_detect_fragment()
    return detect + "\n" + r"""{
  if [ "$PKGMGR" = "dnf" ] || [ "$PKGMGR" = "yum" ]; then
    "$PKGMGR" -q search """ + t + r""" 2>/dev/null | grep ' : ' | sed -e 's/\.[^. :]* : / /' -e 's/ : / /'
  elif [ "$PKGMGR" = "zypper" ]; then
    zypper -q search """ + t + r""" 2>/dev/null | awk -F'|' 'NF>=3 {n=$2; sub(/^ +/,"",n); sub(/ +$/,"",n); if(n!="" && n!="Name" && n !~ /^-+$/){s=$3; sub(/^ +/,"",s); sub(/ +$/,"",s); print n" "s}}'
  else
    apt-cache search """ + t + r""" 2>/dev/null | sed 's/ - / /'
  fi
} | head -n 300
"""


def cmd_list_installed_packages() -> str:
    """Newline-per-package list, parsed by the GUI's filterable
    "Installed Packages" picker. Reads dpkg/rpm directly instead of
    going through dnf/zypper/apt-get's own listing subcommands."""
    return (
        'if command -v dpkg-query >/dev/null 2>&1; then '
        "dpkg-query -W -f='${Package}\\n' 2>/dev/null; "
        'elif command -v rpm >/dev/null 2>&1; then '
        "rpm -qa --qf '%{NAME}\\n' 2>/dev/null; "
        'else echo "Neither dpkg nor rpm found on this host."; fi'
    )
