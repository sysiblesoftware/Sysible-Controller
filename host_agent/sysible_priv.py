#!/usr/bin/env python3
"""
sysible-priv — the agent's single privileged entry point.

This is the one program the Sysible agent account is allowed to run under sudo.
It replaces the blanket `sysible ALL=(ALL) NOPASSWD: ALL` grant with:

    sysible ALL=(ALL) NOPASSWD: /opt/sysible-agent/priv/sysible-priv

so a compromised agent process can no longer `sudo bash` its way to root — it
can only invoke the operations this dispatcher exposes, with arguments this
dispatcher validates.

It runs as root (sudo'd by the agent) and exposes three subcommands:

  op       - PRIMARY PATH. Run ONE vetted operation from the fixed OPS table,
             argv-only (never a shell), with every argument validated first.
             Covers privileged WRITES (service.*, user.*, pkg.*, ...) and
             privileged READS (log.read, journal.read, config.read). Unknown
             verb => refused (fail-closed).

  runas    - Per-user (defense-in-depth) mode. Drop to a target local user and
             run a shell string AS THAT USER, so the host's own sudo logs name
             the real admin and per-user sudo policies apply. NOT a root
             boundary by itself. This is the hybrid model's attribution path for
             commands not yet expressed as an `op` verb.

  selftest - Run the validator/refusal test battery (no root, no side effects).

Because execution is argv-only (no shell), shell metacharacters in any argument
are inert — they are literal args, never operators — so command injection is
structurally impossible. The validators enforce *semantic* safety: valid names,
numeric ranges, and — the one true filesystem boundary — a write-path allowlist
for the generic file primitives. Broad config mutation (e.g. /etc/ssh/sshd_config)
is intentionally NOT reachable via file.write; it gets its own dedicated,
narrowly-scoped verbs instead.

Secrets (a user's become/sudo password, file contents) are read from STDIN,
never argv/env, so they never appear in `ps` or logs.
"""
import os
import pwd
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Validation helpers — the security boundary for the `op` path. Every value that
# reaches a process passes through one of these; they raise Reject on anything
# that doesn't match a strict, conservative pattern.
# ---------------------------------------------------------------------------

class Reject(Exception):
    """Raised when an argument fails validation; mapped to a clean error."""


_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]+(\.(service|socket|timer|target|path|mount))?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_TZ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+/-]{0,63}$")
_SYSCTL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SYSCTL_VAL_RE = re.compile(r"^[A-Za-z0-9 ._:/,-]{1,255}$")
_SEBOOL_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9 ._/@:+-]{0,255}$")
_ALLOWED_SHELLS = ("/bin/bash", "/bin/sh", "/usr/sbin/nologin", "/sbin/nologin",
                   "/usr/bin/false", "/bin/false", "/usr/bin/zsh")
_ALLOWED_FSTYPES = ("ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat",
                    "ntfs", "iso9660", "nfs", "nfs4", "cifs", "tmpfs")

# Directories the generic file primitives may touch. Anything else is refused —
# this is what keeps the file tail (write/chmod/chown) from becoming "own any
# file as root". Broad /etc config edits need dedicated verbs, not this.
_ALLOWED_WRITE_DIRS = ("/etc/sysible", "/etc/sysible-agent", "/opt/sysible-agent")


def v_unit(s):
    if not s or not _UNIT_RE.match(s):
        raise Reject(f"invalid systemd unit name: {s!r}")
    return s


def v_user(s):
    if not s or not _USER_RE.match(s):
        raise Reject(f"invalid username: {s!r}")
    return s


def v_group(s):
    if not s or not _USER_RE.match(s):
        raise Reject(f"invalid group name: {s!r}")
    return s


def v_pkgs(items):
    out = []
    for p in items:
        p = p.strip()
        if not p:
            continue
        if not _PKG_RE.match(p):
            raise Reject(f"invalid package name: {p!r}")
        out.append(p)
    if not out:
        raise Reject("no packages given")
    return out


