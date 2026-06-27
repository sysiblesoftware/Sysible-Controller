"""
Sysible host agent.

Runs on a managed host, enrolls itself with the controller using a
one-time token (generated in the GUI's Host Enrollment page), then
polls for queued commands and reports results back.

Configuration (env vars, all optional except the token on first run):
  SYSIBLE_CONTROLLER       Base URL of the controller, or a comma-separated list of
                           candidate URLs to fail over between - e.g. when the
                           controller's "All Detected IPs (failover)" address mode
                           bundled every address it found on itself - tried in order
                           until one connects (default https://127.0.0.1:9000)
  SYSIBLE_ENROLL_TOKEN     One-time enrollment token (required only the first time)
  SYSIBLE_AGENT_STATE      Where to persist host_id/agent_secret (default /var/lib/sysible/agent_state.json)
  SYSIBLE_POLL_INTERVAL    Seconds between heartbeats/command polls when idle (default 1.5) -
                           a queued command is picked up on the very next poll, so this is
                           the main knob on "how long after I click Run does the agent notice"
  SYSIBLE_CA_CERT          Path to the controller's TLS cert, copied from its
                           $BASE/certs/server.crt, for pinned verification
                           (default /etc/sysible/controller.crt)

The token may also be passed as the first CLI argument, e.g.:
  python3 agent.py <token>
"""

import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import time
import traceback
import uuid

import requests

# Cap on stdout/stderr bytes kept from a single command - a runaway
# command (e.g. `cat` on a huge file, a noisy build log) shouldn't be
# able to balloon this process's memory or the JSON payload sent back
# to the controller. Output is truncated, not the command's actual
# execution - capture_output still has to buffer it all in memory
# either way, but this bounds what we hold onto and ship afterward.
MAX_OUTPUT_BYTES = 200_000

# May be a single URL or a comma-separated list of candidate URLs (see
# the module docstring above) - CONTROLLER itself stays mutable after
# this point, since _request() below switches it to whichever candidate
# most recently answered, so the startup print and the TLS check just
# below always reflect "the one that's currently working" rather than
# frozen at whatever was first in the list.
_CONTROLLER_CANDIDATES = [
    c.strip() for c in os.getenv("SYSIBLE_CONTROLLER", "https://127.0.0.1:9000").split(",") if c.strip()
] or ["https://127.0.0.1:9000"]
CONTROLLER = _CONTROLLER_CANDIDATES[0]
STATE_FILE = os.getenv("SYSIBLE_AGENT_STATE", "/var/lib/sysible/agent_state.json")

# Was 5s - that meant a freshly queued command could sit for up to 5
# full seconds before this agent even noticed it, on top of however
# long the command itself takes and the GUI's own poll interval on the
# way back. 1.5s matches the GUI's AGENT_CMD_POLL_MS (remote_administration_page.py)
# so neither side is the bottleneck. loop() below also skips this
# sleep entirely right after handling a task, so a burst of several
# queued commands (e.g. System Health & Logs running a few checks
# back to back) doesn't pay this delay between each one either.
POLL_INTERVAL = float(os.getenv("SYSIBLE_POLL_INTERVAL", "1.5"))

# =========================================================
# TLS
# The controller's cert is self-signed (LAN-only, no public domain),
# so verification means pinning that specific cert rather than
# trusting any CA - or, worse, disabling verification entirely. Copy
# the controller's $BASE/certs/server.crt to this host once (e.g. via
# scp, the same one-time step as distributing the enrollment token)
# and either leave it at the default path below or point
# SYSIBLE_CA_CERT at wherever it landed.
# =========================================================
_CA_CERT_FILE = os.getenv("SYSIBLE_CA_CERT", "/etc/sysible/controller.crt")

if CONTROLLER.startswith("https://") and os.path.exists(_CA_CERT_FILE):
    _VERIFY = _CA_CERT_FILE
elif CONTROLLER.startswith("https://"):
    print(
        f"[agent] warning: no pinned CA cert found at {_CA_CERT_FILE} - "
        "copy the controller's certs/server.crt here or set SYSIBLE_CA_CERT. "
        "TLS verification will likely fail until then."
    )
    _VERIFY = True
else:
    _VERIFY = True

SESSION = requests.Session()
SESSION.verify = _VERIFY


