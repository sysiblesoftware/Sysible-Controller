"""Time Synchronization command builders (dual-host).

chrony / NTP configuration and verification, clock-drift troubleshooting,
and time-zone management. Plain POSIX sh, shlex.quote() on interpolated
values, explicit messages for missing tools, real exit codes.
"""
import re
import shlex

# NTP server addresses: hostnames/IPs separated by spaces.
_SERVERS_RE = re.compile(r"^[\w.\-: ]+$")


def cmd_timesync_status() -> str:
    return (
        "echo '== timedatectl =='; timedatectl 2>&1 || echo '(timedatectl not available)'; echo; "
        "if command -v chronyc >/dev/null 2>&1; then echo '== chronyc tracking =='; chronyc tracking 2>&1; "
        "elif command -v ntpq >/dev/null 2>&1; then echo '== ntpq -p =='; ntpq -p 2>&1; "
        "else echo '(neither chrony nor ntp is installed)'; fi"
    )


def _install_chrony_fragment() -> str:
    return (
        "if command -v apt-get >/dev/null 2>&1; then export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y chrony; "
        "elif command -v dnf >/dev/null 2>&1; then dnf install -y chrony; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y chrony; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install chrony; "
        "else echo 'No supported package manager found.' >&2; exit 1; fi"
    )


def _chrony_service_fragment() -> str:
    # Service is chronyd on RHEL/SUSE, chrony on Debian/Ubuntu.
    # No '2>&1 || true' on the enable: that swallowed a privilege failure into
    # exit 0, so the agent's run-as-user path never saw the error and never
    # retried under sudo (the service silently stayed disabled). Let it fail
    # loudly instead - 'enable --now' returns 0 when the unit is already
    # enabled, so this only errors on a real problem (missing unit / no root).
    return (
        "svc=chronyd; systemctl list-unit-files 2>/dev/null | grep -q '^chrony\\.service' && svc=chrony; "
        "systemctl enable --now \"$svc\""
    )


def cmd_configure_chrony() -> str:
    """Install chrony (if needed) and enable + start it."""
    return (
        _install_chrony_fragment() + " && " + _chrony_service_fragment() +
        " && echo 'chrony installed, enabled, and started.'"
    )


def cmd_set_ntp_servers(servers: str) -> str:
    """Point chrony at the given NTP servers and restart it."""
    servers = (servers or "").strip()
    if not servers or not _SERVERS_RE.match(servers):
        raise ValueError("Enter one or more NTP server hostnames/IPs, space-separated.")
    server_lines = "\\n".join(f"server {s} iburst" for s in servers.split())
    # &&-chain the privileged writes (cp/sed/append/restart) so the FIRST
    # permission failure aborts the whole command with a non-zero status and
    # the error on stderr - which is what lets the agent recognize it needs
    # root and retry under sudo. The old ';'-separated form let cp/sed fail and
    # still exit 0 (the trailing restart swallowed its own failure), so a
    # password-sudo host silently changed nothing while reporting success.
    return (
        "conf=/etc/chrony/chrony.conf; [ -f /etc/chrony.conf ] && conf=/etc/chrony.conf; "
        "if [ ! -f \"$conf\" ]; then echo \"chrony config not found - run Configure chrony first.\" >&2; exit 1; fi; "
        "cp \"$conf\" \"$conf.sysible.bak\" "
        "&& sed -i '/^\\(server\\|pool\\) /d' \"$conf\" "
        f"&& printf '{server_lines}\\n' >> \"$conf\" "
        "&& " + _chrony_service_fragment().replace("enable --now", "restart") +
        f" && echo 'Set NTP servers ({servers}) and restarted chrony (backup at $conf.sysible.bak).'"
    )


def cmd_verify_sync() -> str:
    return (
        "echo '== Sync state =='; timedatectl show -p NTP -p NTPSynchronized 2>/dev/null || timedatectl 2>&1; echo; "
        "if command -v chronyc >/dev/null 2>&1; then echo '== chronyc sources =='; chronyc sources -v 2>&1; echo; "
        "echo '== chronyc tracking =='; chronyc tracking 2>&1; "
        "elif command -v ntpstat >/dev/null 2>&1; then ntpstat 2>&1; "
        "else echo '(install chrony for detailed sync verification)'; fi"
    )


def cmd_troubleshoot_drift() -> str:
    return (
        "echo '== System vs hardware clock =='; "
        "echo \"System: $(date)\"; (hwclock 2>/dev/null && echo \"(hardware clock above)\") || echo '(hwclock unavailable)'; echo; "
        "if command -v chronyc >/dev/null 2>&1; then "
        "echo '== Drift / offset (chronyc tracking) =='; chronyc tracking 2>&1; echo; "
        "echo '== Source measurements =='; chronyc sourcestats -v 2>&1; "
        "else echo '(install chrony for drift analysis)'; fi"
    )


def cmd_set_timezone(tz: str) -> str:
    tz = (tz or "").strip()
    if not tz or not re.match(r"^[\w+\-]+(/[\w+\-]+)*$", tz):
        raise ValueError("Enter a valid time zone, e.g. America/New_York or UTC.")
    q = shlex.quote(tz)
    return (
        f"if timedatectl set-timezone {q} 2>&1; then echo 'Time zone set to {tz}.'; timedatectl 2>/dev/null | grep -i 'time zone'; "
        "else echo 'Failed to set time zone (is it a valid zone? see List Time Zones).' >&2; exit 1; fi"
    )


def cmd_list_timezones(filter_text: str = "") -> str:
    filter_text = (filter_text or "").strip()
    base = "{ timedatectl list-timezones 2>/dev/null || find /usr/share/zoneinfo -type f -printf '%P\\n' 2>/dev/null; }"
    if filter_text:
        if not re.match(r"^[\w/\-+ ]+$", filter_text):
            raise ValueError("Filter contains unexpected characters.")
        return f"{base} | grep -i {shlex.quote(filter_text)} || echo 'No matching time zones.'"
    return base