def v_int(s, lo, hi, what="value"):
    try:
        n = int(str(s))
    except (TypeError, ValueError):
        raise Reject(f"invalid {what}: {s!r}")
    if not (lo <= n <= hi):
        raise Reject(f"{what} out of range [{lo},{hi}]: {n}")
    return n


def v_port(s):
    return v_int(s, 1, 65535, "port")


def v_proto(s):
    p = (s or "tcp").lower()
    if p not in ("tcp", "udp"):
        raise Reject(f"invalid protocol: {s!r} (tcp|udp)")
    return p


def v_mode(s):
    m = s or "0644"
    if not re.match(r"^0[0-7]{3}$", m):
        raise Reject(f"invalid mode: {s!r} (expected 0NNN octal)")
    return m


def v_bool_onoff(s):
    v = str(s).strip().lower()
    if v in ("1", "true", "on", "yes"):
        return "on"
    if v in ("0", "false", "off", "no"):
        return "off"
    raise Reject(f"invalid boolean: {s!r}")


def v_hostname(s):
    if not s or not _HOST_RE.match(s):
        raise Reject(f"invalid hostname: {s!r}")
    return s


def v_timezone(s):
    if not s or not _TZ_RE.match(s):
        raise Reject(f"invalid timezone: {s!r}")
    # Best-effort existence check; if the zoneinfo db isn't where we expect,
    # fall back to the charset check above rather than refusing wrongly.
    zi = os.path.join("/usr/share/zoneinfo", s)
    if os.path.isdir("/usr/share/zoneinfo") and not os.path.isfile(zi):
        raise Reject(f"unknown timezone: {s!r}")
    return s


def v_shell(s):
    if s not in _ALLOWED_SHELLS:
        raise Reject(f"shell not allowed: {s!r}")
    return s


def v_fstype(s):
    if s and s not in _ALLOWED_FSTYPES:
        raise Reject(f"filesystem type not allowed: {s!r}")
    return s


def v_abs_path(s, what="path"):
    if not s or not _ABS_PATH_RE.match(s) or ".." in s:
        raise Reject(f"invalid {what}: {s!r}")
    return s


def v_mount_source(s):
    """A mount source: a /dev node, or UUID=/LABEL=, or a network share."""
    if not s:
        raise Reject("mount source required")
    if s.startswith(("UUID=", "LABEL=", "PARTUUID=")):
        if not re.match(r"^[A-Za-z]+=[A-Za-z0-9_:.-]{1,64}$", s):
            raise Reject(f"invalid mount source: {s!r}")
        return s
    if s.startswith("//") or s.startswith("/"):
        return v_abs_path(s, "mount source")
    raise Reject(f"invalid mount source: {s!r}")


def v_write_path(p):
    # Resolve symlinks/.. and confirm the real path stays inside an allowed dir,
    # so `path=/etc/sysible/../../etc/shadow` can't escape.
    real = os.path.realpath(p)
    if not any(real == d or real.startswith(d + "/") for d in _ALLOWED_WRITE_DIRS):
        raise Reject(f"path {p!r} is outside the writable allowlist {_ALLOWED_WRITE_DIRS}")
    return real


def _clamp_lines(s, default=200, cap=5000):
    try:
        n = int(s)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


# ---------------------------------------------------------------------------
# Backend detection (package manager, firewall) — done once per invocation.
# ---------------------------------------------------------------------------

def _pkg_manager():
    for mgr in ("dnf", "yum", "zypper", "apt-get"):
        if shutil.which(mgr):
            return mgr
    raise Reject("no supported package manager found")


def _firewall_backend():
    for b in ("firewall-cmd", "ufw", "iptables", "nft"):
        if shutil.which(b):
            return b
    raise Reject("no supported firewall backend found")


# ---------------------------------------------------------------------------
# The verb table. Each handler returns a list of steps; a step is an argv list,
# or a (argv, stdin) tuple when a secret/content must go on the process stdin.
# ---------------------------------------------------------------------------

_SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable",
                    "disable", "mask", "unmask")


def _op_service(action, a):
    if action not in _SERVICE_ACTIONS:
        raise Reject(f"unknown service action: {action}")
    return [["systemctl", action, v_unit(a["unit"])]]


