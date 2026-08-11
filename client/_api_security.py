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


from client._pkgmgr import (
    pkgmgr_detect_fragment as _pkgmgr_detect_fragment,
    pkgmgr_dispatch as _pkgmgr_dispatch,
)
from client._validators import validate_int_range as _validate_int_range
from client._validators import validate_nonempty_line as _validate_nonempty_line


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
    # Reject NUL and CR/LF: a newline in a path could append an extra line to a
    # file this value is written into (e.g. /etc/fstab). Matches the mount copy.
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} contains an invalid character.")
    return value



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
        # The Python management package (semanage/audit2allow/audit2why) is
        # policycoreutils-python-utils on dnf (RHEL8+/Fedora/Rocky/Alma) but
        # policycoreutils-python on yum (EL7/CentOS7/Oracle 7/Amazon Linux 2).
        rpm_cmd=(
            'if [ "$PKGMGR" = "dnf" ]; then '
            '"$PKGMGR" install -y policycoreutils policycoreutils-python-utils setools-console libselinux-utils; '
            'else yum install -y policycoreutils policycoreutils-python setools-console libselinux-utils; fi'
        ),
        # openSUSE/SLES: install each package separately so one wrong/renamed name on
        # a given SUSE release doesn't abort the whole zypper transaction (exit 104)
        # and leave NONE of the tools installed. Mirrors cmd_install_firewalld's loop.
        zypper_cmd=(
            "for _p in policycoreutils policycoreutils-python-utils setools-console libselinux-tools; do "
            "  rpm -q \"$_p\" >/dev/null 2>&1 || zypper --non-interactive install \"$_p\" 2>&1 || "
            "    echo \"Warning: could not install $_p on this SUSE release.\" >&2; "
            "done"
        ),
        apt_cmd="apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y policycoreutils policycoreutils-python-utils selinux-utils setools",
        # Arch does NOT ship the SELinux userspace in its official repos - the
        # whole SELinux stack lives in the AUR 'selinux' group, so there's nothing
        # pacman can install here. Fail with a clear pointer instead of a wrong
        # command (Arch uses AppArmor/none by default anyway).
        pacman_cmd=(
            "echo 'SELinux is not available in the Arch official repositories "
            "(see the AUR selinux group); this action cannot install it via pacman.' >&2; exit 1"
        ),
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
        # -a fails with "already defined" if the rule exists, so fall back to -m
        # (modify) for an idempotent upsert. Gate restorecon + the success line on
        # && so success (and exit 0) is reported only when the rule actually took.
        f"{{ semanage fcontext -a -t {file_type} {q_path} 2>/dev/null "
        f"|| semanage fcontext -m -t {file_type} {q_path} 2>&1; }} "
        f"&& restorecon -Rv {q_path} 2>&1 "
        f"&& printf 'File context rule added for %s -> {file_type}.\\n' {q_path}"
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


def _sshd_bin_fragment(var: str = "SSHDBIN") -> str:
    """Sets $<var> to the sshd binary's path. sshd ships in /usr/sbin (or /sbin),
    which is NOT on a non-root user's PATH on openSUSE (and some others), so a
    bare `sshd -t` / `sshd -T` fails with 'command not found' (exit 127). Resolve
    it explicitly, falling back to a bare `sshd` if nothing is found so the error
    message stays meaningful."""
    return (
        f"{var}=$(command -v sshd 2>/dev/null); "
        f"if [ -z \"${var}\" ]; then for _p in /usr/sbin/sshd /sbin/sshd /usr/bin/sshd; do "
        f"[ -x \"$_p\" ] && {var}=\"$_p\" && break; done; fi; "
        f"[ -z \"${var}\" ] && {var}=sshd"
    )


def _sshd_privsep_dir_fragment() -> str:
    """Ensure the sshd privilege-separation directory exists before `sshd -t`.

    On Debian/Ubuntu (and others) `sshd -t` FAILS with 'Missing privilege
    separation directory: /run/sshd' when that directory is absent — a runtime
    dir the systemd unit's RuntimeDirectory normally creates, but which can be
    missing at config-test time (a cleared /run, sshd started outside systemd,
    a fresh boot state). That made a perfectly VALID hardening config get
    rejected and rolled back.

    Creating it — root-owned, 0755, exactly what sshd itself uses — lets a good
    config validate. It does NOT bypass validation: `sshd -t` still runs and a
    genuinely bad config still fails and is restored. Best-effort: any error is
    ignored and `sshd -t` then reports the real problem. Harmless on distros that
    don't use /run/sshd (an empty tmpfs dir cleared at reboot)."""
    return ("[ -d /run/sshd ] || mkdir -p /run/sshd 2>/dev/null || true; "
            "chmod 0755 /run/sshd 2>/dev/null || true; ")


