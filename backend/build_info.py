"""Deployment self-awareness for the controller.

Surfaces WHERE the running controller's code actually lives and WHAT commit it is,
so an operator can never again silently update one checkout while the service runs
another (the classic "I git-pulled but nothing changed" trap — the pull was in a
different directory than the one systemd loads).

Two signals:
  * `running_dir` / `commit` / `branch` — the code the live process is executing,
    read from the package's own location (not a guess);
  * `restart_needed` — the on-disk git HEAD has moved since this process started,
    i.e. someone pulled in THIS directory but hasn't restarted to load it.

Exposed at GET /version and shown in the web console's Controller Configuration.
"""
import os
import subprocess
from pathlib import Path


def running_dir() -> Path:
    """The repo/install root the live backend package is imported from — the
    authoritative answer to 'which directory is actually running'."""
    return Path(__file__).resolve().parent.parent


def _git(*args, cwd=None):
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or running_dir()), *args],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _head_commit(cwd=None):
    commit = _git("rev-parse", "HEAD", cwd=cwd)
    if commit:
        return commit
    # Fallback: read .git/HEAD manually if the git binary isn't available.
    try:
        gitdir = Path(cwd or running_dir()) / ".git"
        head = (gitdir / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (gitdir / ref).read_text().strip()
        return head or None
    except Exception:
        return None


# Commit at process start — compared against the live HEAD to detect "pulled but
# not restarted". Captured once, at import (i.e. process start).
_STARTED_COMMIT = _head_commit()


def info() -> dict:
    rd = running_dir()
    current = _head_commit()
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    return {
        "running_dir": str(rd),
        "commit": current,
        "commit_short": current[:12] if current else None,
        "branch": branch,
        "dirty": dirty,
        # Code on disk moved since we started → the operator pulled here but hasn't
        # restarted the service, so the running code is stale.
        "restart_needed": bool(_STARTED_COMMIT and current and current != _STARTED_COMMIT),
        "pid": os.getpid(),
    }


def log_startup():
    """One clear line at boot so `journalctl -u sysible-backend` always shows which
    directory + commit is actually running — the fastest way to catch a wrong-dir
    deploy."""
    i = info()
    print(f"[startup] Sysible controller running from {i['running_dir']} "
          f"@ {i['commit_short'] or 'unknown'} ({i['branch'] or 'no-git'})"
          f"{' [uncommitted changes]' if i['dirty'] else ''}")
