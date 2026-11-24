"""The patch probe is read out of human-facing output, so it has to pin a locale
and must not go to the mirrors on the path that promises not to.

Reported as "host patch scanning seems slow and inaccurate especially with suse
hosts". Two causes, one of them SUSE's specifically.

SLOW. The probe has a fast path, documented as using "each manager's
already-downloaded metadata". It never passed --no-refresh, and zypper
refreshes a stale repo on its own before answering a query -- so the branch
that promised not to touch the network went to the mirrors anyway. On the live
path, which runs an explicit `zypper refresh` first, the metadata was then
fetched a SECOND time. SUSE being the slow hosts, and the ones that timed out,
follows directly.

INACCURATE. Every count is grep/awk over English words -- dnf's
Obsoleting/Last/Security/Loaded/Dependencies, zypper's "needed", apt's
"security". Package managers translate those. On a host whose locale is not
English the filters stop matching and it reports zero updates: not an error, a
clean bill of health it never earned, on the screen whose job is to say whether
the fleet is patched.

The shell is EXECUTED against fake package managers. Reading it cannot tell you
whether an awk filter counts the right lines.
"""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from client import _api_dispatch as D   # noqa: E402

SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(not SH, reason="no sh on this machine")

ZYPPER_UPDATES = """\
S | Repository       | Name        | Current Version | Available Version | Arch
--+------------------+-------------+-----------------+-------------------+-------
v | Update-Oss       | bash        | 5.1.16-1.1      | 5.1.16-2.1        | x86_64
v | Update-Oss       | glibc       | 2.38-1.1        | 2.38-3.1          | x86_64
"""
ZYPPER_PATCHES = """\
Repository | Name            | Category | Severity  | Interactive | Status | Summary
-----------+-----------------+----------+-----------+-------------+--------+--------
Update-Oss | SUSE-SLE-15-001 | security | important | ---         | needed | fix
"""


# The shell utilities the probe itself needs. Everything else -- above all any
# real package manager on the machine running these tests -- must be OUT of the
# way, or the "no package manager" case finds this container's own apt-get and
# the fake host is not the host.
_UTILS = ("grep", "awk", "sed", "cat", "printf", "cut", "tr", "head", "tail", "env")


def _host(tmp_path, **progs):
    """An ISOLATED PATH: the shell utilities, plus only the package managers
    this fake host is meant to have."""
    b = tmp_path / "bin"
    b.mkdir(parents=True, exist_ok=True)
    for u in _UTILS:
        real = shutil.which(u)
        if real and not (b / u).exists():
            (b / u).symlink_to(real)
    for name, body in progs.items():
        f = b / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)
    return b


def _probe(bin_dir, refresh=False):
    cmd = D.cmd_update_status(refresh=refresh)
    env = {"PATH": str(bin_dir), "HOME": str(bin_dir)}
    p = subprocess.run([SH, "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    line = next((l for l in p.stdout.splitlines() if l.startswith("SYSUPDATES|")), "")
    return dict(kv.split("=", 1) for kv in line.split("|")[1:]) if line else {}


def _zypper(tmp_path, record=None):
    rec = f'echo "$@" >> {record}\n' if record else ""
    return _host(tmp_path, zypper=(
        rec +
        'for a in "$@"; do\n'
        '  case "$a" in list-updates) cat <<"EOF"\n' + ZYPPER_UPDATES + 'EOF\n'
        '    exit 0;; list-patches) cat <<"EOF"\n' + ZYPPER_PATCHES + 'EOF\n'
        '    exit 0;; needs-rebooting) exit 0;; esac\n'
        'done\nexit 0\n'))


def test_a_suse_host_is_counted_correctly(tmp_path):
    got = _probe(_zypper(tmp_path))
    assert got.get("mgr") == "zypper", got
    assert got.get("total") == "2", got
    assert got.get("security") == "1", got


def test_the_fast_path_does_not_let_zypper_go_to_the_mirrors(tmp_path):
    """The whole reason it is called fast."""
    rec = tmp_path / "calls"
    _probe(_zypper(tmp_path, record=rec))
    calls = rec.read_text().splitlines()
    reads = [c for c in calls if "list-updates" in c or "list-patches" in c]
    assert reads, "zypper was never asked for updates"
    for c in reads:
        assert "--no-refresh" in c, f"this query may refresh stale repos: {c}"
    assert not [c for c in calls if c.strip().endswith("refresh")], \
        "the fast path ran an explicit refresh"


def test_the_live_path_refreshes_once_not_twice(tmp_path):
    """It refreshes deliberately, then must not have each query refresh again."""
    rec = tmp_path / "calls"
    _probe(_zypper(tmp_path, record=rec), refresh=True)
    calls = rec.read_text().splitlines()
    explicit = [c for c in calls if c.strip().endswith("refresh") and "--no-refresh" not in c]
    assert len(explicit) == 1, f"refreshed {len(explicit)} times: {calls}"
    for c in calls:
        if "list-updates" in c or "list-patches" in c:
            assert "--no-refresh" in c, c


def test_a_non_english_host_does_not_report_a_clean_bill_of_health(tmp_path):
    """The fake answers in German unless the caller pinned a locale -- which is
    what a real package manager does. Without LC_ALL the counts come back 0 and
    the host reads as fully patched."""
    b = _host(tmp_path, zypper=(
        'if [ "$LC_ALL" = "C" ]; then\n'
        '  for a in "$@"; do case "$a" in\n'
        '    list-updates) cat <<"EOF"\n' + ZYPPER_UPDATES + 'EOF\n'
        '      exit 0;; list-patches) cat <<"EOF"\n' + ZYPPER_PATCHES + 'EOF\n'
        '      exit 0;; esac; done\n'
        'else\n'
        '  for a in "$@"; do case "$a" in\n'
        '    list-updates) printf "S | Quelle | Name | Version\\n--+--------+------+--------\\n'
        'v | Update | bash | 5.1\\nv | Update | glibc | 2.38\\n"; exit 0;;\n'
        '    list-patches) printf "Quelle | Name | Status\\n-------+------+-------\\n'
        'Update | SLE-1 | erforderlich\\n"; exit 0;; esac; done\n'
        'fi\nexit 0\n'))
    got = _probe(b)
    assert got.get("total") == "2", f"package updates miscounted on a de_DE host: {got}"
    assert got.get("security") == "1", f"security patches miscounted on a de_DE host: {got}"


def test_a_host_with_no_package_manager_says_unknown_not_zero(tmp_path):
    """mgr=unknown is what lets the console tell "nothing to install" from
    "nobody could look"."""
    got = _probe(_host(tmp_path))
    assert got.get("mgr") == "unknown", got


def test_an_apt_host_still_counts(tmp_path):
    """The locale pin must not break the branches that already worked."""
    b = _host(tmp_path, **{"apt-get": (
        'case "$*" in *upgrade*) printf "Inst bash [5.1] (5.2 Ubuntu:security)\\n'
        'Inst tar [1.3] (1.4 Ubuntu:noble)\\n"; exit 0;; esac\nexit 0\n')})
    got = _probe(b)
    assert got.get("mgr") == "apt", got
    assert got.get("total") == "2", got
    assert got.get("security") == "1", got
