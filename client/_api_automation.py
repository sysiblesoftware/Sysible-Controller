"""CRON & SYSTEMD TIMERS + HOST SOFTWARE MANAGEMENT dual-host command
builders - split out of client/api.py to keep individual file sizes
manageable. Imported via `from client._api_automation import *` at the
bottom of client/api.py.
"""
import re
import shlex


from client._pkgmgr import (
    pkgmgr_detect_fragment as _pkgmgr_detect_fragment,
    pkgmgr_dispatch as _pkgmgr_dispatch,
)


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


def _reject_newlines(value: str, field: str) -> str:
    """A stored systemd/unit field is written into a heredoc-delimited file
    (`cat > unit <<'SYSIBLE_EOF'`). An embedded newline — a body line that is
    exactly `SYSIBLE_EOF`, or an injected `ExecStart=`/extra directive — would
    break out of the heredoc and run as shell at the task's privilege. Reject
    CR/LF at ingest (mirrors the guard _api_repo.py already applies)."""
    v = value or ""
    if "\n" in v or "\r" in v:
        raise ValueError(f"{field} may not contain newlines")
    return v


def _safe_unit_base(name: str) -> str:
    """A unit basename joined into /etc/systemd/system/<name>. Reject path
    separators and traversal so the write can't be redirected to another file
    (e.g. `../../etc/cron.d/x`) — shlex.quote blocks shell injection but not '/'."""
    n = (name or "").strip()
    if "/" in n or "\\" in n or ".." in n:
        raise ValueError("Unit name may not contain '/', '\\' or '..'")
    return n


def _timer_unit(name: str) -> str:
    name = _safe_unit_base(name)
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
    base = _safe_unit_base(name)
    if base.endswith(".timer"):
        base = base[: -len(".timer")]
    if base.endswith(".service"):
        base = base[: -len(".service")]
    if not base:
        raise ValueError("Timer name cannot be empty")
    if not exec_start.strip():
        raise ValueError("ExecStart command cannot be empty")
    # These all flow into the unit-file heredoc body — reject newline breakout.
    for _fld, _val in (("ExecStart", exec_start), ("Description", description),
                       ("User", run_as_user), ("OnCalendar", on_calendar),
                       ("OnBootSec", on_boot_sec), ("OnUnitActiveSec", on_unit_active_sec)):
        _reject_newlines(_val, _fld)

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
    # Strip a supplied .timer/.service suffix, then run through _safe_unit_base so a
    # traversal like `../../../etc/cron.d/foo` can't redirect the rm -f outside
    # /etc/systemd/system/ (shlex.quote blocks shell injection but NOT the '/' / '..').
    # Its sibling builders (cmd_create_systemd_timer, _timer_unit) already do this.
    base = (name or "").strip()
    if base.endswith(".timer"):
        base = base[: -len(".timer")]
    if base.endswith(".service"):
        base = base[: -len(".service")]
    base = _safe_unit_base(base)
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
# ENVIRONMENT & SHELL - persistent, system-wide environment variables and
# shell aliases, managed through drop-in files under /etc/profile.d so every
# NEW login shell picks them up (existing sessions must re-login / re-source).
# The variable/alias NAME is charset-whitelisted (so it is safe to interpolate
# into the sed match regex); the VALUE/COMMAND is shlex.quote'd into the file
# line and CR/LF-rejected, then that whole line is written with a quoted
# `printf` - so a value can neither break out into the shell nor inject a
# second profile.d directive. Idempotent: setting an existing name replaces its
# line rather than appending a duplicate.
# ---------------------------------------------------------
_SYSIBLE_ENV_FILE = "/etc/profile.d/sysible-env.sh"
_SYSIBLE_ALIAS_FILE = "/etc/profile.d/sysible-aliases.sh"
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _validate_env_key(name: str) -> str:
    key = (name or "").strip()
    if not _ENV_KEY_RE.match(key):
        raise ValueError(
            "Environment variable name must start with a letter or underscore and "
            "contain only letters, digits, and underscores.")
    return key


def _validate_alias_name(name: str) -> str:
    a = (name or "").strip()
    if not _ALIAS_NAME_RE.match(a):
        raise ValueError(
            "Alias name must start with a letter or underscore and contain only "
            "letters, digits, underscores, or hyphens.")
    return a