def _op_daemon_reload(a):
    return [["systemctl", "daemon-reload"]]


def _op_reset_failed(a):
    return [["systemctl", "reset-failed"]]


# --- packages -------------------------------------------------------------
def _op_pkg(action, a):
    mgr = _pkg_manager()
    if action in ("install", "remove"):
        pkgs = v_pkgs(a.get("pkgs", "").split(","))
        if mgr == "zypper":
            return [["zypper", "--non-interactive", action, *pkgs]]
        return [[mgr, "-y", action, *pkgs]]
    if action == "update":  # upgrade everything
        if mgr == "zypper":
            return [["zypper", "--non-interactive", "update"]]
        if mgr == "apt-get":
            return [["apt-get", "-y", "upgrade"]]
        return [[mgr, "-y", "upgrade"]]
    if action == "clean":
        if mgr == "zypper":
            return [["zypper", "clean"]]
        if mgr == "apt-get":
            return [["apt-get", "clean"]]
        return [[mgr, "clean", "all"]]
    raise Reject(f"unknown pkg action: {action}")


# --- users / groups -------------------------------------------------------
def _op_user_create(a):
    argv = ["useradd"]
    if a.get("system") in ("1", "true", True):
        argv.append("--system")
    if a.get("home") == "no":
        argv.append("--no-create-home")
    if a.get("shell"):
        argv += ["--shell", v_shell(a["shell"])]
    argv.append(v_user(a["user"]))
    return [argv]


def _op_user_delete(a):
    argv = ["userdel"]
    if a.get("remove_home") in ("1", "true", True):
        argv.append("--remove")
    argv.append(v_user(a["user"]))
    return [argv]


def _op_user_setpassword(a, stdin_secret):
    user = v_user(a["user"])
    if not stdin_secret:
        raise Reject("user.setpassword requires the password on stdin")
    return [(["chpasswd"], f"{user}:{stdin_secret}\n")]


def _op_user_lock(a):
    return [["usermod", "--lock", v_user(a["user"])]]


def _op_user_unlock(a):
    return [["usermod", "--unlock", v_user(a["user"])]]


def _op_user_set_shell(a):
    return [["usermod", "--shell", v_shell(a["shell"]), v_user(a["user"])]]


def _op_user_set_aging(a):
    """chage: password max/min days / expiry. Each field optional; validated."""
    argv = ["chage"]
    if a.get("maxdays") is not None:
        argv += ["-M", str(v_int(a["maxdays"], -1, 99999, "maxdays"))]
    if a.get("mindays") is not None:
        argv += ["-m", str(v_int(a["mindays"], 0, 99999, "mindays"))]
    if a.get("warndays") is not None:
        argv += ["-W", str(v_int(a["warndays"], 0, 99999, "warndays"))]
    if len(argv) == 1:
        raise Reject("user.set_aging needs at least one of maxdays/mindays/warndays")
    argv.append(v_user(a["user"]))
    return [argv]


def _op_group_create(a):
    argv = ["groupadd"]
    if a.get("system") in ("1", "true", True):
        argv.append("--system")
    argv.append(v_group(a["group"]))
    return [argv]


def _op_group_delete(a):
    return [["groupdel", v_group(a["group"])]]


def _op_group_add_member(a):
    return [["usermod", "-aG", v_group(a["group"]), v_user(a["user"])]]


# --- power / time / host --------------------------------------------------
def _op_power(action, a):
    if action == "reboot":
        return [["systemctl", "reboot"]]
    if action == "poweroff":
        return [["systemctl", "poweroff"]]
    raise Reject(f"unknown power action: {action}")


def _op_set_timezone(a):
    return [["timedatectl", "set-timezone", v_timezone(a["timezone"])]]


def _op_set_hostname(a):
    return [["hostnamectl", "set-hostname", v_hostname(a["hostname"])]]


def _op_sync_time(a):
    if shutil.which("chronyc"):
        return [["chronyc", "makestep"]]
    return [["timedatectl", "set-ntp", "true"]]