def cmd_sshd_status() -> str:
    svc = _sshd_service_fragment()
    binf = _sshd_bin_fragment()
    psd = _sshd_privsep_dir_fragment()
    return (
        f"{svc}; {binf}; {psd}echo \"-- systemctl status $SSHSVC --\" && systemctl status \"$SSHSVC\" --no-pager 2>&1; "
        "echo; echo '-- sshd -t (config syntax check) --' && \"$SSHDBIN\" -t 2>&1 && echo 'sshd config OK.'"
    )


def cmd_sshd_get_effective_config(key: str = "") -> str:
    """Dumps sshd's effective (fully-resolved) configuration via
    `sshd -T`, optionally filtered to one directive."""
    key = (key or "").strip()
    binf = _sshd_bin_fragment()
    if key:
        key = _validate_identifier(key, "Directive")
        return f"{binf}; \"$SSHDBIN\" -T 2>&1 | grep -i {shlex.quote(key)}"
    return f"{binf}; \"$SSHDBIN\" -T 2>&1"


def _build_sshd_set_option_script(key: str, value: str, reload: bool = False) -> str:
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
    q_dropdir = shlex.quote(_SSHD_CONFIG + ".d")   # /etc/ssh/sshd_config.d
    # sshd keywords are case-insensitive, so the strip must be too (the GNU sed
    # `I` flag) - a drop-in may write `passwordauthentication` in any case.
    strip = f"sed -i -E '/^[[:space:]]*{key}[[:space:]]/Id'"
    binf = _sshd_bin_fragment()
    psd = _sshd_privsep_dir_fragment()
    if reload:
        # Apply-and-reload: after the new config validates, reload sshd so the
        # change takes effect in one click (a reload keeps existing sessions).
        svc = _sshd_service_fragment()
        on_ok = (
            f"{svc}; "
            f"if systemctl reload \"$SSHSVC\" 2>&1; then "
            f"printf 'sshd_config: %s set to %s, and sshd reloaded.\\n' {qk} \"$v\"; "
            f"else printf 'sshd_config: %s set to %s, but the sshd reload FAILED - use the Reload sshd button to apply.\\n' {qk} \"$v\" >&2; fi"
        )
    else:
        on_ok = f"printf 'sshd_config: %s set to %s. Reload sshd to apply.\\n' {qk} \"$v\""
    # Modern Debian/Ubuntu (and all cloud images) put `Include
    # /etc/ssh/sshd_config.d/*.conf` at the TOP of sshd_config, and sshd honours
    # the FIRST occurrence of a keyword. A cloud image ships
    # 50-cloud-init.conf with `PasswordAuthentication yes`, so a line we append
    # to the bottom of the main file is silently overridden - the operator
    # disables password/root login, `sshd -t` passes, the reload succeeds, and
    # the control never actually takes effect. So strip the key from every
    # drop-in as well, making our appended line the sole authoritative
    # occurrence. Back up the drop-in dir too and roll it back if validation
    # fails, exactly like the main file.
    return (
        f"{binf}; {psd}"
        f"cp {q_cfg} {q_bak} 2>&1; "
        f"dropd={q_dropdir}; bakd=$(mktemp -d 2>/dev/null || echo \"/tmp/sysible-sshd-bak.$$\"); "
        f"mkdir -p \"$bakd\"; "
        f"if [ -d \"$dropd\" ]; then cp -a \"$dropd\"/. \"$bakd\"/ 2>/dev/null; fi; "
        f"v={qv}; "
        f"{strip} {q_cfg}; "
        f"if [ -d \"$dropd\" ]; then for f in \"$dropd\"/*.conf; do [ -e \"$f\" ] && {strip} \"$f\"; done; fi; "
        f"printf '%s %s\\n' {qk} \"$v\" >> {q_cfg}; "
        f"if \"$SSHDBIN\" -t 2>&1; then rm -rf \"$bakd\" 2>/dev/null; {on_ok}; "
        f"else echo 'New config failed validation - restoring previous sshd_config.' >&2; "
        f"cp {q_bak} {q_cfg}; "
        f"if [ -d \"$dropd\" ]; then rm -f \"$dropd\"/*.conf 2>/dev/null; cp -a \"$bakd\"/. \"$dropd\"/ 2>/dev/null; fi; "
        f"rm -rf \"$bakd\" 2>/dev/null; exit 1; fi"
    )


