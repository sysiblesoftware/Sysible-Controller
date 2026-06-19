import time

import psutil
import pwd
from fastapi import APIRouter, HTTPException

from backend.models.user_models import CreateUserRequest, SetPasswordRequest
from backend.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


def _process_avg_cpu_percent(proc):
    """Average CPU% over the process's lifetime - avoids the "always
    reads 0.0" trap of psutil's interval-based cpu_percent() when each
    request builds fresh Process objects instead of re-using one."""

    try:
        times = proc.cpu_times()
        elapsed = time.time() - proc.create_time()

        if elapsed <= 0:
            return 0.0

        return round(((times.user + times.system) / elapsed) * 100, 1)

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return 0.0


# ---------------- LIST USERS ----------------
@router.get("/")
def list_users():
    users = []

    for u in pwd.getpwall():
        if u.pw_uid < 1000:
            continue

        users.append({
            "username": u.pw_name,
            "uid": u.pw_uid,
            "gid": u.pw_gid,
            "home": u.pw_dir,
            "shell": u.pw_shell
        })

    return users


# ---------------- USER DETAILS ----------------
@router.get("/{username}")
def user_details(username: str):
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such user")

    groups = user_service.get_groups(username)

    return {
        "username": pw.pw_name,
        "uid": pw.pw_uid,
        "gid": pw.pw_gid,
        "home": pw.pw_dir,
        "shell": pw.pw_shell,
        "groups": groups,
        "sudo": "sudo" in groups,
        "locked": user_service.is_locked(username)
    }


# ---------------- SESSIONS / METRICS ----------------
@router.get("/{username}/sessions")
def user_sessions(username: str):
    sessions = [
        {
            "type": "terminal",
            "session_id": s.terminal or "-",
            "tty": s.terminal or "-",
            "host": s.host,
            "started": s.started
        }
        for s in psutil.users()
        if s.name == username
    ]

    cpu_total = 0.0
    mem_total = 0.0
    process_count = 0

    for proc in psutil.process_iter(["username", "memory_percent"]):
        try:
            if proc.info["username"] != username:
                continue

            process_count += 1
            mem_total += proc.info["memory_percent"] or 0.0
            cpu_total += _process_avg_cpu_percent(proc)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "sessions": sessions,
        "metrics": {
            "cpu": round(cpu_total, 1),
            "memory": round(mem_total, 1),
            "processes": process_count
        }
    }


# ---------------- CREATE USER ----------------
@router.post("/")
def create_user(user: CreateUserRequest):
    result = user_service.create_user(user.username, user.shell)

    if result["returncode"] != 0:
        raise HTTPException(
            status_code=400,
            detail=result["stderr"] or "useradd failed"
        )

    if user.password:
        user_service.set_password(user.username, user.password)

    return {"ok": True}


# ---------------- PASSWORD ----------------
@router.post("/{username}/password")
def set_password(username: str, body: SetPasswordRequest):
    result = user_service.set_password(username, body.password)

    return {"ok": result["returncode"] == 0}


# ---------------- LOCK / UNLOCK ----------------
@router.post("/{username}/lock")
def lock_user(username: str):
    result = user_service.lock_user(username)

    return {"ok": result["returncode"] == 0, "locked": True}


@router.post("/{username}/unlock")
def unlock_user(username: str):
    result = user_service.unlock_user(username)

    return {"ok": result["returncode"] == 0, "locked": False}


# ---------------- TOGGLE SUDO ----------------
@router.post("/{username}/sudo/toggle")
def toggle_sudo(username: str):
    return {"sudo": user_service.toggle_sudo(username)}


# ---------------- DELETE USER ----------------
@router.delete("/{username}")
def delete_user(username: str):
    result = user_service.delete_user(username)

    return {"ok": result["returncode"] == 0, "stderr": result["stderr"]}
