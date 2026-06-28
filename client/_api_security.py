"""SECURITY ADMINISTRATION dual-host command builders - split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_security import *` at the bottom of client/api.py.

Covers SELinux (mode/booleans, denial troubleshooting, file contexts,
policy modules), SSH hardening (sshd options, root login, key-based
auth, key rotation), audit logs, failed-login review, security
updates, password policy, baseline system hardening, and vulnerability
scans. Same rules as the rest of this split: plain POSIX sh,
shlex.quote() (or explicit validation) on anything interpolated, a
clear "X is not installed" message instead of a bare command-not-
found, and explicit guardrails before anything destructive (host key
regeneration, ruleset/account changes).
"""
import shlex


def _validate_identifier(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError(f"{label} may only contain letters, numbers, dashes, and underscores.")
    return value


def _validate_username(value: str, label: str = "User") -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if not all(c.isalnum() or c in "_-." for c in value) or value[0] == "-":
        raise ValueError(f"{label} may only contain letters, numbers, dots, dashes, and underscores.")
    return value


def _validate_path(value: str, label: str = "Path") -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if "\x00" in value:
        raise ValueError(f"{label} contains an invalid character.")
    return value


def _validate_nonempty_line(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} cannot span multiple lines.")
    return value


def _validate_int_range(value, lo: int, hi: int, label: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not (lo <= n <= hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


_AUDITD_MISSING = (
    "if ! command -v ausearch >/dev/null 2>&1; then "
    "echo 'The audit package (ausearch/auditctl) is not installed on this host (package: audit).' >&2; exit 1; fi; "
)


# ---------------------------------------------------------
# cross-distro package manager detection, mirrors the helper of the
# same name in client/_api_automation.py - kept as a private local
# copy rather than imported, since none of the _api_*.py modules
# import from one another (each stays self-contained).
# ---------------------------------------------------------
def _pkgmgr_detect_fragment(var: str = "PKGMGR") -> str:
    return (
        f"if command -v dnf >/dev/null 2>&1; then {var}=dnf; "
        f"elif command -v yum >/dev/null 2>&1; then {var}=yum; "
        f"elif command -v zypper >/dev/null 2>&1; then {var}=zypper; "
        f"elif command -v apt-get >/dev/null 2>&1; then {var}=apt-get; "
        f"else echo 'No supported package manager found (looked for dnf, yum, zypper, apt-get).' >&2; exit 1; fi"
    )


def _pkgmgr_dispatch(rpm_cmd: str, zypper_cmd: str, apt_cmd: str) -> str:
    detect = _pkgmgr_detect_fragment()
    return (
        f'{detect}; '
        f'if [ "$PKGMGR" = "dnf" ] || [ "$PKGMGR" = "yum" ]; then {rpm_cmd}; '
        f'elif [ "$PKGMGR" = "zypper" ]; then {zypper_cmd}; '
        f'else {apt_cmd}; fi'
    )


# ===========================================================
# Configure SELinux
# ===========================================================
_VALID_SELINUX_MODES = {"enforcing", "permissive", "disabled"}


def _validate_selinux_mode(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in _VALID_SELINUX_MODES:
        raise ValueError(f"Mode must be one of: {', '.join(sorted(_VALID_SELINUX_MODES))}")
    return value


_SELINUX_MISSING = (
    "if ! command -v getenforce >/dev/null 2>&1; then "
    "echo 'SELinux userspace tools are not installed on this host (package: libselinux-utils / policycoreutils).' >&2; exit 1; fi; "
)


def cmd_install_selinux_tools() -> str:
    """Install the SELinux userspace tools every other action on this tab
    needs - getenforce/setenforce, semanage, getsebool/setsebool, restorecon,
    audit2allow, sesearch. Package names differ per distro. On Debian/Ubuntu
    (AppArmor by default) this installs the tools but does NOT switch the host
    to SELinux - that's a separate, reboot-level decision."""
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" install -y policycoreutils policycoreutils-python-utils setools-console libselinux-utils',
        zypper_cmd="zypper --non-interactive install policycoreutils policycoreutils-python-utils setools-console libselinux-tools",
        apt_cmd="apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y policycoreutils selinux-utils setools",
    ) + " && echo 'SELinux userspace tools installed.'"


def cmd_selinux_status() -> str:
    return (
        _SELINUX_MISSING +
        "echo '-- sestatus --' && sestatus 2>&1; "
        "echo; echo '-- Config file (/etc/selinux/config) --' && "
        "grep -E '^(SELINUX|SELINUXTYPE)=' /etc/selinux/config 2>&1"
    )


def cmd_set_selinux_mode(mode: str) -> str:
    """Runtime-only switch between enforcing/permissive via setenforce.
    Does not touch /etc/selinux/config, so it reverts on reboot - use
    cmd_set_selinux_config_mode() for a persistent change. Switching to
    "disabled" cannot be done at runtime; use the config-mode command
    and reboot."""
    mode = _validate_selinux_mode(mode)
    if mode == "disabled":
        raise ValueError(
            "SELinux cannot be set to disabled at runtime. Use the persistent config "
            "option instead, then reboot the host."
        )
    flag = "1" if mode == "enforcing" else "0"
    return (
        _SELINUX_MISSING +
        f"setenforce {flag} 2>&1 && echo 'SELinux runtime mode set to {mode}.'"
    )


def cmd_set_selinux_config_mode(mode: str) -> str:
    """Persists the SELinux mode in /etc/selinux/config. Takes effect
    immediately for enforcing/permissive (also applied at runtime);
    switching to/from disabled requires a reboot."""
    mode = _validate_selinux_mode(mode)
    cmd = (
        _SELINUX_MISSING +
        "cp /etc/selinux/config /etc/selinux/config.bak 2>&1 && "
        f"sed -i -E 's/^SELINUX=.*/SELINUX={mode}/' /etc/selinux/config 2>&1 "
        f"&& echo 'Persistent SELinux mode set to {mode} (config saved to /etc/selinux/config.bak).'"
    )
    if mode == "disabled":
        cmd += "; echo 'Reboot required for SELinux to actually go disabled.'"
    else:
        cmd += f" && setenforce {'1' if mode == 'enforcing' else '0'} 2>/dev/null"
    return cmd


def cmd_selinux_list_booleans(filter_text: str = "") -> str:
    filter_text = (filter_text or "").strip()
    if filter_text:
        return _SELINUX_MISSING + f"getsebool -a 2>&1 | grep -i {shlex.quote(filter_text)}"
    return _SELINUX_MISSING + "getsebool -a 2>&1"


def cmd_set_selinux_boolean(name: str, enabled: bool, permanent: bool = True) -> str:
    name = _validate_identifier(name, "Boolean name")
    value = "on" if enabled else "off"
    flag = "-P " if permanent else ""
    scope = "persistently" if permanent else "for this boot only"
    return (
        _SELINUX_MISSING +
        f"setsebool {flag}{shlex.quote(name)} {value} 2>&1 "
        f"&& echo 'Boolean {name} set to {value} ({scope}).'"
    )


# ===========================================================
# Troubleshoot SELinux denials
# ===========================================================
def cmd_selinux_recent_denials(lines: int = 50) -> str:
    lines = _validate_int_range(lines, 1, 5000, "Line count")
    return (
        _AUDITD_MISSING +
        f"ausearch -m avc,user_avc -ts recent -i 2>&1 | tail -n {lines}"
    )


def cmd_selinux_explain_denials(lines: int = 50) -> str:
    """Runs the recent AVC denials back through audit2why, which
    annotates each with a human-readable cause and (where one exists)
    the exact `audit2allow`/`semanage` fix."""
    lines = _validate_int_range(lines, 1, 5000, "Line count")
    return (
        _AUDITD_MISSING +
        "if ! command -v audit2why >/dev/null 2>&1; then "
        "echo 'audit2why is not installed on this host (package: policycoreutils-python-utils).' >&2; exit 1; fi; "
        f"ausearch -m avc,user_avc -ts recent -i 2>&1 | tail -n {lines} | audit2why 2>&1"
    )


def cmd_selinux_journal_denials(lines: int = 50) -> str:
    """Falls back to setroubleshoot's journal entries, for hosts where
    auditd itself isn't running but setroubleshootd still is."""
    lines = _validate_int_range(lines, 1, 5000, "Line count")
    return f"journalctl -t setroubleshoot --no-pager -n {lines} 2>&1"


# ===========================================================
# Restore file contexts
# ===========================================================
def cmd_selinux_get_context(path: str) -> str:
    path = _validate_path(path)
    return _SELINUX_MISSING + f"ls -lZd {shlex.quote(path)} 2>&1"


def cmd_selinux_restore_context(path: str, recursive: bool = False) -> str:
    path = _validate_path(path)
    flag = "-R " if recursive else ""
    scope = "recursively" if recursive else "(single path)"
    return (
        _SELINUX_MISSING +
        f"restorecon {flag}-v {shlex.quote(path)} 2>&1 "
        f"&& printf 'Restored SELinux file context for %s {scope}.\\n' {shlex.quote(path)}"
    )


# ===========================================================
# Create SELinux policies
# ===========================================================
_SEMANAGE_MISSING = (
    "if ! command -v semanage >/dev/null 2>&1; then "
    "echo 'semanage is not installed on this host (package: policycoreutils-python-utils).' >&2; exit 1; fi; "
)


def cmd_selinux_list_fcontext(pattern: str = "") -> str:
    pattern = (pattern or "").strip()
    if pattern:
        return _SEMANAGE_MISSING + f"semanage fcontext -l 2>&1 | grep -i {shlex.quote(pattern)}"
    return _SEMANAGE_MISSING + "semanage fcontext -l 2>&1"


def cmd_selinux_add_fcontext(path_regex: str, file_type: str) -> str:
    """`path_regex` is a semanage-style path spec, e.g.
    '/srv/myapp(/.*)?'. `file_type` is the SELinux type to assign,
    e.g. 'httpd_sys_content_t'."""
    path_regex = _validate_nonempty_line(path_regex, "Path spec")
    file_type = _validate_identifier(file_type, "SELinux type")
    q_path = shlex.quote(path_regex)
    return (
        _SEMANAGE_MISSING +
        f"semanage fcontext -a -t {file_type} {q_path} 2>&1 "
        f"&& restorecon -Rv {q_path} 2>&1; "
        f"printf 'File context rule added for %s -> {file_type}.\\n' {q_path}"
    )


def cmd_selinux_remove_fcontext(path_regex: str, file_type: str) -> str:
    path_regex = _validate_nonempty_line(path_regex, "Path spec")
    file_type = _validate_identifier(file_type, "SELinux type")
    q_path = shlex.quote(path_regex)
    return (
        _SEMANAGE_MISSING +
        f"semanage fcontext -d -t {file_type} {q_path} 2>&1 "
        f"&& printf 'File context rule removed for %s ({file_type}).\\n' {q_path}"
    )


def cmd_selinux_generate_policy_from_denials(module_name: str) -> str:
    """Feeds the recent AVC denials through audit2allow to synthesize
    a custom policy module, then loads it with semodule. Review the
    denials first (cmd_selinux_explain_denials) - this grants whatever
    those denials were asking for, so only run it once you've
    confirmed the access is legitimate."""
    module_name = _validate_identifier(module_name, "Module name")
    q_name = shlex.quote(module_name)
    return (
        _AUDITD_MISSING +
        "if ! command -v audit2allow >/dev/null 2>&1 || ! command -v semodule >/dev/null 2>&1; then "
        "echo 'audit2allow/semodule are not installed on this host (package: policycoreutils-python-utils).' >&2; exit 1; fi; "
        f"cd /tmp && ausearch -m avc,user_avc -ts recent -i 2>&1 | audit2allow -M {q_name} 2>&1 "
        f"&& semodule -i {q_name}.pp 2>&1 "
        f"&& echo 'Policy module {module_name} generated from recent denials and loaded.'"
    )


# ===========================================================
# Configure SSH
# ===========================================================
_SSHD_CONFIG = "/etc/ssh/sshd_config"


def _sshd_service_fragment(var: str = "SSHSVC") -> str:
    """Sets $<var> to whichever of sshd / ssh the host's init system
    actually knows about (RHEL-family vs Debian-family unit names)."""
    return (
        f"if systemctl list-unit-files 2>/dev/null | grep -q '^sshd\\.service'; then {var}=sshd; "
        f"else {var}=ssh; fi"
    )


def cmd_sshd_status() -> str:
    svc = _sshd_service_fragment()
    return (
        f"{svc}; echo \"-- systemctl status $SSHSVC --\" && systemctl status \"$SSHSVC\" --no-pager 2>&1; "
        "echo; echo '-- sshd -t (config syntax check) --' && sshd -t 2>&1 && echo 'sshd config OK.'"
    )


def cmd_sshd_get_effective_config(key: str = "") -> str:
    """Dumps sshd's effective (fully-resolved) configuration via
    `sshd -T`, optionally filtered to one directive."""
    key = (key or "").strip()
    if key:
        key = _validate_identifier(key, "Directive")
        return f"sshd -T 2>&1 | grep -i {shlex.quote(key)}"
    return "sshd -T 2>&1"


def _build_sshd_set_option_script(key: str, value: str) -> str:
    """Replaces an existing uncommented sshd_config line for `key` if
    present, or appends one if not. Backs up the file first, then
    validates the result with `sshd -t` and restores the backup if
    validation fails, so a typo can't lock out SSH access. Caller is
    responsible for reloading sshd (cmd_sshd_reload) afterward to
    apply it."""
    q_cfg = shlex.quote(_SSHD_CONFIG)
    q_bak = shlex.quote(_SSHD_CONFIG + ".bak")
    # `key` is a validated identifier (alnum/_-), safe to inline into the sed
    # pattern. `value` is free text - it can legitimately contain '/' (e.g. a
    # Banner path) or spaces, and must never be inlined into sed's replacement
    # (a '/' breaks the delimiter) or into the shell (a single quote would break
    # out of the surrounding quoting and run arbitrary code as root). Carry it
    # through a single-quoted shell variable instead, and never feed it to sed.
    #
    # sshd uses the FIRST occurrence of a keyword, so delete every existing
    # uncommented line for this key and append ours - that makes ours
    # authoritative regardless of where an old one sat.
    qk = shlex.quote(key)
    qv = shlex.quote(value)
    return (
        f"cp {q_cfg} {q_bak} 2>&1; "
        f"v={qv}; "
        f"sed -i -E '/^[[:space:]]*{key}[[:space:]]/d' {q_cfg}; "
        f"printf '%s %s\\n' {qk} \"$v\" >> {q_cfg}; "
        f"if sshd -t 2>&1; then printf 'sshd_config: %s set to %s. Reload sshd to apply.\\n' {qk} \"$v\"; "
        f"else echo 'New config failed validation - restoring previous sshd_config.' >&2; cp {q_bak} {q_cfg}; exit 1; fi"
    )


def cmd_sshd_set_option(key: str, value: str) -> str:
    """Sets one sshd_config directive (e.g. 'X11Forwarding' / 'no')."""
    key = _validate_identifier(key, "Directive")
    value = _validate_nonempty_line(value, "Value")
    return _build_sshd_set_option_script(key, value)


def cmd_sshd_reload() -> str:
    svc = _sshd_service_fragment()
    return (
        "if ! sshd -t 2>&1; then echo 'Current sshd_config does not pass validation - not reloading.' >&2; exit 1; fi; "
        f"{svc}; systemctl reload \"$SSHSVC\" 2>&1 && echo 'sshd reloaded.'"
    )


# ===========================================================
# Disable root login
# ===========================================================
def cmd_set_root_login(allow: bool) -> str:
    value = "yes" if allow else "no"
    verb = "enabled" if allow else "disabled"
    return _build_sshd_set_option_script("PermitRootLogin", value) + f"; echo 'Root SSH login {verb} (reload sshd to apply).'"


# ===========================================================
# Configure key-based authentication
# ===========================================================
def cmd_set_pubkey_auth(enabled: bool) -> str:
    value = "yes" if enabled else "no"
    return _build_sshd_set_option_script("PubkeyAuthentication", value)


def cmd_set_password_auth(enabled: bool) -> str:
    """Disabling this forces key-based authentication only. Make sure
    at least one working key is already installed before turning
    password auth off, or the account can be locked out."""
    value = "yes" if enabled else "no"
    return _build_sshd_set_option_script("PasswordAuthentication", value)


def cmd_list_authorized_keys(user: str) -> str:
    user = _validate_username(user)
    q_user = shlex.quote(user)
    return (
        f"home=$(getent passwd {q_user} | cut -d: -f6); "
        f'if [ -z "$home" ]; then echo \'No such user: {user}\' >&2; exit 1; fi; '
        f'if [ -r "$home/.ssh/authorized_keys" ]; then cat "$home/.ssh/authorized_keys"; '
        f"else echo 'No authorized_keys file for {user}.'; fi"
    )


def cmd_install_authorized_key(user: str, public_key: str) -> str:
    user = _validate_username(user)
    public_key = _validate_nonempty_line(public_key, "Public key")
    q_user = shlex.quote(user)
    q_key = shlex.quote(public_key)
    return (
        f"home=$(getent passwd {q_user} | cut -d: -f6); "
        f'if [ -z "$home" ]; then echo \'No such user: {user}\' >&2; exit 1; fi; '
        f'mkdir -p "$home/.ssh" && chmod 700 "$home/.ssh"; '
        f'touch "$home/.ssh/authorized_keys"; '
        f'grep -qxF {q_key} "$home/.ssh/authorized_keys" || echo {q_key} >> "$home/.ssh/authorized_keys"; '
        f'chmod 600 "$home/.ssh/authorized_keys" && chown -R {q_user} "$home/.ssh"; '
        f"echo 'Public key installed for {user}.'"
    )


# ===========================================================
# Rotate SSH keys
# ===========================================================
def cmd_remove_authorized_key(user: str, match_text: str) -> str:
    """Removes any authorized_keys line containing `match_text` (e.g.
    a key's comment/fingerprint) - the way to retire an old user key
    as part of a rotation."""
    user = _validate_username(user)
    match_text = _validate_nonempty_line(match_text, "Key match text")
    q_user = shlex.quote(user)
    q_match = shlex.quote(match_text)
    return (
        f"home=$(getent passwd {q_user} | cut -d: -f6); "
        f'if [ -z "$home" ]; then echo \'No such user: {user}\' >&2; exit 1; fi; '
        f'f="$home/.ssh/authorized_keys"; '
        f'if [ ! -f "$f" ]; then echo \'No authorized_keys file for {user}.\' >&2; exit 1; fi; '
        f'grep -vF {q_match} "$f" > "$f.tmp" && mv "$f.tmp" "$f" && chmod 600 "$f"; '
        f"echo 'Removed matching key(s) for {user}.'"
    )


def cmd_rotate_host_keys() -> str:
    """Regenerates this host's SSH host keys (the identity SSH
    presents to clients - distinct from any user's personal
    keypairs) and restarts sshd to pick them up. Irreversible and
    will trigger a "host key changed" warning on every client that
    has connected before - confirm with the admin first."""
    svc = _sshd_service_fragment()
    return (
        "mkdir -p /etc/ssh/old_host_keys_$(date +%Y%m%d%H%M%S) 2>&1 && "
        "back=$(ls -d /etc/ssh/old_host_keys_* 2>/dev/null | tail -1) && "
        "mv /etc/ssh/ssh_host_*key* \"$back/\" 2>/dev/null; "
        "ssh-keygen -A 2>&1 && "
        f"{svc}; systemctl restart \"$SSHSVC\" 2>&1 && "
        "echo 'SSH host keys regenerated and sshd restarted (old keys backed up under /etc/ssh/old_host_keys_*).'"
    )


# ===========================================================
# Audit logs
# ===========================================================
def cmd_auditd_status() -> str:
    return (
        _AUDITD_MISSING +
        "echo '-- systemctl status auditd --' && systemctl status auditd --no-pager 2>&1; "
        "echo; echo '-- auditctl -s --' && auditctl -s 2>&1"
    )


def cmd_tail_audit_log(lines: int = 200) -> str:
    lines = _validate_int_range(lines, 1, 10000, "Line count")
    return (
        "if [ ! -r /var/log/audit/audit.log ]; then "
        "echo '/var/log/audit/audit.log is missing or not readable (is auditd installed and running?).' >&2; exit 1; fi; "
        f"tail -n {lines} /var/log/audit/audit.log 2>&1"
    )


def cmd_search_audit_log(query: str, lines: int = 200) -> str:
    query = _validate_nonempty_line(query, "Search text")
    lines = _validate_int_range(lines, 1, 10000, "Line count")
    return (
        "if [ ! -r /var/log/audit/audit.log ]; then "
        "echo '/var/log/audit/audit.log is missing or not readable (is auditd installed and running?).' >&2; exit 1; fi; "
        f"grep -iF {shlex.quote(query)} /var/log/audit/audit.log 2>&1 | tail -n {lines}"
    )


# ===========================================================
# Review failed logins
# ===========================================================
def cmd_list_failed_logins(lines: int = 50) -> str:
    lines = _validate_int_range(lines, 1, 5000, "Line count")
    return (
        f"if command -v lastb >/dev/null 2>&1; then lastb -n {lines} 2>&1; "
        f"else journalctl -u sshd --no-pager 2>&1 | grep -i 'failed password' | tail -n {lines}; fi"
    )


def cmd_failed_login_summary(top_n: int = 20) -> str:
    """Counts failed-password attempts by source IP, highest first -
    quick view of who/what is hammering SSH."""
    top_n = _validate_int_range(top_n, 1, 500, "Result count")
    return (
        "src=/var/log/secure; [ -r \"$src\" ] || src=/var/log/auth.log; "
        'if [ -r "$src" ]; then '
        'grep -i "failed password" "$src" 2>&1; '
        "else journalctl -u sshd --no-pager 2>&1 | grep -i 'failed password'; fi "
        "| grep -oE 'from [0-9a-fA-F:.]+' | awk '{print $2}' | sort | uniq -c | sort -rn "
        f"| head -n {top_n}"
    )


def cmd_list_locked_accounts() -> str:
    """Read-only: lists local accounts currently locked (password
    field starts with '!' in /etc/shadow)."""
    return r"""awk -F: '($2 ~ /^!/ || $2 == "*") {print $1}' /etc/shadow 2>&1 || echo 'Could not read /etc/shadow (requires root).' >&2"""


# ===========================================================
# Install security updates
# ===========================================================
def cmd_check_security_updates() -> str:
    """Read-only: lists available security-relevant updates without
    installing anything."""
    return _pkgmgr_dispatch(
        rpm_cmd=(
            'if [ "$PKGMGR" = "dnf" ]; then dnf updateinfo list security 2>&1; '
            "else (yum --security check-update 2>&1 || true); fi"
        ),
        zypper_cmd="zypper list-patches --category security 2>&1",
        apt_cmd=(
            "apt-get update >/dev/null 2>&1; "
            "if command -v unattended-upgrade >/dev/null 2>&1; then "
            "unattended-upgrade --dry-run -d 2>&1; "
            "else echo 'apt-get upgradable packages (install unattended-upgrades for a security-only view):'; "
            "apt list --upgradable 2>/dev/null; fi"
        ),
    )


def cmd_install_security_updates() -> str:
    """Installs security-relevant updates only (not a full upgrade)
    using whichever mechanism the host's package manager provides for
    that distinction."""
    return _pkgmgr_dispatch(
        rpm_cmd=(
            'if [ "$PKGMGR" = "dnf" ]; then dnf upgrade --security -y 2>&1; '
            "else (yum --security update -y 2>&1 || echo 'yum-plugin-security may be required for security-only updates.' >&2); fi"
        ),
        zypper_cmd="zypper --non-interactive patch --category security 2>&1",
        apt_cmd=(
            "DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades 2>&1 && "
            "unattended-upgrade -d 2>&1"
        ),
    ) + " && echo 'Security updates installed (see output above for details).'"


# ===========================================================
# Configure password policies
# ===========================================================
def cmd_get_password_policy() -> str:
    return (
        "echo '-- /etc/security/pwquality.conf --' && "
        "(grep -vE '^[[:space:]]*#|^[[:space:]]*$' /etc/security/pwquality.conf 2>&1 || echo 'not present'); "
        "echo; echo '-- /etc/login.defs (aging) --' && grep -E '^PASS_(MAX|MIN|WARN)_' /etc/login.defs 2>&1"
    )


def cmd_set_pwquality_option(key: str, value) -> str:
    """Sets one /etc/security/pwquality.conf directive (e.g. 'minlen'
    -> 12, 'dcredit' -> -1), creating the file if it doesn't exist
    yet."""
    key = _validate_identifier(key, "Option")
    value = _validate_nonempty_line(str(value), "Value")
    q_file = shlex.quote("/etc/security/pwquality.conf")
    # `key` is a validated identifier (safe to inline into the sed pattern);
    # `value` is free text and must never be inlined into sed/printf (a '/'
    # breaks the delimiter, a single quote breaks out and runs as root). Carry
    # it via a single-quoted shell var, and delete-then-append so our line is
    # the only one for this key.
    qk = shlex.quote(key)
    qv = shlex.quote(value)
    return (
        f"touch {q_file}; v={qv}; "
        f"sed -i -E '/^[[:space:]]*{key}[[:space:]]*=/d' {q_file}; "
        f"printf '%s = %s\\n' {qk} \"$v\" >> {q_file}; "
        f"printf 'pwquality.conf: %s set to %s.\\n' {qk} \"$v\""
    )


def cmd_set_password_aging(max_days=None, min_days=None, warn_days=None) -> str:
    """Sets the default password-aging values new accounts inherit
    from /etc/login.defs (PASS_MAX_DAYS / PASS_MIN_DAYS /
    PASS_WARN_AGE). Leave an argument as None to leave that setting
    untouched. Does not retroactively change existing accounts - use
    `chage` per-account for that."""
    edits = []
    if max_days is not None:
        edits.append(("PASS_MAX_DAYS", _validate_int_range(max_days, 0, 99999, "Max days")))
    if min_days is not None:
        edits.append(("PASS_MIN_DAYS", _validate_int_range(min_days, 0, 99999, "Min days")))
    if warn_days is not None:
        edits.append(("PASS_WARN_AGE", _validate_int_range(warn_days, 0, 99999, "Warn days")))
    if not edits:
        raise ValueError("Specify at least one of max days, min days, or warn days.")
    q_file = shlex.quote("/etc/login.defs")
    parts = [f"cp {q_file} {q_file}.bak 2>&1"]
    summary = []
    for directive, n in edits:
        parts.append(
            f"if grep -qE '^[[:space:]]*{directive}[[:space:]]' {q_file}; then "
            f"sed -i -E 's/^[[:space:]]*{directive}[[:space:]]+.*/{directive}\\t{n}/' {q_file}; "
            f"else printf '{directive}\\t{n}\\n' >> {q_file}; fi"
        )
        summary.append(f"{directive}={n}")
    parts.append(f"echo 'login.defs updated: {', '.join(summary)}.'")
    return "; ".join(parts)


def cmd_set_account_lockout(attempts: int, unlock_seconds: int) -> str:
    """Best-effort pam_faillock configuration: `attempts` failed logins
    within the configured interval locks the account for
    `unlock_seconds` (0 = locked until an admin runs `faillock
    --reset`). Uses authselect on hosts that have it (RHEL 8+),
    otherwise edits /etc/security/faillock.conf directly, which is
    honored by pam_faillock wherever it's already wired into PAM."""
    attempts = _validate_int_range(attempts, 1, 100, "Failed-attempt threshold")
    unlock_seconds = _validate_int_range(unlock_seconds, 0, 86400 * 7, "Unlock time (seconds)")
    q_file = shlex.quote("/etc/security/faillock.conf")
    return (
        f"touch {q_file} && "
        f"if grep -qE '^[[:space:]]*deny[[:space:]]*=' {q_file}; then "
        f"sed -i -E 's/^[[:space:]]*deny[[:space:]]*=.*/deny = {attempts}/' {q_file}; "
        f"else printf 'deny = {attempts}\\n' >> {q_file}; fi; "
        f"if grep -qE '^[[:space:]]*unlock_time[[:space:]]*=' {q_file}; then "
        f"sed -i -E 's/^[[:space:]]*unlock_time[[:space:]]*=.*/unlock_time = {unlock_seconds}/' {q_file}; "
        f"else printf 'unlock_time = {unlock_seconds}\\n' >> {q_file}; fi; "
        "if command -v authselect >/dev/null 2>&1 && authselect current >/dev/null 2>&1; then "
        "authselect enable-feature with-faillock 2>&1 || true; fi; "
        f"echo 'faillock.conf: deny={attempts}, unlock_time={unlock_seconds}s. "
        "Confirm /etc/pam.d/system-auth (or common-auth) actually references pam_faillock on this host.'"
    )


# ===========================================================
# Harden systems
# ===========================================================
_HARDENING_SYSCTL_FILE = "/etc/sysctl.d/99-sysible-hardening.conf"
_HARDENING_SYSCTL_BODY = (
    "net.ipv4.conf.all.accept_redirects = 0\n"
    "net.ipv4.conf.all.send_redirects = 0\n"
    "net.ipv4.conf.all.accept_source_route = 0\n"
    "net.ipv4.conf.all.log_martians = 1\n"
    "net.ipv4.icmp_echo_ignore_broadcasts = 1\n"
    "net.ipv4.tcp_syncookies = 1\n"
    "kernel.randomize_va_space = 2\n"
    "fs.suid_dumpable = 0\n"
)


def cmd_get_hardening_overview() -> str:
    """Read-only snapshot of common hardening-relevant settings:
    SELinux mode, whether root SSH login is allowed, whether password
    auth is enabled, and currently-listening network services."""
    return (
        "echo '-- SELinux --' && (getenforce 2>&1 || echo 'not installed'); "
        "echo; echo '-- sshd: root login / password auth --' && "
        "(sshd -T 2>&1 | grep -iE '^(permitrootlogin|passwordauthentication)' || echo 'sshd -T unavailable'); "
        "echo; echo '-- Listening services --' && "
        "(ss -tulpn 2>&1 || netstat -tulpn 2>&1)"
    )


def cmd_apply_sysctl_hardening() -> str:
    """Writes a small set of conservative network/kernel hardening
    sysctl values to a dedicated drop-in file and applies them
    immediately, without touching any other sysctl settings already
    configured on the host."""
    q_file = shlex.quote(_HARDENING_SYSCTL_FILE)
    body = _HARDENING_SYSCTL_BODY.replace("\n", "\\n")
    return (
        f"printf '{body}' > {q_file} && "
        f"sysctl --system 2>&1 | tail -n 20 "
        f"&& echo 'Hardening sysctl values applied ({_HARDENING_SYSCTL_FILE}).'"
    )


def cmd_disable_core_dumps() -> str:
    """Belt-and-suspenders core dump lockdown: sets fs.suid_dumpable=0
    at runtime/on boot and adds a hard limit of 0 in
    /etc/security/limits.conf so per-process ulimit settings can't
    re-enable them."""
    q_file = shlex.quote("/etc/security/limits.conf")
    return (
        "sysctl -w fs.suid_dumpable=0 2>&1 && "
        f"grep -qxF '* hard core 0' {q_file} || printf '* hard core 0\\n' >> {q_file}; "
        "echo 'Core dumps disabled (fs.suid_dumpable=0, limits.conf hard core 0).'"
    )


def cmd_list_world_writable_files(path: str = "/etc") -> str:
    """Read-only audit: world-writable regular files under `path`,
    excluding mounted filesystems other than `path`'s own (-xdev) so
    it doesn't wander into /proc, /sys, or other mounts."""
    path = _validate_path(path)
    return f"find {shlex.quote(path)} -xdev -type f -perm -0002 2>/dev/null"


def cmd_list_suid_binaries(path: str = "/") -> str:
    """Read-only audit: setuid binaries under `path` (-xdev, same
    rationale as above)."""
    path = _validate_path(path)
    return f"find {shlex.quote(path)} -xdev -type f -perm -4000 2>/dev/null"


# ===========================================================
# Run vulnerability scans
# ===========================================================
_LYNIS_MISSING_MSG = "Lynis is not installed on this host (run Install Lynis first, or install the 'lynis' package)."


def cmd_lynis_status() -> str:
    return (
        "if command -v lynis >/dev/null 2>&1; then lynis show version 2>&1; "
        f"else echo {shlex.quote(_LYNIS_MISSING_MSG)} >&2; exit 1; fi"
    )


def cmd_install_lynis() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" install -y lynis',
        zypper_cmd="zypper --non-interactive install lynis",
        apt_cmd="apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y lynis",
    ) + " && echo 'Lynis installed.'"


def cmd_install_rkhunter() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" install -y rkhunter',
        zypper_cmd="zypper --non-interactive install rkhunter",
        apt_cmd="apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y rkhunter",
    ) + " && echo 'rkhunter installed.'"


def cmd_run_lynis_scan() -> str:
    """Runs a quick (non-interactive) Lynis system audit and prints the
    full report, including its hardening-index score and suggestions."""
    return (
        "if ! command -v lynis >/dev/null 2>&1; then "
        f"echo {shlex.quote(_LYNIS_MISSING_MSG)} >&2; exit 1; fi; "
        "lynis audit system --quick --no-colors 2>&1"
    )


def cmd_run_rkhunter_scan() -> str:
    """Runs rkhunter's rootkit/anomaly check as a second, differently-
    focused scanner alongside Lynis. --sk skips the "press enter to
    continue" prompts so it can run unattended."""
    return (
        "if ! command -v rkhunter >/dev/null 2>&1; then "
        "echo 'rkhunter is not installed on this host (package: rkhunter).' >&2; exit 1; fi; "
        "rkhunter --check --sk --no-colors 2>&1"
    )
