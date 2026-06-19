from backend.utils.command_executor import run_command


def create_user(username, shell="/bin/bash"):
    return run_command(["useradd", "-m", "-s", shell, username])


def delete_user(username):
    return run_command(["userdel", "-r", username])


def set_password(username, password):
    return run_command(["chpasswd"], input_text=f"{username}:{password}")


def lock_user(username):
    return run_command(["usermod", "-L", username])


def unlock_user(username):
    return run_command(["usermod", "-U", username])


def get_groups(username):
    result = run_command(["id", "-nG", username])
    return result["stdout"].strip().split()


def toggle_sudo(username):
    groups = get_groups(username)

    if "sudo" in groups:
        run_command(["deluser", username, "sudo"])
        return False

    run_command(["usermod", "-aG", "sudo", username])
    return True


def is_locked(username):
    """`passwd -S <user>` prefixes the status field with L when locked,
    P when it has a usable password, and NP when there is none."""

    result = run_command(["passwd", "-S", username])
    fields = result["stdout"].split()

    return len(fields) > 1 and fields[1].startswith("L")