def cmd_sshd_set_option(key: str, value: str) -> str:
    """Sets one sshd_config directive (e.g. 'X11Forwarding' / 'no')."""
    key = _validate_identifier(key, "Directive")
    value = _validate_nonempty_line(value, "Value")
    return _build_sshd_set_option_script(key, value)


def cmd_sshd_reload() -> str:
    svc = _sshd_service_fragment()
    binf = _sshd_bin_fragment()
    psd = _sshd_privsep_dir_fragment()
    return (
        f"{binf}; {psd}if ! \"$SSHDBIN\" -t 2>&1; then echo 'Current sshd_config does not pass validation - not reloading.' >&2; exit 1; fi; "
        f"{svc}; systemctl reload \"$SSHSVC\" 2>&1 && echo 'sshd reloaded.'"
    )


# ===========================================================
# Disable root login
# ===========================================================
_ROOT_LOGIN_MODES = {"no", "yes", "prohibit-password"}


def cmd_set_root_login_mode(mode: str) -> str:
    """Set sshd PermitRootLogin to an explicit mode and reload. `mode` is one of
    'no' (deny root SSH), 'prohibit-password' (root by key only), or 'yes' (root
    with a password). Whitelisted so only valid sshd values reach the config. The
    console offers these as three explicit buttons instead of a checkbox, so an
    operator clicks the exact intent rather than toggling a box and guessing."""
    m = (mode or "").strip().lower()
    if m not in _ROOT_LOGIN_MODES:
        raise ValueError("root login mode must be one of: no, prohibit-password, yes")
    return _build_sshd_set_option_script("PermitRootLogin", m, reload=True)


def cmd_set_root_login(allow: bool) -> str:
    # Back-compat boolean wrapper (True=yes, False=no); the console now uses the
    # explicit-mode buttons via cmd_set_root_login_mode.
    return cmd_set_root_login_mode("yes" if allow else "no")


# ===========================================================
# Configure key-based authentication
# ===========================================================
def cmd_set_pubkey_auth(enabled: bool) -> str:
    value = "yes" if enabled else "no"
    return _build_sshd_set_option_script("PubkeyAuthentication", value, reload=True)


def cmd_set_password_auth(enabled: bool) -> str:
    """Disabling this forces key-based authentication only. Make sure
    at least one working key is already installed before turning
    password auth off, or the account can be locked out."""
    value = "yes" if enabled else "no"
    return _build_sshd_set_option_script("PasswordAuthentication", value, reload=True)


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
    # The SSH unit is `sshd.service` on RHEL/SUSE but `ssh.service` on
    # Debian/Ubuntu; hardcoding `sshd` returned nothing from the journal there.
    svc = _sshd_service_fragment()
    return (
        f"{svc}; if command -v lastb >/dev/null 2>&1; then lastb -n {lines} 2>&1; "
        f"else journalctl -u \"$SSHSVC\" --no-pager 2>&1 | grep -i 'failed password' | tail -n {lines}; fi"
    )


