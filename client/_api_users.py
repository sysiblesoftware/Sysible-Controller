"""Users/groups/remote-hosts/file-transfer/fleet-user-mgmt/password helpers - split out of client/api.py to keep individual file sizes manageable. Imported via `from client._api_users import *` at the bottom of client/api.py."""
import base64
import hashlib
import json
import random
import secrets
import shlex
import string
import re
from pathlib import Path

from client.api import _request, _download_binary


# NOTE: The local /users and /groups route wrappers that used to live here
# were removed - they targeted OS users/groups on the *controller's own
# machine* (backend/routes/users.py + groups.py), a model the app no longer
# uses. All user/group management is now dispatched to enrolled remote hosts
# via the cmd_* builders further down this file.


def list_hosts():
    return _request("GET", "/remote/hosts")


def delete_host(name: str):
    return _request("DELETE", f"/remote/hosts/{name}")


def get_controller_key():
    return _request("GET", "/remote/controller-key").get("public_key")


def enroll_ssh(name: str, ip: str, username: str, password: str, environment: str = ""):
    return _request("POST", "/remote/enroll-ssh", json={
        "name": name, "ip": ip, "username": username or "root", "password": password, "environment": environment or "",
    })


def set_host_environment(name: str, environment: str):
    return _request("POST", f"/remote/hosts/{name}/environment", json={"environment": environment})


def exec_remote(name: str, cmd: str, description: str = None, become_password: str = None,
                log: bool = True):
    body = {"cmd": cmd}
    if description:
        body["description"] = description
    if become_password:
        body["become_password"] = become_password
    if not log:
        body["log"] = False
    return _request("POST", f"/remote/hosts/{name}/exec", json=body)


def open_terminal(name: str):
    # Returns {"host", "session_id", "opened"}. Each call opens a fresh,
    # independent session; read/write/close address it by session_id.
    return _request("POST", f"/remote/hosts/{name}/terminal/open", timeout=20)


def write_terminal(session_id: str, data: str):
    return _request("POST", f"/remote/terminal/{session_id}/write", json={"data": data})


def read_terminal(session_id: str):
    return _request("GET", f"/remote/terminal/{session_id}/read")


def close_terminal(session_id: str):
    return _request("POST", f"/remote/terminal/{session_id}/close")


def resize_terminal(session_id: str, cols: int, rows: int):
    return _request(
        "POST", f"/remote/terminal/{session_id}/resize",
        json={"cols": cols, "rows": rows},
    )


def upload_file_ssh(name: str, local_path, remote_path: str):
    local_path = Path(local_path)
    with open(local_path, "rb") as f:
        return _request(
            "POST", f"/remote/hosts/{quote(name, safe='')}/files/upload",
            data={"remote_path": remote_path}, files={"file": (local_path.name, f)}, timeout=120,
        )


def download_file_ssh(name: str, remote_path: str, save_path):
    qpath = quote(remote_path, safe="")
    data = _download_binary(f"/remote/hosts/{quote(name, safe='')}/files/download?path={qpath}")
    Path(save_path).write_bytes(data)
    return save_path


from urllib.parse import quote

AGENT_FILE_TRANSFER_LIMIT_BYTES = 140_000