def cmd_set_env_var(name: str, value: str = "") -> str:
    """Persist a system-wide environment variable in /etc/profile.d/sysible-env.sh
    (export NAME=VALUE), replacing any existing definition of NAME. Applies to new
    login shells."""
    key = _validate_env_key(name)
    _reject_newlines(value or "", "value")
    f = shlex.quote(_SYSIBLE_ENV_FILE)
    line = f"export {key}={shlex.quote(value or '')}"
    return (
        f"touch {f} && chmod 644 {f}; "
        f"sed -i '/^export {key}=/d' {f}; "
        f"printf '%s\\n' {shlex.quote(line)} >> {f} && "
        f"echo 'Set {key} in {_SYSIBLE_ENV_FILE} (applies to new login shells).'"
    )


def cmd_unset_env_var(name: str) -> str:
    """Remove a system-wide environment variable previously set via
    cmd_set_env_var (deletes its line from /etc/profile.d/sysible-env.sh)."""
    key = _validate_env_key(name)
    f = shlex.quote(_SYSIBLE_ENV_FILE)
    return (
        f"if [ -f {f} ]; then sed -i '/^export {key}=/d' {f} && "
        f"echo 'Removed {key} from {_SYSIBLE_ENV_FILE} (new login shells only).'; "
        f"else echo '{_SYSIBLE_ENV_FILE} does not exist; nothing to remove.'; fi"
    )


def cmd_set_alias(name: str, command: str) -> str:
    """Persist a system-wide shell alias in /etc/profile.d/sysible-aliases.sh
    (alias NAME='COMMAND'), replacing any existing alias of NAME. Applies to new
    login shells."""
    a = _validate_alias_name(name)
    _reject_newlines(command or "", "command")
    if not (command or "").strip():
        raise ValueError("Alias command cannot be empty.")
    f = shlex.quote(_SYSIBLE_ALIAS_FILE)
    line = f"alias {a}={shlex.quote(command)}"
    return (
        f"touch {f} && chmod 644 {f}; "
        f"sed -i '/^alias {a}=/d' {f}; "
        f"printf '%s\\n' {shlex.quote(line)} >> {f} && "
        f"echo 'Set alias {a} in {_SYSIBLE_ALIAS_FILE} (applies to new login shells).'"
    )


def cmd_remove_alias(name: str) -> str:
    """Remove a system-wide shell alias previously set via cmd_set_alias
    (deletes its line from /etc/profile.d/sysible-aliases.sh)."""
    a = _validate_alias_name(name)
    f = shlex.quote(_SYSIBLE_ALIAS_FILE)
    return (
        f"if [ -f {f} ]; then sed -i '/^alias {a}=/d' {f} && "
        f"echo 'Removed alias {a} from {_SYSIBLE_ALIAS_FILE}.'; "
        f"else echo '{_SYSIBLE_ALIAS_FILE} does not exist; nothing to remove.'; fi"
    )


def cmd_list_env_and_aliases() -> str:
    """Show the Sysible-managed environment variables and shell aliases (the
    contents of both drop-in files), so an operator can see what's set."""
    ef = shlex.quote(_SYSIBLE_ENV_FILE)
    af = shlex.quote(_SYSIBLE_ALIAS_FILE)
    return (
        f"echo '# {_SYSIBLE_ENV_FILE}'; [ -f {ef} ] && cat {ef} || echo '(none)'; "
        f"echo; echo '# {_SYSIBLE_ALIAS_FILE}'; [ -f {af} ] && cat {af} || echo '(none)'"
    )


# ---------------------------------------------------------
# HOST SOFTWARE MANAGEMENT - cross-distro package manager detection
# + package actions. Every command below detects which of dnf / yum /
# zypper / apt-get is actually present on the specific host it runs
# against at the moment it runs, so one click with a mixed-distro
# fleet checked does the right thing everywhere.
# ---------------------------------------------------------


def _pkg_quote_list(text: str) -> str:
    """Split whitespace-separated package names and shell-quote each one.

    Rejects any token beginning with '-'. shlex.quote stops shell-metacharacter
    injection but NOT *option* injection: a token like ``-o`` would otherwise be
    parsed by apt-get/dnf as an OPTION rather than a package operand
    (``-o DPkg::Pre-Invoke=<cmd>`` runs an arbitrary command as root and hides it
    from the command-policy scan of the built string). Callers additionally place
    a ``--`` end-of-options separator before the operands as a second layer."""
    out = []
    for tok in (text or "").split():
        if tok.startswith("-"):
            raise ValueError(
                f"Invalid package name {tok!r}: package names cannot begin with "
                "'-' (it would be interpreted as a command-line option).")
        out.append(shlex.quote(tok))
    return " ".join(out)


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
        rpm_cmd=f'"$PKGMGR" install -y -- {pkgs}',
        zypper_cmd=f'zypper --non-interactive install -- {pkgs}',
        apt_cmd=f'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y -- {pkgs}',
        pacman_cmd=f'pacman -Sy --needed --noconfirm {pkgs}',
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
        # Arch installs a local package FILE (.pkg.tar.zst) with -U.
        pacman_cmd=f'pacman -U --noconfirm {q}',
    )


