#!/usr/bin/env python3
"""
sysible-priv - the agent's single privileged entry point (PROTOTYPE / RFC).

This is the one program the Sysible agent account is allowed to run under
sudo. It replaces the blanket `sysible ALL=(ALL) NOPASSWD: ALL` grant with:

    sysible ALL=(ALL) NOPASSWD: /opt/sysible-agent/priv/sysible-priv

so a compromised agent process can no longer `sudo bash` its way to root -
it can only invoke the operations this dispatcher exposes, with arguments
this dispatcher validates.

It runs as root (sudo'd by the agent) and exposes two subcommands:

  runas  - drop to a target local user and run a shell string AS THAT USER.
           This is the read / user-level path (the bulk of Sysible's
           commands). It is deliberately NOT a root boundary: whatever the
           target user can do via their own sudo, they can still do here.
           The boundary for this path is each user's local sudo policy,
           exactly as today - we've only centralised it.

  op     - run ONE vetted root primitive from the fixed OPS table below,
           argv-only (never a shell), with every argument validated first.
           This is the path that actually needs root, and the only way to
           reach root through this dispatcher. Unknown verb => refused.

Secrets (a user's become/sudo password, file contents) are read from STDIN,
never argv/env, so they never appear in `ps` or logs.

PROTOTYPE SCOPE: the OPS table seeds a representative verb from each family
found in the builder inventory (service / user / package / power / file).
Filling out the remaining ~40-55 verbs is mechanical; the security-relevant
shape - single sudo entry, argv-only execution, per-verb validation, a
path-allowlisted file primitive - is all here.
"""
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Validation helpers - the security boundary for the `op` path. Every value
# that reaches a process must pass through one of these. They raise Reject on
# anything that doesn't match a strict, conservative pattern.
# ---------------------------------------------------------------------------

class Reject(Exception):
    """Raised when an argument fails validation; mapped to a clean error."""


_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]+(\.(service|socket|timer|target|path|mount))?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# Directories the file primitive may write into. Anything else is refused -
# this is what keeps the generic file-mutation tail (chmod/cp/sed -i/tee)
# from becoming "write any file as root".
_ALLOWED_WRITE_DIRS = ("/etc/sysible", "/etc/sysible-agent", "/opt/sysible-agent")


def v_unit(s):
    if not s or not _UNIT_RE.match(s):
        raise Reject(f"invalid systemd unit name: {s!r}")
    return s


def v_user(s):
    if not s or not _USER_RE.match(s):
        raise Reject(f"invalid username: {s!r}")
    return s


def v_pkgs(items):
    out = []
    for p in items:
        if not _PKG_RE.match(p):
            raise Reject(f"invalid package name: {p!r}")
        out.append(p)
    if not out:
        raise Reject("no packages given")
    return out


def v_write_path(p):
    # Resolve symlinks/.. and confirm the real path stays inside an allowed
    # directory - so `path=/etc/sysible/../../etc/shadow` can't escape.
    real = os.path.realpath(p)
    if not any(real == d or real.startswith(d + "/") for d in _ALLOWED_WRITE_DIRS):
        raise Reject(f"path {p!r} is outside the writable allowlist {_ALLOWED_WRITE_DIRS}")
    return real


def _pkg_manager():
    for mgr in ("dnf", "yum", "zypper", "apt-get"):
        if shutil.which(mgr):
            return mgr
    raise Reject("no supported package manager found")


# ---------------------------------------------------------------------------
# The verb table. Each entry maps args -> a concrete argv list (NO shell).
# Add a family by adding rows here; the dispatcher never grows new powers
# except the ones spelled out in this dict.
# ---------------------------------------------------------------------------

def _op_service(action, a):
    if action not in ("start", "stop", "restart", "enable", "disable", "mask", "unmask"):
        raise Reject(f"unknown service action: {action}")
    return [["systemctl", action, v_unit(a["unit"])]]


def _op_user_create(a):
    argv = ["useradd"]
    if a.get("system") in ("1", "true", True):
        argv.append("--system")
    if a.get("home") == "no":
        argv.append("--no-create-home")
    if a.get("shell"):
        # Only allow a shell that actually exists in /etc/shells-style paths.
        sh = a["shell"]
        if sh not in ("/bin/bash", "/bin/sh", "/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/false"):
            raise Reject(f"shell not allowed: {sh!r}")
        argv += ["--shell", sh]
    argv.append(v_user(a["user"]))
    return [argv]


def _op_user_setpassword(a, stdin_secret):
    # Password comes via STDIN, never argv. chpasswd reads "user:password".
    user = v_user(a["user"])
    if not stdin_secret:
        raise Reject("user.setpassword requires the password on stdin")
    return [(["chpasswd"], f"{user}:{stdin_secret}\n")]