def cmd_failed_login_summary(top_n: int = 20) -> str:
    """Counts failed-password attempts by source IP, highest first -
    quick view of who/what is hammering SSH."""
    top_n = _validate_int_range(top_n, 1, 500, "Result count")
    svc = _sshd_service_fragment()   # ssh.service (Debian) vs sshd.service (RHEL/SUSE)
    return (
        f"{svc}; src=/var/log/secure; [ -r \"$src\" ] || src=/var/log/auth.log; "
        'if [ -r "$src" ]; then '
        'grep -i "failed password" "$src" 2>&1; '
        "else journalctl -u \"$SSHSVC\" --no-pager 2>&1 | grep -i 'failed password'; fi "
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
            # dnf5 (Fedora 41+, RHEL 10) renamed the `updateinfo` command to
            # `advisory` and DROPPED the `list security` positional aliases, so
            # `dnf updateinfo list security` errors there. dnf5 has an `advisory`
            # subcommand (dnf4 does not), so probe for it and use `advisory list
            # --security`; fall back to the dnf4 form otherwise.
            'if [ "$PKGMGR" = "dnf" ]; then '
            'if dnf advisory --help >/dev/null 2>&1; then dnf advisory list --security 2>&1; '
            'else dnf updateinfo list security 2>&1; fi; '
            # yum --security relies on the repos publishing updateinfo (security-
            # advisory) metadata. RHEL and Amazon Linux 2 publish it; classic CentOS 7
            # repos do NOT, so --security matches nothing and this can read as "no
            # security updates" even when updates exist. Flag that possibility.
            'else out=$(yum --security check-update 2>&1); rc=$?; echo "$out"; '
            'if ! echo "$out" | grep -q updateinfo && [ "$rc" -ne 100 ]; then '
            "echo 'Note: if this host uses repos without updateinfo metadata "
            "(e.g. classic CentOS 7), yum cannot classify security-only updates "
            "- run a full update check to be sure.' >&2; fi; true; fi"
        ),
        zypper_cmd="zypper list-patches --category security 2>&1",
        apt_cmd=(
            "apt-get update >/dev/null 2>&1; "
            "if command -v unattended-upgrade >/dev/null 2>&1; then "
            "unattended-upgrade --dry-run -d 2>&1; "
            "else echo 'apt-get upgradable packages (install unattended-upgrades for a security-only view):'; "
            "apt list --upgradable 2>/dev/null; fi"
        ),
        # Arch is a rolling release with no security-only update channel, so the
        # closest read-only equivalent is "what would a full upgrade pull in". Use
        # checkupdates (pacman-contrib) when present - it queries a temporary db and
        # so needs no root and never touches the live sync db; otherwise fall back
        # to refreshing the db and listing upgradable packages with pacman -Qu
        # (which exits 1 when nothing is upgradable, hence the explicit message).
        pacman_cmd=(
            "echo 'Arch Linux is a rolling release and does not classify updates as "
            "security-only; showing all available updates.'; "
            "if command -v checkupdates >/dev/null 2>&1; then checkupdates 2>&1 || echo 'No updates available.'; "
            # Do NOT run `pacman -Sy` here: this is a READ-ONLY check, and a bare -Sy
            # mutates the live sync db (arming a partial-upgrade hazard). Without
            # checkupdates, compare against the current local db and point the operator
            # at pacman-contrib for an accurate, non-mutating check.
            "else echo 'Install pacman-contrib (provides checkupdates) for an accurate, "
            "non-mutating check; listing upgrades against the current local sync db:' >&2; "
            "pacman -Qu 2>&1 || echo 'No updates available (local db may be stale).'; fi"
        ),
    )