def _request(method, path, **kwargs):
    """All controller calls below go through this instead of
    SESSION.<verb> directly. Tries the current CONTROLLER first; on a
    connection failure (refused/unreachable/DNS - i.e. nothing answered
    at all) rotates through the rest of _CONTROLLER_CANDIDATES until one
    responds. Whichever candidate succeeds becomes the new CONTROLLER,
    so the next call tries it first instead of re-walking the whole
    list every time - once one NIC/IP proves reachable it's very likely
    to stay that way.

    Deliberately does NOT fail over on an ordinary HTTP error response
    (404, 500, etc.) - only on requests.ConnectionError/Timeout. An HTTP
    error means the controller WAS reached, just didn't like the
    request, and trying a different IP for the exact same controller
    process would only get the same answer. Re-raises the last
    connection error if every candidate fails, same as a plain
    SESSION.<verb> call would raise on the one URL it knew about
    before - callers' existing `except requests.RequestException`
    handling around fetch_tasks/send_result/heartbeat needs no changes."""
    global CONTROLLER

    candidates = _CONTROLLER_CANDIDATES
    start = candidates.index(CONTROLLER) if CONTROLLER in candidates else 0

    last_exc = None
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        try:
            r = SESSION.request(method, f"{candidate}{path}", **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            continue

        if candidate != CONTROLLER:
            print(f"[agent] switched to controller candidate: {candidate}")
            CONTROLLER = candidate

        return r

    raise last_exc


def _local_ip():
    """Best-effort local (LAN-facing) IP for this host, shown in the
    Address column of Remote Administration instead of the opaque
    host_id. Opens a UDP socket "connected" to an arbitrary external
    address and reads back the outbound interface IP - no packets are
    actually sent (UDP connect() just picks a route), so this works
    without internet access and without parsing ifconfig/ip output,
    which varies a lot across platforms. Falls back to "" (not None,
    so it's at least JSON-serializable) if nothing usable comes back -
    e.g. no network interfaces are up at all."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""
    finally:
        s.close()


class UnknownHostError(Exception):
    """Raised when the controller responds 404 "Unknown host_id" - this
    agent's enrollment no longer exists on the controller (disenrolled
    via the GUI, or the controller's database was reset/recreated)
    even though this host still has a cached, now-stale state file.

    Deliberately NOT a requests.RequestException subclass: heartbeat(),
    fetch_tasks(), and send_result() below each catch
    `requests.RequestException` broadly (for ordinary network blips) and
    swallow it with just a printed warning - if this were one of those,
    "unknown host" would loop silently forever. Keeping it a plain
    Exception lets it fall through those catches untouched and surface
    all the way up to loop(), which is the only place that should react
    to it."""


# =========================================================
# STATE
# =========================================================
def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def clear_state():
    """Wipe the cached host_id/agent_secret so the next run looks like
    a fresh install and goes through register() again instead of
    reusing a state the controller no longer recognizes. Note this
    does NOT get the agent re-enrolled by itself: register() still
    needs a *fresh* SYSIBLE_ENROLL_TOKEN, since the one baked into this
    host's original bundle was already consumed on first enrollment
    and the controller will reject it a second time."""
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def get_enroll_token():
    if len(sys.argv) > 1:
        return sys.argv[1]

    return os.getenv("SYSIBLE_ENROLL_TOKEN")


# =========================================================
# REGISTER
# =========================================================
def register():
    token = get_enroll_token()

    if not token:
        print(
            "[agent] no enrollment token found - set SYSIBLE_ENROLL_TOKEN "
            "or pass it as the first argument"
        )
        sys.exit(1)

    state = load_state() or {}
    host_id = state.get("host_id") or str(uuid.uuid4())

    payload = {
        "token": token,
        "host_id": host_id,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "kernel": platform.release(),
        "ip": _local_ip(),
    }

    r = _request("POST", "/agents/enroll", json=payload, timeout=15)
    _raise_with_detail(r)
    data = r.json()

    state = {
        "host_id": data["host_id"],
        "agent_secret": data["agent_secret"],
    }

    save_state(state)

    print("[agent] enrolled:", state["host_id"])

    return state


# =========================================================
# COMMANDS
# =========================================================
def _raise_with_detail(r):
    """r.raise_for_status() alone only ever says e.g. "404 Client Error:
    Not Found for url: ..." - it throws away the FastAPI {"detail": ...}
    body, which is exactly what distinguishes "this host_id was never
    enrolled" (controller is up, just doesn't know this agent) from "the
    route doesn't exist" (stale controller code) or a bad agent_secret.
    Surface it instead of leaving that to guesswork."""
    if r.ok:
        return

    detail = None
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        pass

    if r.status_code == 404 and detail == "Unknown host_id":
        raise UnknownHostError(detail)

    raise requests.exceptions.HTTPError(
        f"{r.status_code} {detail or r.reason}", response=r
    )


def fetch_tasks(state):
    try:
        # Send the secret in a header, not the query string, so it can't land
        # in access/proxy logs. (The controller still accepts the legacy query
        # param for older agents.)
        r = _request(
            "GET",
            f"/agents/{state['host_id']}/tasks",
            headers={"X-Agent-Secret": state["agent_secret"]},
            timeout=10,
        )
        _raise_with_detail(r)
        return r.json().get("tasks", [])
    except requests.RequestException as e:
        print("[agent] could not fetch tasks:", e)
        return []


def _truncate(s):
    if s is None or len(s) <= MAX_OUTPUT_BYTES:
        return s

    return s[:MAX_OUTPUT_BYTES] + f"\n...[truncated, {len(s) - MAX_OUTPUT_BYTES} more bytes]"


_PRIV_ERROR_HINTS = (
    "permission denied", "operation not permitted", "must be root",
    "must be run as root", "are not allowed", "not permitted", "only root",
    "you need to be root", "eperm", "eacces",
    "a password is required", "a terminal is required", "sudo:", "root privileges",
    "access denied", "not authorized", "requires root",
    # polkit / D-Bus (systemctl, hostnamectl, timedatectl, etc. run as a
    # non-root user answer this instead of a plain permission error):
    "interactive authentication required", "authentication is required",
    "authentication required", "not privileged", "rejected send message",
)


def _looks_like_privilege_error(stderr):
    s = (stderr or "").lower()
    return any(h in s for h in _PRIV_ERROR_HINTS)


def _local_user_exists(user):
    try:
        import pwd
        pwd.getpwnam(user)
        return True
    except (KeyError, ImportError):
        return False


def _exec(argv, shell=False, input_data=None):
    try:
        proc = subprocess.run(argv, shell=shell, capture_output=True, text=True,
                              timeout=300, input=input_data)
        return {
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "command timed out", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


def _run_as_user(user, cmd, become_password=None):
    """RBAC: run `cmd` as local user `user`. Tried as that user first, so
    read-only commands work even for a user with no sudo; on a privilege
    error it's retried under that user's own sudo. Elevation uses `sudo -n`
    (passwordless) unless a `become_password` was supplied for this task, in
    which case it uses `sudo -S` and feeds the password on STDIN (never on
    argv/env), for hosts that forbid NOPASSWD. runuser needs root; if the
    agent isn't root it prefixes its own sudo."""
    if not _local_user_exists(user):
        return {
            "stdout": "",
            "stderr": (f"RBAC: local user '{user}' does not exist on this host, so the "
                       f"command cannot be run as that role. Create the user (with the "
                       f"sudo policy you want) on this host."),
            "returncode": 126,
        }

    root = os.geteuid() == 0
    plain = (["runuser", "-u", user, "--", "bash", "-c", cmd] if root
             else ["sudo", "-n", "runuser", "-u", user, "--", "bash", "-c", cmd])
    first = _exec(plain)
    # Look for the privilege error in BOTH streams: some commands redirect
    # their stderr into stdout (e.g. `... 2>&1`), which would otherwise hide a
    # "Permission denied" from this check and stop us from escalating.
    combined = (first["stderr"] or "") + "\n" + (first["stdout"] or "")
    if first["returncode"] == 0 or not _looks_like_privilege_error(combined):
        return first

    # Escalate. With a become-password use `sudo -S` (read password from
    # stdin, empty prompt); otherwise `sudo -n` (passwordless).
    if become_password:
        inner = ["sudo", "-S", "-p", "", "bash", "-c", cmd]
        stdin = become_password + "\n"
    else:
        inner = ["sudo", "-n", "bash", "-c", cmd]
        stdin = None
    elevated = (["runuser", "-u", user, "--"] + inner if root
                else ["sudo", "-n", "runuser", "-u", user, "--"] + inner)
    res = _exec(elevated, input_data=stdin)

    if res["returncode"] != 0:
        low = (res["stderr"] or "").lower()
        if become_password and ("try again" in low or "incorrect password" in low
                                or "sorry" in low):
            res["stderr"] = (res["stderr"].rstrip()
                             + f"\n[sysible] sudo rejected the password for '{user}' on this host.")
        elif not become_password and (
                "password is required" in low or "a terminal is required" in low
                or "no tty present" in low or "not allowed to execute" in low
                or "not in the sudoers" in low):
            res["stderr"] = (res["stderr"].rstrip() + (
                f"\n[sysible] This action needs root, but '{user}' can't run it via "
                f"passwordless sudo here. Either grant '{user}' NOPASSWD sudo for it, or "
                f"mark this host as 'password sudo' so Sysible supplies your sudo password."))
    return res


def run_command(cmd, run_as=None, become_password=None):
    # RBAC path: a task tagged with an initiating admin username runs as the
    # matching local user, gated by that host's sudo policy (see
    # _run_as_user). Without run_as it's an internal/controller task: a root
    # agent runs it directly; an unprivileged agent escalates via sudo -n
    # (the pre-RBAC behaviour, unchanged).
    if run_as:
        return _run_as_user(run_as, cmd, become_password=become_password)
    if os.geteuid() != 0:
        cmd = "sudo -n bash -c " + shlex.quote(cmd)
    return _exec(cmd, shell=True)


def send_result(state, task_id, result):
    try:
        r = _request(
            "POST",
            f"/agents/{state['host_id']}/tasks/result",
            json={
                "host_id": state["host_id"],
                "agent_secret": state["agent_secret"],
                "task_id": task_id,
                "result": json.dumps(result),
            },
            timeout=10,
        )
        _raise_with_detail(r)
    except requests.RequestException as e:
        print("[agent] could not send result:", e)


# =========================================================
# LOOP
# =========================================================
def heartbeat(state):
    try:
        r = _request(
            "POST",
            "/agents/heartbeat",
            json={
                "host_id": state["host_id"],
                "agent_secret": state["agent_secret"],
                # Re-sent on every heartbeat, not just enroll, so a
                # DHCP-reassigned IP keeps the controller's Address
                # column accurate without needing a full re-enroll.
                "ip": _local_ip(),
                # Likewise re-read each heartbeat so a hostname change
                # (e.g. via Set Hostname) shows up in the inventory
                # without re-enrolling. gethostname() reflects the new
                # name immediately after hostnamectl set-hostname.
                "hostname": socket.gethostname(),
            },
            timeout=10,
        )
        _raise_with_detail(r)
    except requests.RequestException as e:
        print("[agent] heartbeat failed:", e)


def loop(state):
    controller_desc = (
        CONTROLLER
        if len(_CONTROLLER_CANDIDATES) == 1
        else f"{CONTROLLER} (+{len(_CONTROLLER_CANDIDATES) - 1} more candidate(s))"
    )
    print("[agent] running:", state["host_id"], "controller:", controller_desc)

    while True:
        ran_task = False

        try:
            heartbeat(state)

            tasks = fetch_tasks(state)
            ran_task = bool(tasks)

            for task in tasks:
                # Deliberately no command text here (and run_command's
                # result is never printed either) - only the task id.
                # The command itself can carry secrets (passwords, API
                # keys, tokens passed as args/env), and this print goes
                # to the agent's own stdout/log on the managed host,
                # which is a much wider-open place for that to leak
                # than the controller's already-authenticated DB.
                task_id = task.get("id")
                print("[agent] running task", task_id)

                try:
                    result = run_command(task["command"], task.get("run_as"),
                                         task.get("become_password"))
                    send_result(state, task_id, result)
                except UnknownHostError:
                    raise
                except Exception as e:
                    # One malformed/failing task (e.g. missing
                    # "command", a send_result network blip that
                    # somehow raised, etc.) must not take the whole
                    # agent process down - log it and keep polling.
                    print(f"[agent] task {task_id} failed: {e}")
        except UnknownHostError:
            print(
                f"[agent] controller no longer recognizes host_id {state['host_id']} "
                "- disenrolled, or the controller's database was reset/recreated. "
                "Clearing local state and exiting. Re-run this agent with a FRESH "
                "enrollment token (e.g. re-download the agent bundle) - the token "
                "this host enrolled with originally has already been used and "
                "won't be accepted again."
            )
            clear_state()
            sys.exit(1)
        except Exception:
            # Catch-all so an unexpected error - a heartbeat/fetch
            # hiccup under heavy load, a transient JSON/parsing error,
            # anything not already handled above - logs and retries on
            # the next poll instead of killing the agent process. A
            # "bogged down" host (high CPU/memory pressure, a flaky
            # network blip mid-request) is exactly when staying alive
            # matters most.
            traceback.print_exc()

        # Skip the idle delay entirely right after handling at least
        # one task - check again immediately in case another command
        # was queued in the meantime, instead of always waiting out a
        # fixed interval between every single task. Only an actually
        # idle cycle (nothing to do) pays POLL_INTERVAL - the network
        # round-trip of heartbeat()+fetch_tasks() itself still bounds
        # how tight this loop can spin either way.
        if not ran_task:
            time.sleep(POLL_INTERVAL)


# =========================================================
# MAIN
# =========================================================
def main():
    state = load_state()

    if not state or "agent_secret" not in state:
        state = register()

    loop(state)


if __name__ == "__main__":
    main()
