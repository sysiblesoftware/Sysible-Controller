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

# Beside this repo, ABOVE it, and the conventional absolute locations. Above
# matters because this repo is also vendored INTO SLOP, at <slop>/apps/
# controller -- and in that tree every check that looked only beside itself
# found apps/ and gave up. That is the tree SLOP builds and ships from, so the
# cross-app checks were skipping in exactly the place they count.
# BESIDE this repo, or ABOVE it. Both are relationships to this checkout.
#
# Above matters because this repo is also vendored INTO SLOP, at
# <slop>/apps/controller -- and in that tree every check that looked only
# beside itself found apps/ and gave up. That is the tree SLOP builds and ships
# from, so the cross-app checks were skipping in exactly the place they count.
#
# What is NOT here is a list of absolute guesses. /home/user/sysible-linux-
# operations-platform was one of the hardcodes this module replaced, and
# keeping it as a fallback brings the bug back in a quieter form: a second
# checkout anywhere on a machine that also has one at /home/user would compare
# itself against THAT tree and report on a directory it has no relationship
# to. The installer's own layout needs no special case -- it puts both
# checkouts side by side, which the first rule already covers.
_CANDIDATES = tuple(
    [_REPO.parent / "sysible-linux-operations-platform",
     _REPO.parent / "slop"]
    + list(_REPO.parents)
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
        # What marks the real root is its CONTENT, not its name: an ancestor
        # walk passes through directories that carry neither.
        if (cand / "docker-compose.yml").is_file() \
                and (cand / "flashback" / "backend" / "store.py").is_file():
            return cand
    return None


def slop_file(*relative):
    """A path inside the SLOP checkout, or None if either it or the file is absent."""
    root = slop_dir()
    if root is None:
        return None
    p = root.joinpath(*relative)
    return p if p.exists() else None