def cmd_install_security_updates() -> str:
    """Installs security-relevant updates only (not a full upgrade)
    using whichever mechanism the host's package manager provides for
    that distinction."""
    return _pkgmgr_dispatch(
        rpm_cmd=(
            'if [ "$PKGMGR" = "dnf" ]; then dnf upgrade --security -y 2>&1; '
            "else (yum --security update -y 2>&1; rc=$?; [ \"$rc\" -ne 0 ] && echo 'yum-plugin-security may be required for security-only updates.' >&2; exit \"$rc\"); fi"
        ),
        # --auto-agree-with-licenses: without it, zypper in non-interactive mode
        # auto-DECLINES (and silently skips) any security patch that needs a
        # license acceptance, so "install security updates" would report success
        # while leaving those patches uninstalled.
        zypper_cmd="zypper --non-interactive patch --auto-agree-with-licenses --category security 2>&1",
        apt_cmd=(
            "DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades 2>&1 && "
            "unattended-upgrade -d 2>&1"
        ),
        # Arch has no security-only update mechanism, and partial upgrades
        # (pacman -Sy <pkg>) are explicitly unsupported and can break the system.
        # The only correct way to pick up security fixes is a FULL system upgrade,
        # so that's what we emit (with a note explaining why it isn't security-only).
        pacman_cmd=(
            "echo 'Arch has no security-only update channel; applying a full system "
            "upgrade instead (partial upgrades are unsupported on Arch).'; "
            "pacman -Syu --noconfirm 2>&1"
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
        f"printf 'pwquality.conf: %s set to %s.\\n' {qk} \"$v\"; "
        # pwquality.conf is read by pam_pwquality, which RHEL/Fedora/SUSE wire into
        # the password stack by default. Debian/Ubuntu do NOT install
        # libpam-pwquality or reference it in common-password by default, so the
        # directive is inert there until that package is installed and wired - warn
        # rather than imply the policy is in force.
        'if command -v apt-get >/dev/null 2>&1 && '
        '! grep -rq pam_pwquality /etc/pam.d/ 2>/dev/null; then '
        "echo 'Note: pam_pwquality is not referenced in this host'\"'\"'s PAM stack "
        "(Debian/Ubuntu default). Install libpam-pwquality and enable it in "
        "/etc/pam.d/common-password for this to take effect.' >&2; fi"
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
    # Resolve the sshd binary path (it's not on a non-root PATH on openSUSE), same as
    # every other sshd command in this module — otherwise the section falsely reports
    # "sshd -T unavailable" on a healthy SUSE host.
    binf = _sshd_bin_fragment()
    return (
        "echo '-- SELinux --' && (getenforce 2>&1 || echo 'not installed'); "
        "echo; echo '-- sshd: root login / password auth --' && "
        f"({binf}; \"$SSHDBIN\" -T 2>/dev/null | grep -iE '^(permitrootlogin|passwordauthentication)' "
        "|| echo 'sshd -T unavailable'); "
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
    q_limits = shlex.quote("/etc/security/limits.conf")
    q_sysctl = shlex.quote("/etc/sysctl.d/99-sysible-coredumps.conf")
    return (
        # Persist across reboot via a drop-in (systemd-sysctl re-applies it at boot)
        # and apply it now — the previous `sysctl -w` was runtime-only despite the
        # "on boot" claim.
        f"printf 'fs.suid_dumpable = 0\\n' > {q_sysctl} && "
        f"sysctl --system 2>&1 | tail -n 5; "
        # Idempotent ulimit backstop; the guard runs regardless of the sysctl outcome.
        f"grep -qxF '* hard core 0' {q_limits} || printf '* hard core 0\\n' >> {q_limits}; "
        "echo 'Core dumps disabled (fs.suid_dumpable=0 persisted, limits.conf hard core 0).'"
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
        # lynis is in Arch's 'extra' repo under the same package name.
        pacman_cmd="pacman -Sy --needed --noconfirm lynis",
    ) + " && echo 'Lynis installed.'"


def cmd_install_rkhunter() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" install -y rkhunter',
        zypper_cmd="zypper --non-interactive install rkhunter",
        apt_cmd="apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y rkhunter",
        # rkhunter is in Arch's 'extra' repo under the same package name.
        pacman_cmd="pacman -Sy --needed --noconfirm rkhunter",
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


def cmd_allow_user_sudo_password(username: str) -> str:
    """openSUSE/SLES ship `Defaults targetpw`, so sudo demands the ROOT password
    instead of the invoking user's - which is why a correct user sudo password is
    rejected there with 'Sorry, try again.'. This installs a MINIMAL, validated
    /etc/sudoers.d override for JUST this user: `Defaults:<user> !targetpw`.

    It grants NO new privilege and adds NO NOPASSWD - the host's sudo policy is
    otherwise unchanged; sudo simply checks THIS user's own password, exactly like
    every other distro. On non-SUSE hosts (which don't set targetpw) it's a
    harmless no-op. The rule is written to a temp file, validated with `visudo
    -cf`, and only then installed at 0440, so a malformed rule can never break
    sudo. First run needs root once: store the host's root password, run this,
    then switch back to your user password."""
    user = _validate_username(username, "Sudo user")
    u = shlex.quote(user)
    dst = shlex.quote(f"/etc/sudoers.d/sysible-{user}-targetpw")
    return (
        f"u={u}; tmp=$(mktemp) || exit 1; "
        "printf 'Defaults:%s !targetpw\\n' \"$u\" > \"$tmp\"; "
        f"if visudo -cf \"$tmp\" >/dev/null 2>&1; then "
        f"install -m 0440 -o root -g root \"$tmp\" {dst} && rm -f \"$tmp\" && "
        "printf 'Done - sudo now checks the user %s'\\''s own password on this host "
        "(targetpw disabled for this user only; no other policy change).\\n' \"$u\"; "
        "else rm -f \"$tmp\"; echo 'The generated sudoers rule failed visudo validation; "
        "nothing was changed.' >&2; exit 1; fi"
    )