def cmd_remove_packages(names: str) -> str:
    pkgs = _pkg_quote_list(names)
    if not pkgs:
        raise ValueError("Specify at least one package name.")
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" remove -y -- {pkgs}',
        zypper_cmd=f'zypper --non-interactive remove -- {pkgs}',
        apt_cmd=f'DEBIAN_FRONTEND=noninteractive apt-get remove -y -- {pkgs}',
        pacman_cmd=f'pacman -R --noconfirm {pkgs}',
    )


# Opt-in resolver flags for the RPM (dnf/yum) update command, for when an update
# dead-ends on a dependency conflict (dnf itself suggests these). WHITELIST ONLY -- a
# free-form flag string would be a shell-injection vector, so only these known tokens map
# to a fixed flag and anything else is dropped. dnf-centric; that's where these conflicts
# arise. (`--nobest` is dnf-only, harmless-to-omit on yum.)
_RPM_UPDATE_FLAGS = {
    "nobest": "--nobest",
    "skip-broken": "--skip-broken",
    "allowerasing": "--allowerasing",
}


def _rpm_update_flags(flags: str) -> str:
    """Map an opt-in, space/comma-separated flag list to a VALIDATED dnf/yum flag string.
    Accepts tokens with or without leading dashes (e.g. 'nobest' or '--nobest'); unknown
    tokens are silently dropped, so a hostile value can never inject shell."""
    out = []
    for tok in (flags or "").replace(",", " ").split():
        mapped = _RPM_UPDATE_FLAGS.get(tok.strip().lstrip("-"))
        if mapped and mapped not in out:
            out.append(mapped)
    return (" " + " ".join(out)) if out else ""


def cmd_update_packages(names: str = "", flags: str = "") -> str:
    """Upgrades the named package(s), or every installed package if left blank.

    `flags` is an opt-in, whitelisted set of dnf/yum resolver flags (nobest, skip-broken,
    allowerasing) for when an update dead-ends on a dependency conflict -- applied only to
    the RPM command; ignored for apt/zypper."""
    pkgs = _pkg_quote_list(names)
    rf = _rpm_update_flags(flags)
    if pkgs:
        return _pkgmgr_dispatch(
            rpm_cmd=f'"$PKGMGR" update -y{rf} -- {pkgs}',
            zypper_cmd=f'zypper --non-interactive update -- {pkgs}',
            apt_cmd=f'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y -- {pkgs}',
            pacman_cmd=f'pacman -Sy --noconfirm {pkgs}',
        )
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" upgrade -y{rf}',
        zypper_cmd='zypper --non-interactive update',
        apt_cmd='apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y',
        pacman_cmd='pacman -Syu --noconfirm',
    )


def cmd_package_status(names: str) -> str:
    """Read-only: for each named package, report whether it's installed on the
    host and its version, one clean machine-parseable line per package:
        SYSIBLE_PKG <name> installed <version>
        SYSIBLE_PKG <name> absent
    This is what makes the multi-host view meaningful — the console turns these
    lines into a per-host installed/not-installed tally instead of a wall of
    package-manager text."""
    pkgs = _pkg_quote_list(names)
    if not pkgs:
        raise ValueError("Specify at least one package name.")
    # Single quotes around the dpkg/rpm format specs so the shell leaves
    # ${db:...}/%{...} for the tool to expand; double quotes on echo so $p/$v
    # do expand.
    apt = (
        "for p in " + pkgs + "; do "
        "st=$(dpkg-query -W -f='${db:Status-Status}' \"$p\" 2>/dev/null); "
        "v=$(dpkg-query -W -f='${Version}' \"$p\" 2>/dev/null); "
        "if [ \"$st\" = installed ] && [ -n \"$v\" ]; then echo \"SYSIBLE_PKG $p installed $v\"; "
        "else echo \"SYSIBLE_PKG $p absent\"; fi; done"
    )
    rpm = (
        "for p in " + pkgs + "; do "
        "if v=$(rpm -q --qf '%{VERSION}-%{RELEASE}' \"$p\" 2>/dev/null); "
        "then echo \"SYSIBLE_PKG $p installed $v\"; else echo \"SYSIBLE_PKG $p absent\"; fi; done"
    )
    pacman = (
        "for p in " + pkgs + "; do "
        "v=$(pacman -Q \"$p\" 2>/dev/null | awk '{print $2}'); "
        "if [ -n \"$v\" ]; then echo \"SYSIBLE_PKG $p installed $v\"; "
        "else echo \"SYSIBLE_PKG $p absent\"; fi; done"
    )
    return _pkgmgr_dispatch(rpm_cmd=rpm, zypper_cmd=rpm, apt_cmd=apt, pacman_cmd=pacman)