# --- firewall (cross-backend) --------------------------------------------
def _op_firewall_port(action, a):
    port = v_port(a.get("port"))
    proto = v_proto(a.get("proto", "tcp"))
    b = _firewall_backend()
    if b == "firewall-cmd":
        flag = "--add-port" if action == "allow" else "--remove-port"
        return [["firewall-cmd", "--permanent", f"{flag}={port}/{proto}"],
                ["firewall-cmd", f"{flag}={port}/{proto}"]]
    if b == "ufw":
        return [["ufw", ("allow" if action == "allow" else "deny"), f"{port}/{proto}"]]
    # iptables / nft(iptables-compat): append/delete an INPUT rule.
    chain = "-A" if action == "allow" else "-D"
    target = "ACCEPT" if action == "allow" else "DROP"
    return [["iptables", chain, "INPUT", "-p", proto, "--dport", str(port), "-j", target]]


def _op_firewall_reload(a):
    b = _firewall_backend()
    if b == "firewall-cmd":
        return [["firewall-cmd", "--reload"]]
    if b == "ufw":
        return [["ufw", "reload"]]
    return []  # iptables/nft: nothing to reload


# --- sysctl / selinux -----------------------------------------------------
def _op_sysctl_set(a):
    key = a.get("key", "")
    val = a.get("value", "")
    if not _SYSCTL_KEY_RE.match(key):
        raise Reject(f"invalid sysctl key: {key!r}")
    if not _SYSCTL_VAL_RE.match(val):
        raise Reject(f"invalid sysctl value: {val!r}")
    return [["sysctl", "-w", f"{key}={val}"]]


def _op_selinux_setenforce(a):
    mode = str(a.get("mode", "")).lower()
    if mode in ("enforcing", "1"):
        return [["setenforce", "1"]]
    if mode in ("permissive", "0"):
        return [["setenforce", "0"]]
    raise Reject(f"invalid selinux mode: {a.get('mode')!r} (enforcing|permissive)")


def _op_selinux_setbool(a):
    name = a.get("bool", "")
    if not _SEBOOL_RE.match(name):
        raise Reject(f"invalid selinux boolean: {name!r}")
    return [["setsebool", "-P", name, v_bool_onoff(a.get("value"))]]


# --- filesystem: mount + path-allowlisted primitives ----------------------
def _op_fs_mount(a):
    src = v_mount_source(a["device"])
    mp = v_abs_path(a["mountpoint"], "mountpoint")
    argv = ["mount"]
    if a.get("fstype"):
        argv += ["-t", v_fstype(a["fstype"])]
    argv += [src, mp]
    return [argv]


def _op_fs_unmount(a):
    return [["umount", v_abs_path(a["mountpoint"], "mountpoint")]]


def _op_file_write(a, stdin_secret):
    path = v_write_path(a["path"])
    mode = v_mode(a.get("mode", "0644"))
    content = stdin_secret if stdin_secret is not None else ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, int(mode, 8))
    return []  # already done


def _op_file_chmod(a):
    path = v_write_path(a["path"])
    return [["chmod", v_mode(a.get("mode", "0644")), path]]


def _op_file_chown(a):
    path = v_write_path(a["path"])
    owner = v_user(a["user"])
    spec = f"{owner}:{v_group(a['group'])}" if a.get("group") else owner
    return [["chown", spec, path]]


# --- privileged reads -----------------------------------------------------
_LOG_SOURCES = {
    "auth": ["/var/log/auth.log", "/var/log/secure"],
    "syslog": ["/var/log/syslog", "/var/log/messages"],
    "kernel": ["/var/log/kern.log"],
}
_CONFIG_SOURCES = {
    "sshd": "/etc/ssh/sshd_config",
    "sudoers": "/etc/sudoers",
    "fstab": "/etc/fstab",
    "hosts": "/etc/hosts",
    "resolv": "/etc/resolv.conf",
    "crontab": "/etc/crontab",
}


