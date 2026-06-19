from fastapi import APIRouter
import subprocess
import grp

router = APIRouter(prefix="/groups", tags=["groups"])


# ---------------- LIST GROUPS ----------------
@router.get("/")
def list_groups():
    groups = []

    for g in grp.getgrall():
        groups.append({
            "name": g.gr_name,
            "gid": g.gr_gid,
            "members": g.gr_mem
        })

    return groups


# ---------------- CREATE GROUP ----------------
@router.post("/")
def create_group(body: dict):

    name = body.get("name")

    subprocess.run(["groupadd", name])

    return {"ok": True}


# ---------------- DELETE GROUP ----------------
@router.delete("/{group}")
def delete_group(group: str):

    proc = subprocess.run(
        ["groupdel", group],
        capture_output=True,
        text=True
    )

    return {"ok": proc.returncode == 0, "stderr": proc.stderr}


# ---------------- ADD USER TO GROUP ----------------
@router.post("/{group}/users/{username}")
def add_user(group: str, username: str):

    subprocess.run(["usermod", "-aG", group, username])

    return {"ok": True}


# ---------------- REMOVE USER FROM GROUP ----------------
@router.delete("/{group}/users/{username}")
def remove_user(group: str, username: str):

    # safe remove using gpasswd
    subprocess.run(["gpasswd", "-d", username, group])

    return {"ok": True}