def cmd_query_package(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Specify a package name.")
    pkg = shlex.quote(name)
    return _pkgmgr_dispatch(
        rpm_cmd=f'"$PKGMGR" info {pkg} 2>/dev/null || rpm -qi {pkg}',
        zypper_cmd=f'zypper info {pkg}',
        apt_cmd=f'apt-cache show {pkg} 2>/dev/null || dpkg -s {pkg} 2>/dev/null || echo "Package not found:" {pkg}',
        # -Qi: installed package info; -Si: repo info for a not-yet-installed one.
        pacman_cmd=f'pacman -Qi {pkg} 2>/dev/null || pacman -Si {pkg} 2>/dev/null || echo "Package not found:" {pkg}',
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
        f'if [ -z "$out" ]; then echo "OK -" {pkg} "- no discrepancies found."; '
        f'else echo "$out"; fi'
    )

    pacman_verify = (
        f'out=$(pacman -Qkk {pkg} 2>&1 | grep -vE "0 altered files|, 0 missing files"); '
        f'if [ -z "$out" ]; then echo "OK -" {pkg} "- no discrepancies found."; '
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
            f'if [ -z "$out" ]; then echo "OK -" {pkg} "- no discrepancies found."; '
            'else echo "$out"; fi; fi'
        ),
        pacman_cmd=pacman_verify,
    )


def cmd_clean_package_cache() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" clean all',
        zypper_cmd='zypper clean --all',
        apt_cmd='apt-get clean',
        pacman_cmd='pacman -Sc --noconfirm',
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
  elif [ "$PKGMGR" = "pacman" ]; then
    pacman -Ss """ + t + r""" 2>/dev/null | awk '/^[^[:space:]]/{split($1,a,"/"); n=a[2]; getline d; sub(/^[[:space:]]+/,"",d); print n" "d}'
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
        'elif command -v pacman >/dev/null 2>&1; then '
        "pacman -Qq 2>/dev/null; "
        'else echo "Neither dpkg, rpm nor pacman found on this host."; fi'
    )


# ----------------------------------------------------------------------
# Major / distribution release upgrade (leapp, dnf system-upgrade,
# do-release-upgrade, zypper migration / dup)
# ----------------------------------------------------------------------
# A `target` release/codename is interpolated into the shell (--releasever=,
# --target, leap $releasever). It is therefore STRICTLY whitelisted: it must
# start alphanumeric and contain only letters, digits, dot, dash or underscore.
# That set covers every real value ("9", "9.4", "40", "15.6", "noble") and
# excludes spaces and every shell metacharacter, so `target` can never inject
# shell. Anything else is rejected before it reaches a command string.
_RELEASE_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def _validate_release_target(target: str) -> str:
    """Return the trimmed target if it is empty or matches the whitelist; raise
    otherwise. Per-distro *requiredness* is enforced in the generated script (it
    depends on the host's distro, only known at run time), so empty is allowed
    here — format is all we can safely check up front."""
    t = (target or "").strip()
    if not t:
        return ""
    if not _RELEASE_TARGET_RE.match(t):
        raise ValueError(
            "Invalid target release. Use only letters, digits, dot, dash or "
            "underscore — e.g. '9', '9.4', '40', '15.6', or an Ubuntu codename "
            "like 'noble'."
        )
    return t


