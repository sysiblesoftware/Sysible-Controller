"""Where the SLOP checkout is, for the tests that check this repo against it.

Three tests each hardcoded their own answer, and two of them got it wrong:

    test_controller_drives_baseline     /home/user/sysible-linux-operations-platform
    test_ctl_backs_up_every_volume      <parent>/slop/docker-compose.yml
    test_a_failing_request_...          /home/user/slop/gateway/Caddyfile

The checkout is named `sysible-linux-operations-platform`, so the two that
looked for `slop` skipped with "the SLOP checkout is not beside this one" on a
machine where it was sitting right beside them. Those two are cross-repo checks
-- sysiblectl's fallback compose against the real one, and the gateway's
Caddyfile -- which is the surface most likely to drift and the least likely to
be noticed drifting, because the skip reads like a fact about the machine.

Resolved in one place, by looking. SYSIBLE_SLOP_DIR wins when it is set.
"""
import os
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[1]

_CANDIDATES = (
    _REPO.parent / "sysible-linux-operations-platform",
    _REPO.parent / "slop",
    pathlib.Path("/home/user/sysible-linux-operations-platform"),
    pathlib.Path("/home/user/slop"),
    pathlib.Path("/opt/sysible-src/sysible-linux-operations-platform"),
)


def slop_dir():
    """The SLOP checkout, or None when there isn't one on this machine.

    Returns None rather than raising: whether a missing checkout is a skip or a
    failure is the test's call.
    """
    env = (os.environ.get("SYSIBLE_SLOP_DIR") or "").strip()
    if env:
        p = pathlib.Path(env)
        return p if p.is_dir() else None
    for cand in _CANDIDATES:
        # docker-compose.yml marks the real root, not just a directory that
        # happens to carry the name.
        if (cand / "docker-compose.yml").is_file():
            return cand
    return None


def slop_file(*relative):
    """A path inside the SLOP checkout, or None if either it or the file is absent."""
    root = slop_dir()
    if root is None:
        return None
    p = root.joinpath(*relative)
    return p if p.exists() else None