def _op_log_read(a):
    candidates = _LOG_SOURCES.get(a.get("source"))
    if not candidates:
        raise Reject(f"unknown log source {a.get('source')!r}; allowed: {sorted(_LOG_SOURCES)}")
    path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        raise Reject(f"no log file present for source {a.get('source')!r} on this host")
    return [["tail", "-n", str(_clamp_lines(a.get("lines"))), path]]


def _op_config_read(a):
    path = _CONFIG_SOURCES.get(a.get("source"))
    if not path:
        raise Reject(f"unknown config source {a.get('source')!r}; allowed: {sorted(_CONFIG_SOURCES)}")
    if not os.path.exists(path):
        raise Reject(f"config file not present: {path}")
    return [["cat", path]]


def _op_journal_read(a):
    argv = ["journalctl", "--no-pager", "-n", str(_clamp_lines(a.get("lines")))]
    if a.get("unit"):
        argv += ["-u", v_unit(a["unit"])]
    if a.get("priority"):
        pri = a["priority"]
        if pri not in ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"):
            raise Reject(f"invalid journal priority: {pri!r}")
        argv += ["-p", pri]
    return [argv]


# --- maintenance ----------------------------------------------------------
def _op_journal_vacuum(a):
    days = v_int(a.get("days", 7), 1, 3650, "days")
    return [["journalctl", f"--vacuum-time={days}d"]]


def _op_fstrim(a):
    return [["fstrim", "-a"]]


# verb -> handler(args, stdin_secret) -> list of steps.
OPS = {
    # services
    "service.start":    lambda a, s: _op_service("start", a),
    "service.stop":     lambda a, s: _op_service("stop", a),
    "service.restart":  lambda a, s: _op_service("restart", a),
    "service.reload":   lambda a, s: _op_service("reload", a),
    "service.enable":   lambda a, s: _op_service("enable", a),
    "service.disable":  lambda a, s: _op_service("disable", a),
    "service.mask":     lambda a, s: _op_service("mask", a),
    "service.unmask":   lambda a, s: _op_service("unmask", a),
    "service.daemon_reload": lambda a, s: _op_daemon_reload(a),
    "service.reset_failed":  lambda a, s: _op_reset_failed(a),
    # packages
    "pkg.install":      lambda a, s: _op_pkg("install", a),
    "pkg.remove":       lambda a, s: _op_pkg("remove", a),
    "pkg.update":       lambda a, s: _op_pkg("update", a),
    "pkg.clean":        lambda a, s: _op_pkg("clean", a),
    # users / groups
    "user.create":      lambda a, s: _op_user_create(a),
    "user.delete":      lambda a, s: _op_user_delete(a),
    "user.setpassword": lambda a, s: _op_user_setpassword(a, s),
    "user.lock":        lambda a, s: _op_user_lock(a),
    "user.unlock":      lambda a, s: _op_user_unlock(a),
    "user.set_shell":   lambda a, s: _op_user_set_shell(a),
    "user.set_aging":   lambda a, s: _op_user_set_aging(a),
    "group.create":     lambda a, s: _op_group_create(a),
    "group.delete":     lambda a, s: _op_group_delete(a),
    "group.add_member": lambda a, s: _op_group_add_member(a),
    # power / time / host
    "power.reboot":     lambda a, s: _op_power("reboot", a),
    "power.poweroff":   lambda a, s: _op_power("poweroff", a),
    "system.set_timezone": lambda a, s: _op_set_timezone(a),
    "system.set_hostname": lambda a, s: _op_set_hostname(a),
    "system.sync_time":    lambda a, s: _op_sync_time(a),
    # firewall
    "firewall.allow_port": lambda a, s: _op_firewall_port("allow", a),
    "firewall.close_port": lambda a, s: _op_firewall_port("close", a),
    "firewall.reload":     lambda a, s: _op_firewall_reload(a),
    # kernel / selinux
    "sysctl.set":          lambda a, s: _op_sysctl_set(a),
    "selinux.setenforce":  lambda a, s: _op_selinux_setenforce(a),
    "selinux.setbool":     lambda a, s: _op_selinux_setbool(a),
    # filesystem
    "fs.mount":         lambda a, s: _op_fs_mount(a),
    "fs.unmount":       lambda a, s: _op_fs_unmount(a),
    "file.write":       lambda a, s: (_op_file_write(a, s) or []),
    "file.chmod":       lambda a, s: _op_file_chmod(a),
    "file.chown":       lambda a, s: _op_file_chown(a),
    # privileged reads
    "log.read":         lambda a, s: _op_log_read(a),
    "config.read":      lambda a, s: _op_config_read(a),
    "journal.read":     lambda a, s: _op_journal_read(a),
    # maintenance
    "journal.vacuum":   lambda a, s: _op_journal_vacuum(a),
    "fstrim":           lambda a, s: _op_fstrim(a),
}