def _build_agent_upload_script(remote_path: str, filename: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode()
    return (
        "import base64, os\n"
        "remote_path = " + repr(remote_path) + "\n"
        "if os.path.isdir(remote_path):\n"
        "    remote_path = os.path.join(remote_path, " + repr(filename) + ")\n"
        "data = base64.b64decode(" + repr(encoded) + ")\n"
        "with open(remote_path, 'wb') as f:\n"
        "    f.write(data)\n"
        "print(remote_path)\n"
    )


def _build_agent_download_script(remote_path: str) -> str:
    return (
        "import base64, os, sys\n"
        "path = " + repr(remote_path) + "\n"
        "if not os.path.isfile(path):\n"
        "    print('not a file or not found: ' + path, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "size = os.path.getsize(path)\n"
        "limit = " + str(AGENT_FILE_TRANSFER_LIMIT_BYTES) + "\n"
        "if size > limit:\n"
        "    print('file too large for agent transfer (%d bytes, limit %d)' % (size, limit), file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "with open(path, 'rb') as f:\n"
        "    print(base64.b64encode(f.read()).decode())\n"
    )


def queue_agent_upload(host_id: str, local_path, remote_path: str, become_password: str = None):
    local_path = Path(local_path)
    size = local_path.stat().st_size
    if size > AGENT_FILE_TRANSFER_LIMIT_BYTES:
        return {"task_id": None, "error": (
            f"File is {size} bytes - agent-host uploads are limited to "
            f"{AGENT_FILE_TRANSFER_LIMIT_BYTES} bytes. SSH hosts have no such limit."
        )}
    script = _build_agent_upload_script(remote_path, local_path.name, local_path.read_bytes())
    cmd = _wrap_python_script(script)
    # Writing under a privileged path (e.g. /etc) needs root - the caller passes
    # the operator's sudo password for password-sudo hosts (resolved from the
    # controller-side store), same as run_on_entry.
    task_ids = queue_command_on_hosts([host_id], cmd, kind="upload_file",
                                      become_password=become_password)
    task_id = task_ids.get(host_id)
    return {"task_id": task_id, "error": None if task_id is not None else "failed to queue upload"}


def poll_agent_upload(host_id: str, task_id):
    raw = get_result_by_task(host_id, task_id)
    output = parse_task_output(raw)
    if output is None:
        return None
    if output.get("returncode") == 0:
        return {"error": None, "remote_path": (output.get("stdout") or "").strip()}
    return {"error": output.get("stderr") or f"upload exited {output.get('returncode')}", "remote_path": None}


def queue_agent_download(host_id: str, remote_path: str, become_password: str = None):
    script = _build_agent_download_script(remote_path)
    cmd = _wrap_python_script(script)
    # Reading a root-only file needs root - the caller passes the sudo password
    # for password-sudo hosts (resolved from the controller-side store) so the
    # agent can elevate.
    task_ids = queue_command_on_hosts([host_id], cmd, kind="download_file",
                                      become_password=become_password)
    task_id = task_ids.get(host_id)
    return {"task_id": task_id, "error": None if task_id is not None else "failed to queue download"}


def poll_agent_download(host_id: str, task_id, save_path):
    raw = get_result_by_task(host_id, task_id)
    output = parse_task_output(raw)
    if output is None:
        return None
    if output.get("returncode") != 0:
        return {"error": output.get("stderr") or f"download exited {output.get('returncode')}"}
    try:
        data = base64.b64decode((output.get("stdout") or "").strip())
    except (ValueError, TypeError) as e:
        return {"error": f"could not decode file data from agent: {e}"}
    try:
        Path(save_path).write_bytes(data)
    except OSError as e:
        return {"error": f"could not save file locally: {e}"}
    return {"error": None}


def queue_command_on_hosts(host_ids, command: str, kind: str = "command",
                           description: str = None, become_password: str = None,
                           log: bool = True):
    body_base = {"command": command, "kind": kind}
    if description:
        body_base["description"] = description
    if become_password:
        body_base["become_password"] = become_password
    if not log:
        body_base["log"] = False
    task_ids = {}
    for host_id in host_ids:
        try:
            result = _request("POST", f"/agents/{host_id}/tasks", json=body_base)
            task_ids[host_id] = result.get("task_id") if result else None
        except Exception:
            task_ids[host_id] = None
    return task_ids


def get_result_by_task(host_id: str, task_id: int):
    if task_id is None:
        return None
    results = _request("GET", f"/agents/{host_id}/results", params={"task_id": task_id}).get("results", [])
    return results[0] if results else None


def get_latest_result(host_id: str, kind: str = None):
    params = {"kind": kind} if kind else {}
    results = _request("GET", f"/agents/{host_id}/results", params=params).get("results", [])
    return results[0] if results else None


def parse_task_output(raw_result):
    if not raw_result or not raw_result.get("result"):
        return None
    try:
        return json.loads(raw_result["result"])
    except (TypeError, ValueError):
        return None


_SYNC_USERS_SCRIPT = """
import pwd, grp, subprocess, json

def _groups(username):
    try:
        out = subprocess.run(["id", "-nG", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout
        return out.split()
    except Exception:
        return []

def _locked(username):
    try:
        out = subprocess.run(["passwd", "-S", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout
        fields = out.split()
        return len(fields) > 1 and fields[1].startswith("L")
    except Exception:
        return False

def _sessions():
    sessions = []
    try:
        out = subprocess.run(["who"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                sessions.append({"username": parts[0], "tty": parts[1]})
    except Exception:
        pass
    return sessions

users = []
for u in pwd.getpwall():
    if u.pw_uid < 1000:
        continue
    groups = _groups(u.pw_name)
    users.append({
        "username": u.pw_name, "uid": u.pw_uid, "gid": u.pw_gid, "home": u.pw_dir,
        "shell": u.pw_shell, "groups": groups, "sudo": bool(set(groups) & {"sudo", "wheel", "admin"}), "locked": _locked(u.pw_name),
    })

groups = []
for g in grp.getgrall():
    groups.append({"name": g.gr_name, "gid": g.gr_gid, "members": g.gr_mem})

print(json.dumps({"users": users, "groups": groups, "sessions": _sessions()}))
"""


def _wrap_python_script(script: str) -> str:
    encoded = base64.b64encode(script.encode()).decode()
    return f'python3 -c "import base64;exec(base64.b64decode(\'{encoded}\').decode())"'


def build_user_sync_command() -> str:
    return _wrap_python_script(_SYNC_USERS_SCRIPT)


def sync_hosts(host_ids):
    return queue_command_on_hosts(host_ids, build_user_sync_command(), kind="sync_users")


def get_sync_result(host_id: str, task_id: int = None):
    raw = (get_result_by_task(host_id, task_id) if task_id is not None else get_latest_result(host_id, kind="sync_users"))
    output = parse_task_output(raw)
    if not output or output.get("returncode") != 0:
        return None
    try:
        return json.loads(output["stdout"])
    except (TypeError, ValueError, KeyError):
        return None


PASSWORD_POLICY_PRESETS = {
    "Basic": {"minlen": 8, "retry": 3, "dcredit": 0, "ucredit": 0, "lcredit": 0, "ocredit": 0, "deny": 5, "unlock_time": 600},
    "Standard": {"minlen": 12, "retry": 3, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": 0, "deny": 5, "unlock_time": 900},
    "Strict": {"minlen": 16, "retry": 3, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1, "deny": 3, "unlock_time": 1800},
}

_DEFAULT_PASSWORD_POLICY = {"minlen": 12, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1}


def check_password_strength(password: str, policy: dict = None):
    policy = policy or _DEFAULT_PASSWORD_POLICY
    minlen = policy.get("minlen", 12)
    if len(password) < minlen:
        return False, f"Password must be at least {minlen} characters long."
    if policy.get("lcredit", 0) < 0 and not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter."
    if policy.get("ucredit", 0) < 0 and not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter."
    if policy.get("dcredit", 0) < 0 and not re.search(r"[0-9]", password):
        return False, "Password must include at least one digit."
    if policy.get("ocredit", 0) < 0 and not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must include at least one symbol (e.g. ! @ # $ %)."
    return True, ""


def generate_strong_password(length: int = 16, policy: dict = None) -> str:
    policy = policy or _DEFAULT_PASSWORD_POLICY
    length = max(length, policy.get("minlen", 12))
    lower, upper, digits = string.ascii_lowercase, string.ascii_uppercase, string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    pool = lower + upper + digits + symbols
    required_pools = []
    if policy.get("lcredit", 0) < 0:
        required_pools.append(lower)
    if policy.get("ucredit", 0) < 0:
        required_pools.append(upper)
    if policy.get("dcredit", 0) < 0:
        required_pools.append(digits)
    if policy.get("ocredit", 0) < 0:
        required_pools.append(symbols)
    chars = [secrets.choice(p) for p in required_pools]
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    rng = random.SystemRandom()
    rng.shuffle(chars)
    return "".join(chars)


# SHA-512 crypt ($6$) alphabet and 24-bit -> base64 packer used by the
# pure-Python fallback below. This is the crypt-specific base64 (NOT the
# standard one) with its own character order and little-endian grouping.
_CRYPT_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _crypt_b64_from_24bit(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_CRYPT_ITOA64[w & 0x3F])
        w >>= 6
    return "".join(out)


def _sha512_crypt(password: str, salt: str) -> str:
    """Pure-Python implementation of the glibc SHA-512 crypt ($6$) scheme
    (Ulrich Drepper's spec, https://www.akkadia.org/drepper/SHA-crypt.txt),
    used when the stdlib `crypt` module is unavailable.

    `crypt` was removed from the standard library in Python 3.13 and never
    existed on non-POSIX platforms, so a controller running a modern Python
    could no longer build `usermod -p '$6$...'` commands. hashlib.sha512 is
    always present, so this fallback keeps password hashing local to the
    controller - the produced hash is byte-for-byte identical to what glibc
    crypt() emits, so the plaintext still never leaves this machine. Uses the
    default 5000 rounds and a 16-char salt, matching crypt.mksalt()."""
    pw = password.encode("utf-8")
    salt_bytes = salt.encode("ascii")[:16]
    plen = len(pw)

    # Digest B = SHA512(password + salt + password)
    alt = hashlib.sha512(pw + salt_bytes + pw).digest()

    # Digest A
    a = hashlib.sha512()
    a.update(pw + salt_bytes)
    i = plen
    while i > 0:
        a.update(alt[: min(i, 64)])
        i -= 64
    i = plen
    while i > 0:
        a.update(alt if (i & 1) else pw)
        i >>= 1
    a_digest = a.digest()

    # DP -> sequence P (password repeated), DS -> sequence S (salt repeated)
    dp = hashlib.sha512(pw * plen).digest()
    p_seq = (dp * (plen // 64 + 1))[:plen]
    ds = hashlib.sha512(salt_bytes * (16 + a_digest[0])).digest()
    s_seq = (ds * (len(salt_bytes) // 64 + 1))[: len(salt_bytes)]

    # 5000 rounds of mixing
    c = a_digest
    for r in range(5000):
        ctx = hashlib.sha512()
        ctx.update(p_seq if (r & 1) else c)
        if r % 3:
            ctx.update(s_seq)
        if r % 7:
            ctx.update(p_seq)
        ctx.update(c if (r & 1) else p_seq)
        c = ctx.digest()

    # crypt-specific base64 with glibc's byte permutation
    order = [
        (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
        (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
        (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
        (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
        (62, 20, 41),
    ]
    enc = [_crypt_b64_from_24bit(c[x], c[y], c[z], 4) for (x, y, z) in order]
    enc.append(_crypt_b64_from_24bit(0, 0, c[63], 2))
    return "$6$%s$%s" % (salt_bytes.decode("ascii"), "".join(enc))


def _hash_password(password: str):
    # Prefer the stdlib crypt when present (POSIX, Python < 3.13); otherwise
    # fall back to the pure-Python SHA-512 crypt so a controller on Python 3.13+
    # or a non-POSIX host can still hash locally instead of refusing the change.
    try:
        import crypt
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except (ImportError, OSError):
        pass
    alphabet = "./" + string.ascii_letters + string.digits
    salt = "".join(secrets.choice(alphabet) for _ in range(16))
    return _sha512_crypt(password, salt)


def _reject_leading_dash(username: str):
    """Reject a username beginning with '-'. shlex.quote stops shell metacharacters but
    a leading dash makes useradd/usermod/userdel/chage parse the value as an OPTION
    instead of an operand. Linux usernames can't start with '-' anyway, so this only
    blocks the option-injection shape — defense-in-depth (these tools have no
    command-executing option, so it was never RCE), keeping the validators consistent
    with _api_security's."""
    if (username or "").strip().startswith("-"):
        raise ValueError("Invalid username: cannot begin with '-'.")


def cmd_create_user(username: str, password: str = "", shell: str = "/bin/bash") -> str:
    _reject_leading_dash(username)
    u = shlex.quote(username)
    sh = shlex.quote(shell or "/bin/bash")
    # `useradd -m` should create and populate the home directory, but a few
    # setups don't (CREATE_HOME disabled in /etc/login.defs, a useradd that
    # only honours --create-home, an empty/odd /etc/skel, or the home base on
    # a mount that wasn't ready). So after creating the account, look up the
    # home path the account was actually given and, if it's still missing,
    # build it explicitly from /etc/skel with the right owner/permissions -
    # then verify it exists and fail loudly if it still doesn't.
    ensure_home = (
        f'h="$(getent passwd {u} | cut -d: -f6)"; '
        f'if [ -n "$h" ] && [ ! -d "$h" ]; then '
        f'  mkdir -p "$h"; cp -aT /etc/skel "$h" 2>/dev/null || true; '
        f'  chown -R {u}:{u} "$h" 2>/dev/null || true; chmod 700 "$h"; '
        f'fi; '
        f'if [ -z "$h" ] || [ ! -d "$h" ]; then '
        f'  echo "user created but home directory could not be created" >&2; exit 1; '
        f'fi; '
        f'echo "home directory ready: $h"'
    )
    # useradd defaults to creating a private group named after the user
    # (USERGROUPS_ENAB). If a group of that name already exists but the user
    # does not - e.g. Ubuntu ships a legacy `admin` group - useradd refuses with
    # "group <name> exists" (exit 9). Detect that and fall back to -N so the
    # account is created with the distro's default group instead of colliding.
    # -N does NOT add the user to the pre-existing group, so no unexpected
    # group membership or privilege is granted.
    create = (
        f"if getent group {u} >/dev/null 2>&1; then "
        f"useradd -m -N -s {sh} {u}; "
        f"else useradd -m -s {sh} {u}; fi"
    )
    cmd = f"{create} && {{ {ensure_home}; }}"
    if password:
        cmd += " && " + cmd_set_password(username, password)
    return cmd


# These low-level account commands emit nothing on success, so the console just
# showed a bare "exit 0" with no confirmation of what happened. Append a
# VALUE-FREE confirmation echo (never interpolate the username — a value like
# `$(id)` would command-substitute inside the double-quoted string, exactly the
# injection cmd_set_sudo's comment warns about; the caller already knows the user).
def cmd_delete_user(username: str) -> str:
    _reject_leading_dash(username)
    return f'userdel -r {shlex.quote(username)} && echo "User deleted (home directory removed)."'


def cmd_lock_user(username: str) -> str:
    _reject_leading_dash(username)
    return f'usermod -L {shlex.quote(username)} && echo "User locked — password authentication is now disabled."'


def cmd_unlock_user(username: str) -> str:
    _reject_leading_dash(username)
    return f'usermod -U {shlex.quote(username)} && echo "User unlocked — password authentication is re-enabled."'


def cmd_set_sudo(username: str, enable: bool) -> str:
    u = shlex.quote(username)
    # The sudo-granting group differs by distro: 'sudo' on Debian/Ubuntu,
    # 'wheel' on RHEL/Fedora/SUSE. Detect by package manager (apt => sudo,
    # otherwise wheel), the same family split used elsewhere. Removal uses
    # gpasswd -d, which is portable (deluser is Debian-only).
    detect = "if command -v apt-get >/dev/null 2>&1; then grp=sudo; else grp=wheel; fi"
    if enable:
        return (
            f"{detect}; "
            f'if ! getent group "$grp" >/dev/null 2>&1; then '
            f'echo "sudo group \'$grp\' does not exist on this host." >&2; exit 1; fi; '
            # The username appears ONLY in the shlex-quoted usermod arg ({u}).
            # It must not be interpolated into the confirmation echo — neither
            # raw nor shlex-quoted is safe inside a double-quoted string, since a
            # value like `$(id)` still command-substitutes there. Keep the echo
            # value-free; the caller already knows which user it acted on.
            f'usermod -aG "$grp" {u} || exit 1; '
            # Group membership only confers sudo if the sudoers policy actually
            # references that group. Debian (%sudo) and RHEL/Fedora (%wheel) enable
            # it by default, but openSUSE/SLES ship the %wheel line COMMENTED OUT, so
            # adding the user to wheel would grant nothing while still reporting
            # success. Install a minimal, visudo-validated drop-in that grants the
            # detected group standard admin sudo (no NOPASSWD, no extra privilege).
            # On distros that already grant the group this is a harmless duplicate.
            'tmp=$(mktemp) || exit 1; '
            'printf \'%%%s ALL=(ALL:ALL) ALL\\n\' "$grp" > "$tmp"; '
            'if visudo -cf "$tmp" >/dev/null 2>&1; then '
            'install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/sysible-sudo-group; '
            'fi; rm -f "$tmp"; '
            'echo "Granted sudo (added to group $grp)."'
        )
    return (
        f"{detect}; "
        # No 2>&1 here: a privilege failure must stay on stderr so the agent's
        # run-as-user path recognizes it and retries under the host's sudo.
        f'gpasswd -d {u} "$grp" && echo "Revoked sudo (removed from group $grp)."'
    )


def cmd_set_password(username: str, password: str) -> str:
    _reject_leading_dash(username)
    if "\n" in (username or "") or "\r" in (username or "") or ":" in (username or ""):
        raise ValueError("Username must not contain a newline or ':'.")
    hashed = _hash_password(password)
    if hashed is None:
        raise RuntimeError("Password hashing unavailable on this client (the `crypt` module requires a POSIX system) - refusing to queue a remote password change rather than send it as plaintext.")
    # Feed the crypt hash to chpasswd on STDIN (heredoc) rather than usermod -p on argv,
    # so the hash is never exposed via `ps`/procfs during the exec. A crypt hash contains
    # no newline, and the username is validated above, so the `user:hash` line is safe.
    return f"chpasswd -e <<'SYSIBLE_PW_EOF'\n{username}:{hashed}\nSYSIBLE_PW_EOF"


def cmd_create_group(name: str) -> str:
    _reject_leading_dash(name)
    return f"groupadd {shlex.quote(name)}"


def cmd_delete_group(name: str) -> str:
    _reject_leading_dash(name)
    return f"groupdel {shlex.quote(name)}"


def cmd_add_user_to_group(group: str, username: str) -> str:
    _reject_leading_dash(group)
    _reject_leading_dash(username)
    return f"usermod -aG {shlex.quote(group)} {shlex.quote(username)}"


def cmd_remove_user_from_group(group: str, username: str) -> str:
    _reject_leading_dash(group)
    _reject_leading_dash(username)
    return f"gpasswd -d {shlex.quote(username)} {shlex.quote(group)}"


def cmd_kill_user_sessions(username: str) -> str:
    user = shlex.quote(username)
    return (
        "if command -v loginctl >/dev/null 2>&1; then "
        f"loginctl terminate-user {user}; rc=$?; "
        f"else pkill -KILL -u {user}; rc=$?; "
        # pkill exits 1 when NO processes matched — that is "no active sessions",
        # not a failure; normalise so we do not report a spurious error.
        "[ \"$rc\" -eq 1 ] && rc=0; fi; "
        # Propagate a REAL failure (e.g. polkit 'Interactive authentication
        # required' on the unprivileged run-as-user attempt) so the agent/SSH
        # path retries under sudo, instead of a trailing echo masking it as 0.
        "if [ \"$rc\" -ne 0 ]; then exit \"$rc\"; fi; "
        "echo 'Done (sessions terminated, or there were none).'"
    )


def cmd_force_password_reset(username: str) -> str:
    _reject_leading_dash(username)
    return f"chage -d 0 {shlex.quote(username)}"


def cmd_set_password_aging(username: str, max_days=None, min_days=None, warn_days=None) -> str:
    user = shlex.quote(username)
    parts = ["chage"]
    if max_days is not None:
        parts += ["-M", str(int(max_days))]
    if min_days is not None:
        parts += ["-m", str(int(min_days))]
    if warn_days is not None:
        parts += ["-W", str(int(warn_days))]
    if len(parts) == 1:
        raise ValueError("Specify at least one of max / min / warn days")
    parts.append(user)
    return " ".join(parts)


def cmd_set_account_expiration(username: str, expire_date: str = "") -> str:
    user = shlex.quote(username)
    date = expire_date.strip()
    if not date:
        return f"chage -E -1 {user}"
    return f"chage -E {shlex.quote(date)} {user}"


def cmd_set_user_shell(username: str, shell: str) -> str:
    _reject_leading_dash(username)
    return f"usermod -s {shlex.quote(shell)} {shlex.quote(username)}"


def cmd_set_user_comment(username: str, comment: str) -> str:
    _reject_leading_dash(username)  # consistency with every other account builder
    return f"usermod -c {shlex.quote(comment)} {shlex.quote(username)}"


def cmd_audit_privileged_users() -> str:
    return (
        "echo '== sudo/wheel/admin group members ==' && "
        "for g in sudo wheel admin; do "
        "getent group \"$g\" 2>/dev/null | awk -F: '{print $1\": \"$4}'; "
        "done; "
        "echo && echo '== Accounts with UID 0 besides root ==' && "
        "(awk -F: '($3==0 && $1!=\"root\"){print $1}' /etc/passwd || true); "
        "echo && echo '== sudoers NOPASSWD entries ==' && "
        "(grep -rH 'NOPASSWD' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v '^[^:]*:#' || echo 'None found.')"
    )


def cmd_list_groups_with_members() -> str:
    return "getent group | awk -F: '{print $1\": \"$4}'"


def _set_security_conf_keys(path: str, settings: dict, sep: str = " = ") -> str:
    quoted_path = shlex.quote(path)
    steps = [f"touch {quoted_path}"]
    for key, value in settings.items():
        steps.append(
            f"(grep -qE '^[[:space:]]*#?[[:space:]]*{key}([[:space:]]|=)' {quoted_path} "
            f"&& sed -i -E 's/^[[:space:]]*#?[[:space:]]*{key}([[:space:]]|=).*/{key}{sep}{value}/' {quoted_path} "
            f"|| echo '{key}{sep}{value}' >> {quoted_path})"
        )
    return " && ".join(steps)


def cmd_set_password_quality_policy(minlen=None, retry=None, dcredit=None, ucredit=None, lcredit=None, ocredit=None) -> str:
    raw = {"minlen": minlen, "retry": retry, "dcredit": dcredit, "ucredit": ucredit, "lcredit": lcredit, "ocredit": ocredit}
    settings = {k: int(v) for k, v in raw.items() if v is not None}
    if not settings:
        raise ValueError("Specify at least one password-quality setting")
    # pwquality.conf is inert unless pam_pwquality is wired into the password stack.
    # RHEL/Fedora/SUSE do this by default; Debian/Ubuntu do not install/reference
    # libpam-pwquality by default, so warn rather than imply the policy is enforced.
    return (
        _set_security_conf_keys("/etc/security/pwquality.conf", settings)
        + "; rc=$?; if command -v apt-get >/dev/null 2>&1 && ! grep -rq pam_pwquality /etc/pam.d/ 2>/dev/null; then "
        "echo 'Note: pam_pwquality is not referenced in this host'\"'\"'s PAM stack "
        "(Debian/Ubuntu default). Install libpam-pwquality and enable it in "
        "/etc/pam.d/common-password for this to take effect.' >&2; fi; "
        "exit \"$rc\""
    )


def cmd_set_account_lockout_policy(deny=None, unlock_time=None) -> str:
    raw = {"deny": deny, "unlock_time": unlock_time}
    settings = {k: int(v) for k, v in raw.items() if v is not None}
    if not settings:
        raise ValueError("Specify at least one of deny / unlock_time")
    # faillock.conf only takes effect where pam_faillock is actually wired into the
    # PAM stack. RHEL 8+/Fedora own it through authselect, so enable the feature;
    # SUSE references it by default. Debian/Ubuntu (pam_faillock not in common-auth
    # by default) and RHEL7/Amazon Linux 2 (pam_tally2) need a manual PAM edit -
    # warn instead of silently reporting success on a policy that isn't in force.
    return (
        _set_security_conf_keys("/etc/security/faillock.conf", settings)
        + "; rc=$?; if command -v authselect >/dev/null 2>&1 && authselect current >/dev/null 2>&1; then "
        "authselect enable-feature with-faillock 2>&1 || true; fi; "
        "echo 'faillock.conf updated. Confirm /etc/pam.d/system-auth (or common-auth) references "
        "pam_faillock on this host - on Debian/Ubuntu and RHEL7/Amazon Linux 2 it may need adding manually.'; "
        "exit \"$rc\""
    )


def cmd_set_umask_policy(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-7]{3,4}", value):
        raise ValueError("Umask must be an octal value like 027 or 0027")
    return _set_security_conf_keys("/etc/login.defs", {"UMASK": value}, sep=" ")


def cmd_set_sudo_policy(timestamp_timeout=None, require_password=None, group: str = "") -> str:
    # The sudo-granting group differs by distro: 'sudo' on Debian/Ubuntu, 'wheel'
    # on RHEL/Fedora/SUSE. A hard-coded "%sudo ..." line passes `visudo -c` but is
    # silently ineffective on SUSE/RHEL, where no 'sudo' group exists — no admin is
    # in it, so the require-password / NOPASSWD rule never applies. So when the
    # caller doesn't name a group (the web form sends ""), detect it at runtime the
    # same way cmd_set_sudo does, and build the %group line from the detected value
    # rather than baking a literal into the sudoers heredoc.
    group = (group or "").strip()
    if group and not all(c.isalnum() or c in "_-" for c in group):
        raise ValueError("Sudo group may only contain letters, numbers, dashes, and underscores.")

    static_lines = []
    if timestamp_timeout is not None:
        static_lines.append(f"Defaults timestamp_timeout={int(timestamp_timeout)}")
    need_group_rule = require_password is not None
    if not static_lines and not need_group_rule:
        raise ValueError("Specify at least one of timestamp_timeout / require_password")

    tmp = "/tmp/.sysible_sudoers_policy"
    dest = "/etc/sudoers.d/sysible-policy"
    quoted_tmp = shlex.quote(tmp)
    quoted_dest = shlex.quote(dest)

    # Runtime group detection + existence gate, only when we're writing a %group rule.
    prefix = ""
    append_rule = ""
    if need_group_rule:
        if group:
            prefix = f"grp={shlex.quote(group)}; "
        else:
            prefix = "if command -v apt-get >/dev/null 2>&1; then grp=sudo; else grp=wheel; fi; "
        prefix += (
            'if ! getent group "$grp" >/dev/null 2>&1; then '
            "echo \"sudo group '$grp' does not exist on this host.\" >&2; exit 1; fi; "
        )
        # `rule` is a fixed, shell-safe literal; only $grp is a shell variable, and
        # it's a validated/detected group name. Appended after the heredoc so the
        # detected group lands in the file.
        rule = "ALL=(ALL:ALL) ALL" if require_password else "ALL=(ALL:ALL) NOPASSWD: ALL"
        append_rule = f'&& echo "%$grp {rule}" >> {quoted_tmp} '

    # Static Defaults line(s) go in the heredoc; the %group rule (if any) is
    # appended at runtime above. The heredoc is wrapped in a { ...; } group so the
    # &&-chain can gate on the cat succeeding (a bare "&&" after the heredoc
    # terminator is a syntax error).
    body = ("\n".join(static_lines) + "\n") if static_lines else ""
    return (
        prefix +
        f"{{ cat > {quoted_tmp} <<'SYSIBLE_EOF'\n{body}SYSIBLE_EOF\n"
        f"}} {append_rule}&& chmod 440 {quoted_tmp} "
        f"&& visudo -c -f {quoted_tmp} "
        f"&& mv {quoted_tmp} {quoted_dest} "
        f"&& chown root:root {quoted_dest} "
        f"&& chmod 440 {quoted_dest}"
    )