def _release_upgrade_script(target: str, assess: bool) -> str:
    """Build a distro-aware major/release-upgrade (or, when ``assess`` is True, a
    non-destructive pre-flight assessment) script.

    The mechanism is chosen from /etc/os-release at run time because it varies by
    distro *family*, which `dnf`-vs-`zypper`-vs-`apt` alone can't distinguish:
      * RHEL family (rhel/rocky/almalinux/centos/oracle) -> Leapp
      * Fedora                                           -> dnf system-upgrade (needs target releasever)
      * Ubuntu                                           -> do-release-upgrade
      * Debian                                           -> not automated (guidance only; sources-list rewrite)
      * openSUSE Leap                                    -> zypper dup at --releasever (needs target)
      * openSUSE Tumbleweed                              -> zypper dup (rolling; no target)
      * SLES/SLED                                        -> zypper migration (needs SCC/RMT registration)

    Nothing here auto-reboots: RHEL/Fedora stage the transaction and print the
    reboot command; the operator (or a follow-up rollout) reboots deliberately.
    `target` is whitelisted by _validate_release_target() so it is safe to inline.
    """
    t = _validate_release_target(target)
    mode = "assess" if assess else "upgrade"
    # `set -e` is deliberately omitted: several assessment commands (e.g.
    # `do-release-upgrade -c`) return non-zero benignly, and each branch does its
    # own error handling.
    return f'''MODE={mode}
TARGET="{t}"
if [ ! -r /etc/os-release ]; then echo "Cannot detect OS: /etc/os-release is missing." >&2; exit 1; fi
. /etc/os-release
echo "Current OS: ${{PRETTY_NAME:-${{ID:-unknown}}}}"

# Resolve the distro family (explicit IDs first, then ID_LIKE as a fallback).
family=unknown
case "${{ID:-}}" in
  fedora) family=fedora ;;
  rhel|centos|rocky|almalinux|ol|oracle|scientific) family=rhel ;;
  ubuntu) family=ubuntu ;;
  debian|raspbian) family=debian ;;
  opensuse-leap|opensuse|opensuse-microos) family=leap ;;
  opensuse-tumbleweed) family=tumbleweed ;;
  sles|sled|suse) family=sles ;;
  *)
    case " ${{ID_LIKE:-}} " in
      *rhel*|*centos*|*fedora*) family=rhel ;;
      *suse*) family=leap ;;
      *ubuntu*) family=ubuntu ;;
      *debian*) family=debian ;;
    esac ;;
esac
echo "Upgrade family: $family (mode: $MODE)"
echo

need_target() {{
  if [ -z "$TARGET" ]; then
    echo "This host ($family) needs an explicit target version — re-run with the Target field set (e.g. $1)." >&2
    exit 2
  fi
}}

case "$family" in
  rhel)
    PKGMGR="$(command -v dnf 2>/dev/null || command -v yum 2>/dev/null)"
    if [ -z "$PKGMGR" ]; then echo "No dnf/yum found — cannot run Leapp." >&2; exit 1; fi
    if ! "$PKGMGR" install -y leapp-upgrade; then
      echo "Could not install leapp-upgrade. Ensure the OS repos/subscription that ship Leapp are enabled." >&2
      exit 1
    fi
    if [ "$MODE" = assess ]; then
      echo "Running Leapp pre-upgrade assessment (no changes to the running system)…"
      leapp preupgrade ${{TARGET:+--target "$TARGET"}} || true
      echo
      echo "Review the report and resolve every inhibitor BEFORE upgrading:"
      echo "  /var/log/leapp/leapp-report.txt"
    else
      echo "Running Leapp upgrade (this refuses to proceed if inhibitors remain)…"
      leapp upgrade ${{TARGET:+--target "$TARGET"}}
      echo
      echo "Leapp staged the upgrade. REBOOT to run the actual upgrade transaction:"
      echo "  sudo shutdown -r now"
      echo "The first boot runs the RPM transaction in the upgrade initramfs — do NOT interrupt it."
    fi
    ;;

  fedora)
    need_target "40"
    if [ "$MODE" = assess ]; then
      echo "Plan: dnf system-upgrade to Fedora $TARGET. Assessment refreshes metadata only."
      dnf -y --refresh check-update >/dev/null 2>&1 || true
      echo "Ready. Run the upgrade action to download Fedora $TARGET, then reboot to apply."
    else
      dnf -y --refresh upgrade
      # Install the plugin matching the ACTIVE dnf: on Fedora 41+ /usr/bin/dnf is
      # dnf5, which does not load the dnf4 Python plugin - installing the dnf4
      # `dnf-plugin-system-upgrade` there succeeds but never registers the
      # `system-upgrade` command, so download fails with "unknown command". Key off
      # the backend rather than relying on install-failure fallthrough.
      if dnf --version 2>/dev/null | grep -q '^dnf5'; then
        dnf -y install dnf5-plugin-system-upgrade || true
      else
        dnf -y install dnf-plugin-system-upgrade || true
      fi
      dnf -y system-upgrade download --releasever="$TARGET"
      echo
      echo "Downloaded Fedora $TARGET. Apply it with a reboot into the offline upgrade:"
      echo "  sudo dnf system-upgrade reboot"
    fi
    ;;

  ubuntu)
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y update-manager-core >/dev/null 2>&1 || true
    if [ "$MODE" = assess ]; then
      echo "Checking for an available Ubuntu release upgrade…"
      if do-release-upgrade -c; then :; else
        echo "No new release offered (or /etc/update-manager/release-upgrades Prompt= policy blocks it)."
      fi
    else
      apt-get update && apt-get -y upgrade
      echo "Starting do-release-upgrade (non-interactive)…"
      do-release-upgrade -f DistUpgradeViewNonInteractive
    fi
    ;;

  debian)
    echo "Debian has no do-release-upgrade. Major-version upgrades are done by:" >&2
    echo "  1) point /etc/apt/sources.list (and sources.list.d/*) at the new codename (e.g. bookworm)" >&2
    echo "  2) apt-get update && apt-get -y upgrade && apt-get -y full-upgrade" >&2
    echo "Sysible does not auto-rewrite Debian sources — run this through your change process." >&2
    [ "$MODE" = assess ] && exit 0 || exit 2
    ;;

  tumbleweed)
    zypper --non-interactive refresh
    if [ "$MODE" = assess ]; then
      echo "openSUSE Tumbleweed is rolling — assessing with a dry-run dup…"
      zypper --non-interactive dup --dry-run
    else
      zypper --non-interactive dup
      echo
      echo "Distribution upgrade applied. Reboot to boot the new kernel: sudo shutdown -r now"
    fi
    ;;

  leap)
    need_target "15.6"
    zypper --releasever="$TARGET" refresh
    if [ "$MODE" = assess ]; then
      echo "Assessing openSUSE Leap $TARGET distribution upgrade (dry-run)…"
      zypper --releasever="$TARGET" --non-interactive dup --dry-run --allow-vendor-change
    else
      zypper --releasever="$TARGET" --non-interactive dup --allow-vendor-change
      echo
      echo "Distribution upgrade to Leap $TARGET applied. Reboot to boot the new kernel: sudo shutdown -r now"
    fi
    ;;

  sles)
    if ! command -v zypper >/dev/null 2>&1; then echo "zypper not found." >&2; exit 1; fi
    if [ "$MODE" = assess ]; then
      echo "Assessing SLES migration (needs SCC/RMT registration). Available targets:"
      zypper --non-interactive migration --dry-run || {{
        echo "Migration query failed — confirm the system is registered (SUSEConnect --status-text)." >&2; exit 1; }}
    else
      echo "Running SLES migration (zypper migration)…"
      zypper --non-interactive migration --auto-agree-with-licenses
      echo
      echo "Migration complete. Reboot to boot the new kernel: sudo shutdown -r now"
    fi
    ;;

  *)
    echo "Release upgrade is not supported/automated for this distro (ID=${{ID:-unknown}})." >&2
    exit 1
    ;;
esac'''


def cmd_release_upgrade_check(target: str = "") -> str:
    """Non-destructive pre-flight assessment of a major/distribution upgrade:
    detects the distro family and runs its assessment step (Leapp preupgrade,
    do-release-upgrade -c, zypper dup --dry-run, …). RHEL's Leapp path installs
    the leapp-upgrade tooling to produce its report; nothing else changes the
    running system, and nothing reboots."""
    return _release_upgrade_script(target, assess=True)


def cmd_release_upgrade(target: str = "") -> str:
    """Perform a major/distribution release upgrade with the right mechanism for
    the host's distro (Leapp, dnf system-upgrade, do-release-upgrade, zypper
    migration/dup). Fedora and openSUSE Leap require an explicit `target`
    version; RHEL/Ubuntu/Tumbleweed/SLES auto-select. Nothing auto-reboots — the
    RPM-family paths stage the transaction and print the reboot command."""
    return _release_upgrade_script(target, assess=False)