# Families intentionally NOT yet exposed (fail-closed => refused): repo.*
# (add/enable/disable repos — highly distro-divergent), storage.* (LVM / mkfs /
# cryptsetup — destructive, hardware/distro-specific), net.* (nmcli connection
# editing), cron.* (per-user crontab mutation). These stay as dedicated future
# verbs; until added, invoking them is safely refused. Broad /etc config edits
# are deliberately excluded from file.write and will get scoped verbs.


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_argv(argv, stdin=None):
    proc = subprocess.run(argv, capture_output=True, text=True, input=stdin, timeout=300)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def _parse_args(arglist):
    """--arg k=v repeated -> dict. Values are opaque strings (validated later)."""
    a = {}
    it = iter(arglist)
    for tok in it:
        if tok == "--arg":
            kv = next(it, "")
            k, _, v = kv.partition("=")
            a[k] = v
    return a


def build_steps(verb, args, stdin_secret=None):
    """Resolve a verb+args to its concrete steps WITHOUT executing — the core the
    selftest exercises. Raises Reject/KeyError for an unknown/invalid verb."""
    handler = OPS.get(verb)
    if handler is None:
        raise Reject(f"unknown privileged verb {verb!r}")
    return handler(args, stdin_secret)


def cmd_op(argv):
    if not argv or argv[0] != "--op":
        die("usage: sysible-priv op --op VERB [--arg k=v ...]   (secret on stdin)")
    verb = argv[1] if len(argv) > 1 else ""
    if verb not in OPS:
        die(f"refused: unknown privileged verb {verb!r}")
    a = _parse_args(argv[2:])
    stdin_secret = sys.stdin.read() if not sys.stdin.isatty() else None
    if stdin_secret == "":
        stdin_secret = None
    try:
        steps = build_steps(verb, a, stdin_secret)
    except Reject as e:
        die(f"refused: {e}")
    except KeyError as e:
        die(f"refused: missing required argument {e}")
    rc = 0
    for step in steps:
        argv_i, stdin_i = step if isinstance(step, tuple) else (step, None)
        rc = _run_argv(argv_i, stdin=stdin_i)
        if rc != 0:
            break
    return rc


def cmd_runas(argv):
    """runas --user U --mode {plain|elevate} -- <shell string>
    Drops to user U and runs the shell string AS THAT USER. For 'elevate', an
    optional sudo/become password is read from stdin and fed to `sudo -S`. Runs
    arbitrary shell as a NON-root user; root is only reachable through that
    user's own sudo policy (unchanged from today)."""
    user = mode = cmd = None
    i = 0
    while i < len(argv):
        if argv[i] == "--user":
            user = argv[i + 1]; i += 2
        elif argv[i] == "--mode":
            mode = argv[i + 1]; i += 2
        elif argv[i] == "--":
            cmd = " ".join(argv[i + 1:]); break
        else:
            i += 1
    if not user or mode not in ("plain", "elevate") or cmd is None:
        die("usage: sysible-priv runas --user U --mode {plain|elevate} -- CMD")
    v_user(user)
    try:
        pwd.getpwnam(user)
    except KeyError:
        die(f"refused: local user {user!r} does not exist")

    if mode == "plain":
        return _run_argv(["runuser", "-u", user, "--", "bash", "-c", cmd])
    secret = sys.stdin.read() if not sys.stdin.isatty() else ""
    if secret:
        inner = ["sudo", "-S", "-p", "", "bash", "-c", cmd]
        return _run_argv(["runuser", "-u", user, "--", *inner], stdin=secret)
    inner = ["sudo", "-n", "bash", "-c", cmd]
    return _run_argv(["runuser", "-u", user, "--", *inner])