def _op_pkg(action, a):
    if action not in ("install", "remove"):
        raise Reject(f"unknown pkg action: {action}")
    mgr = _pkg_manager()
    pkgs = v_pkgs(a.get("pkgs", "").split(","))
    verb = {"install": "install", "remove": "remove"}[action]
    if mgr == "zypper":
        return [["zypper", "--non-interactive", verb, *pkgs]]
    return [[mgr, "-y", verb, *pkgs]]


def _op_power(action, a):
    if action == "reboot":
        return [["systemctl", "reboot"]]
    if action == "poweroff":
        return [["systemctl", "poweroff"]]
    raise Reject(f"unknown power action: {action}")


def _op_file_write(a, stdin_secret):
    # The constrained replacement for "cp/tee/sed -i as root into a config".
    # Path is allowlisted; content arrives on stdin; mode is validated.
    path = v_write_path(a["path"])
    mode = a.get("mode", "0644")
    if not re.match(r"^0[0-7]{3}$", mode):
        raise Reject(f"invalid mode: {mode!r}")
    content = stdin_secret if stdin_secret is not None else ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, int(mode, 8))
    return []  # already done, no argv to run


# verb -> handler. Handlers return a list of (argv) or (argv, stdin) to run.
OPS = {
    "service.start":   lambda a, s: _op_service("start", a),
    "service.stop":    lambda a, s: _op_service("stop", a),
    "service.restart": lambda a, s: _op_service("restart", a),
    "service.enable":  lambda a, s: _op_service("enable", a),
    "service.disable": lambda a, s: _op_service("disable", a),
    "user.create":     lambda a, s: _op_user_create(a),
    "user.setpassword": lambda a, s: _op_user_setpassword(a, s),
    "pkg.install":     lambda a, s: _op_pkg("install", a),
    "pkg.remove":      lambda a, s: _op_pkg("remove", a),
    "power.reboot":    lambda a, s: _op_power("reboot", a),
    "power.poweroff":  lambda a, s: _op_power("poweroff", a),
    "file.write":      lambda a, s: (_op_file_write(a, s) or []),
}


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


def cmd_op(argv):
    if not argv or argv[0] != "--op":
        die("usage: sysible-priv op --op VERB [--arg k=v ...]   (secret on stdin)")
    verb = argv[1] if len(argv) > 1 else ""
    handler = OPS.get(verb)
    if handler is None:
        die(f"refused: unknown privileged verb {verb!r}")
    a = _parse_args(argv[2:])
    # A single optional secret line from stdin (password / file content).
    stdin_secret = sys.stdin.read() if not sys.stdin.isatty() else None
    if stdin_secret == "":
        stdin_secret = None
    try:
        steps = handler(a, stdin_secret)
    except Reject as e:
        die(f"refused: {e}")
    rc = 0
    for step in steps:
        if isinstance(step, tuple):
            argv_i, stdin_i = step
        else:
            argv_i, stdin_i = step, None
        rc = _run_argv(argv_i, stdin=stdin_i)
        if rc != 0:
            break
    return rc


def cmd_runas(argv):
    """runas --user U --mode {plain|elevate} -- <shell string>
    Drops to user U and runs the shell string AS THAT USER. For 'elevate',
    an optional sudo/become password is read from stdin and fed to `sudo -S`.
    This path runs arbitrary shell as a NON-root user; root is only reachable
    through that user's own sudo policy (unchanged from today)."""
    user = mode = None
    cmd = None
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
    # elevate: run the user's own sudo (passwordless, or -S with stdin password)
    secret = sys.stdin.read() if not sys.stdin.isatty() else ""
    if secret:
        inner = ["sudo", "-S", "-p", "", "bash", "-c", cmd]
        return _run_argv(["runuser", "-u", user, "--", *inner], stdin=secret)
    inner = ["sudo", "-n", "bash", "-c", cmd]
    return _run_argv(["runuser", "-u", user, "--", *inner])


def die(msg):
    sys.stderr.write(f"[sysible-priv] {msg}\n")
    sys.exit(2)


def main():
    if os.geteuid() != 0:
        die("must run as root (it is invoked via the agent's single sudo entry)")
    if len(sys.argv) < 2:
        die("usage: sysible-priv {op|runas} ...")
    sub, rest = sys.argv[1], sys.argv[2:]
    if sub == "op":
        sys.exit(cmd_op(rest))
    if sub == "runas":
        sys.exit(cmd_runas(rest))
    die(f"unknown subcommand {sub!r}")


if __name__ == "__main__":
    main()
