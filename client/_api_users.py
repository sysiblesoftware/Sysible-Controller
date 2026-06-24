"""Users/groups/remote-hosts/file-transfer/fleet-user-mgmt/password helpers - split out of client/api.py to keep individual file sizes manageable. Imported via `from client._api_users import *` at the bottom of client/api.py."""
import base64
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


def exec_remote(name: str, cmd: str, description: str = None):
    body = {"cmd": cmd}
    if description:
        body["description"] = description
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


def queue_agent_upload(host_id: str, local_path, remote_path: str):
    local_path = Path(local_path)
    size = local_path.stat().st_size
    if size > AGENT_FILE_TRANSFER_LIMIT_BYTES:
        return {"task_id": None, "error": (
            f"File is {size} bytes - agent-host uploads are limited to "
            f"{AGENT_FILE_TRANSFER_LIMIT_BYTES} bytes. SSH hosts have no such limit."
        )}
    script = _build_agent_upload_script(remote_path, local_path.name, local_path.read_bytes())
    cmd = _wrap_python_script(script)
    task_ids = queue_command_on_hosts([host_id], cmd, kind="upload_file")
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


def queue_agent_download(host_id: str, remote_path: str):
    script = _build_agent_download_script(remote_path)
    cmd = _wrap_python_script(script)
    task_ids = queue_command_on_hosts([host_id], cmd, kind="download_file")
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


def queue_command_on_hosts(host_ids, command: str, kind: str = "command", description: str = None):
    body_base = {"command": command, "kind": kind}
    if description:
        body_base["description"] = description
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
        out = subprocess.run(["id", "-nG", username], capture_output=True, text=True).stdout
        return out.split()
    except Exception:
        return []

def _locked(username):
    try:
        out = subprocess.run(["passwd", "-S", username], capture_output=True, text=True).stdout
        fields = out.split()
        return len(fields) > 1 and fields[1].startswith("L")
    except Exception:
        return False

def _sessions():
    sessions = []
    try:
        out = subprocess.run(["who"], capture_output=True, text=True).stdout
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
        "shell": u.pw_shell, "groups": groups, "sudo": "sudo" in groups, "locked": _locked(u.pw_name),
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


def _hash_password(password: str):
    try:
        import crypt
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except ImportError:
        return None


def cmd_create_user(username: str, password: str = "", shell: str = "/bin/bash") -> str:
    cmd = f"useradd -m -s {shlex.quote(shell or '/bin/bash')} {shlex.quote(username)}"
    if password:
        cmd += " && " + cmd_set_password(username, password)
    return cmd


def cmd_delete_user(username: str) -> str:
    return f"userdel -r {shlex.quote(username)}"


def cmd_lock_user(username: str) -> str:
    return f"usermod -L {shlex.quote(username)}"


def cmd_unlock_user(username: str) -> str:
    return f"usermod -U {shlex.quote(username)}"


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
            f'usermod -aG "$grp" {u} && echo "Granted sudo to {username} (added to group $grp)."'
        )
    return (
        f"{detect}; "
        f'gpasswd -d {u} "$grp" 2>&1 && echo "Revoked sudo from {username} (removed from group $grp)."'
    )


def cmd_set_password(username: str, password: str) -> str:
    hashed = _hash_password(password)
    if hashed is None:
        raise RuntimeError("Password hashing unavailable on this client (the `crypt` module requires a POSIX system) - refusing to queue a remote password change rather than send it as plaintext.")
    return f"usermod -p {shlex.quote(hashed)} {shlex.quote(username)}"


def cmd_create_group(name: str) -> str:
    return f"groupadd {shlex.quote(name)}"


def cmd_delete_group(name: str) -> str:
    return f"groupdel {shlex.quote(name)}"


def cmd_add_user_to_group(group: str, username: str) -> str:
    return f"usermod -aG {shlex.quote(group)} {shlex.quote(username)}"


def cmd_remove_user_from_group(group: str, username: str) -> str:
    return f"gpasswd -d {shlex.quote(username)} {shlex.quote(group)}"


def cmd_kill_user_sessions(username: str) -> str:
    user = shlex.quote(username)
    return (
        f"if command -v loginctl >/dev/null 2>&1; then loginctl terminate-user {user} 2>&1; "
        f"else pkill -KILL -u {user} 2>&1; fi; "
        f"echo 'Done (no error above means it worked, or there were no active sessions).'"
    )


def cmd_force_password_reset(username: str) -> str:
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
    return f"usermod -s {shlex.quote(shell)} {shlex.quote(username)}"


def cmd_set_user_comment(username: str, comment: str) -> str:
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
    return _set_security_conf_keys("/etc/security/pwquality.conf", settings)


def cmd_set_account_lockout_policy(deny=None, unlock_time=None) -> str:
    raw = {"deny": deny, "unlock_time": unlock_time}
    settings = {k: int(v) for k, v in raw.items() if v is not None}
    if not settings:
        raise ValueError("Specify at least one of deny / unlock_time")
    return _set_security_conf_keys("/etc/security/faillock.conf", settings)


def cmd_set_umask_policy(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-7]{3,4}", value):
        raise ValueError("Umask must be an octal value like 027 or 0027")
    return _set_security_conf_keys("/etc/login.defs", {"UMASK": value}, sep=" ")


def cmd_set_sudo_policy(timestamp_timeout=None, require_password=None, group: str = "sudo") -> str:
    lines = []
    if timestamp_timeout is not None:
        lines.append(f"Defaults timestamp_timeout={int(timestamp_timeout)}")
    if require_password is not None:
        rule = "ALL=(ALL:ALL) ALL" if require_password else "ALL=(ALL:ALL) NOPASSWD: ALL"
        lines.append(f"%{group} {rule}")
    if not lines:
        raise ValueError("Specify at least one of timestamp_timeout / require_password")
    body = "\n".join(lines) + "\n"
    tmp = "/tmp/.sysible_sudoers_policy"
    dest = "/etc/sudoers.d/sysible-policy"
    quoted_tmp = shlex.quote(tmp)
    quoted_dest = shlex.quote(dest)
    return (
        f"cat > {quoted_tmp} <<'SYSIBLE_EOF'\n{body}SYSIBLE_EOF\n"
        f"&& chmod 440 {quoted_tmp} "
        f"&& visudo -c -f {quoted_tmp} "
        f"&& mv {quoted_tmp} {quoted_dest} "
        f"&& chown root:root {quoted_dest} "
        f"&& chmod 440 {quoted_dest}"
    )