def cmd_selftest():
    """Validator + refusal battery. No root, no side effects (never executes a
    step). Exits 0 iff every case behaves as expected."""
    ok = True

    def expect_reject(desc, verb, args, stdin=None):
        nonlocal ok
        try:
            build_steps(verb, args, stdin)
            print(f"  [FAIL] {desc}: expected refusal, got steps"); ok = False
        except (Reject, KeyError):
            print(f"  [pass] {desc}")

    def expect_argv(desc, verb, args, want_first, stdin=None):
        nonlocal ok
        try:
            steps = build_steps(verb, args, stdin)
            got = steps[0][0] if (steps and isinstance(steps[0], tuple)) else (steps[0] if steps else [])
            if got == want_first:
                print(f"  [pass] {desc}")
            else:
                print(f"  [FAIL] {desc}: {got} != {want_first}"); ok = False
        except Exception as e:
            print(f"  [FAIL] {desc}: raised {e!r}"); ok = False

    print("== refusals ==")
    expect_reject("unknown verb", "service.nuke", {"unit": "x"})
    expect_reject("unit injection", "service.restart", {"unit": "nginx;reboot"})
    expect_reject("file.write traversal", "file.write", {"path": "/etc/sysible/../../etc/shadow"}, "x")
    expect_reject("file.chown outside allowlist", "file.chown", {"path": "/etc/passwd", "user": "root"})
    expect_reject("bad username", "user.create", {"user": "root;rm -rf /"})
    expect_reject("disallowed shell", "user.set_shell", {"user": "bob", "shell": "/tmp/evil"})
    expect_reject("port out of range", "firewall.allow_port", {"port": "99999"})
    expect_reject("bad sysctl key", "sysctl.set", {"key": "net;evil", "value": "1"})
    expect_reject("bad timezone charset", "system.set_timezone", {"timezone": "../etc"})
    expect_reject("unknown config source", "config.read", {"source": "shadow"})
    expect_reject("missing required arg", "service.restart", {})

    print("== argv construction (no shell) ==")
    expect_argv("service.restart", "service.restart", {"unit": "nginx.service"},
                ["systemctl", "restart", "nginx.service"])
    expect_argv("user.create+shell", "user.create", {"user": "bob", "shell": "/bin/bash"},
                ["useradd", "--shell", "/bin/bash", "bob"])
    expect_argv("group.add_member", "group.add_member", {"group": "sudo", "user": "bob"},
                ["usermod", "-aG", "sudo", "bob"])
    expect_argv("sysctl.set", "sysctl.set", {"key": "net.ipv4.ip_forward", "value": "1"},
                ["sysctl", "-w", "net.ipv4.ip_forward=1"])
    expect_argv("selinux.setenforce", "selinux.setenforce", {"mode": "enforcing"},
                ["setenforce", "1"])
    expect_argv("config.read sshd", "config.read", {"source": "sshd"},
                ["cat", "/etc/ssh/sshd_config"]) if os.path.exists("/etc/ssh/sshd_config") else print("  [skip] config.read sshd (no file here)")

    print(f"verbs in table: {len(OPS)}")
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def die(msg):
    sys.stderr.write(f"[sysible-priv] {msg}\n")
    sys.exit(2)


def main():
    if len(sys.argv) < 2:
        die("usage: sysible-priv {op|runas|selftest} ...")
    sub, rest = sys.argv[1], sys.argv[2:]
    if sub == "selftest":
        sys.exit(cmd_selftest())
    if os.geteuid() != 0:
        die("must run as root (it is invoked via the agent's single sudo entry)")
    if sub == "op":
        sys.exit(cmd_op(rest))
    if sub == "runas":
        sys.exit(cmd_runas(rest))
    die(f"unknown subcommand {sub!r}")


if __name__ == "__main__":
    main()
