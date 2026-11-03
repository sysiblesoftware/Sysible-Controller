"""Installing Salt on a managed host, proved by running the real script.

"Salt isn't installed on baseline" is not a bug in one message — it is a
product that requires a binary on every host it manages and offers no way to
put it there. Baseline writes a state file on the host and runs `salt-call
--local` on it; without salt-call, every apply on every host fails at once.

NOTHING HERE STRING-MATCHES THE COMMAND. Each test builds a PATH of fake
package managers, runs the actual script under a real shell, and checks what
happened on that fake host — which package was asked for, whether the daemon
was disabled, what the exit code and the message were. A script that reads
correctly and does not run is the failure mode this method exists to catch.

THE RULE THE SCRIPT FOLLOWS: the package name is a guess, `command -v
salt-call` is the answer. Which package ships salt-call differs by distro and
has moved between releases, so candidates are tried in turn and the CHECK
decides. A package manager that exits 0 having installed something that does
not provide the binary is a failure, and must be reported as one rather than as
a working host that fails on its next apply.
"""
import os
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from client._api_automation import cmd_install_salt  # noqa: E402
from client._pkgmgr import pkgmgr_detect_fragment  # noqa: E402


def _installs(log):
    """Just the package-manager invocations, as the package names asked for.

    The raw log also carries the systemctl line — which mentions salt-minion —
    and every entry is prefixed with the tmpdir path, which pytest names after
    the test. `test_dnf_is_preferred_over_yum0` contains the string "yum", so
    an assertion that simply searched the log for "yum" was reading the name of
    the test that was asserting it.
    """
    out = []
    for line in (log.read_text() or "").splitlines():
        if line.startswith("systemctl ") or " disable " in line:
            continue
        argv = line.split()
        if not argv:
            continue
        out.append(os.path.basename(argv[0]) + " " + " ".join(argv[1:]))
    return out


def _bin(d, name, body):
    """Put a fake executable on the host's PATH.

    The fake gets the REAL system PATH for its own use, because it needs
    printf and chmod to pretend to install something. That does not weaken the
    isolation of the script under test: this is a child process setting its own
    PATH, and the script above it still sees nothing but this directory.

    Without it the fakes failed silently — "chmod: not found" to a stream
    nobody reads — the package appeared not to install, and the script under
    test looked like it had a shell bug it did not have. Worth the two lines:
    a harness that lies costs more than the thing it is testing.
    """
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nPATH=/usr/bin:/bin:$PATH\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _host(tmp_path, mgr=None, *, provides=(), already=False, systemctl=True):
    """A fake host: a PATH with a package manager that installs `provides`.

    `provides` is the set of package names that, when installed, actually put
    salt-call on PATH. Anything else installs "successfully" and provides
    nothing — which is the real-world case the check exists for.
    """
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    d = str(d)
    log = tmp_path / "log"
    log.write_text("", encoding="utf-8")

    if already:
        _bin(d, "salt-call", 'echo "{\\"local\\": {}}"')

    if mgr:
        # Record every call, and create salt-call only for a package name that
        # really provides it on this fake host.
        checks = "\n".join(
            f'  if [ "$a" = "{pkg}" ]; then '
            f'printf \'#!/bin/sh\\necho "salt-call 3006"\\n\' > {d}/salt-call; '
            f'chmod +x {d}/salt-call; fi'
            for pkg in provides)
        body = (f'echo "$0 $*" >> {log}\n'
                'for a in "$@"; do\n'
                f'{checks}\n'
                'done\n'
                'exit 0')
        _bin(d, mgr, body)
    if systemctl:
        _bin(d, "systemctl", f'echo "systemctl $*" >> {log}\nexit 0')
    return d, log


def _run(path_dir):
    """Run the real script on that fake host.

    The shell is named ABSOLUTELY and PATH is the fake directory ALONE, so
    every lookup the script makes resolves to a fake and nothing leaks in from
    the machine the tests are running on — which really does have apt-get and
    systemctl, and would otherwise answer "no package manager" with one.
    """
    return subprocess.run(["/bin/sh", "-c", cmd_install_salt()],
                          capture_output=True, text=True,
                          env={"PATH": path_dir})


# ---------------------------------------------------------------------------
# it installs, on each manager
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mgr,pkg", [
    ("apt-get", "salt-common"),
    ("dnf", "salt"),
    ("yum", "salt"),
    ("zypper", "salt"),
    ("pacman", "salt"),
])
def test_it_installs_salt_on_each_package_manager(tmp_path, mgr, pkg):
    d, log = _host(tmp_path, mgr, provides=(pkg,))
    r = _run(d)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert os.path.exists(os.path.join(d, "salt-call")), "salt-call was not installed"
    assert any(pkg in c for c in _installs(log)), _installs(log)
    assert "Salt installed" in r.stdout, r.stdout


def test_a_host_that_already_has_salt_is_left_alone(tmp_path):
    """Including one where the operator installed and configured Salt
    themselves. Reinstalling over that is not ours to do."""
    d, log = _host(tmp_path, "apt-get", provides=("salt-common",), already=True)
    r = _run(d)
    assert r.returncode == 0, r.stderr
    assert "already installed" in r.stdout, r.stdout
    assert _installs(log) == [], \
        f"it ran the package manager on a host that already had Salt: {_installs(log)}"


def test_it_tries_the_next_package_when_the_first_provides_nothing(tmp_path):
    """Debian splits salt-call into salt-common and the daemon into
    salt-minion, and the split has not always been that way round. The script
    tries candidates in turn rather than encoding one guess per distro."""
    d, log = _host(tmp_path, "dnf", provides=("salt-minion",))   # NOT `salt`
    r = _run(d)
    assert r.returncode == 0, (r.stdout, r.stderr)
    got = _installs(log)
    assert any("salt-minion" in c for c in got), got
    assert any(c.endswith(" salt") for c in got), got
    assert os.path.exists(os.path.join(d, "salt-call"))


def test_it_stops_at_the_first_package_that_works(tmp_path):
    """Having installed salt-call, it must not go on installing the daemon
    too."""
    d, log = _host(tmp_path, "dnf", provides=("salt", "salt-minion"))
    _run(d)
    got = _installs(log)
    assert not any("salt-minion" in c for c in got), got


# ---------------------------------------------------------------------------
# ...and the check, not the package manager, decides
# ---------------------------------------------------------------------------
def test_a_package_manager_that_succeeds_without_providing_salt_call_fails(tmp_path):
    """THE POINT. Every candidate installs cleanly and none of them provides
    the binary — a distro that packages Salt under a name this file does not
    know. Reporting success here hands back a host that fails on its next
    apply, with nothing anywhere saying why."""
    d, log = _host(tmp_path, "zypper", provides=())
    r = _run(d)
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "still not on PATH" in r.stderr, r.stderr
    assert "by hand" in r.stderr, r.stderr
    assert _installs(log), "it did not even try"


def test_a_host_with_no_package_manager_says_so(tmp_path):
    """The SHARED message, not a Salt-specific one.

    An earlier draft carried its own copy of the cascade with its own wording
    ("...so Salt cannot be installed from here. Install it by hand"). Using the
    shared fragment means one sentence for every action in this module that
    needs a package manager, which is the point of _pkgmgr.py — and it already
    names exactly what was looked for, which is what an operator acts on.
    """
    d, _log = _host(tmp_path, None)
    r = _run(d)
    assert r.returncode != 0
    assert "No supported package manager" in r.stderr, r.stderr
    for mgr in ("dnf", "yum", "zypper", "apt-get", "pacman"):
        assert mgr in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# masterless
# ---------------------------------------------------------------------------
def test_the_minion_daemon_is_disabled(tmp_path):
    """Baseline runs `salt-call --local` and there is no Salt master in this
    product. On a distro that ships salt-call only inside salt-minion, the
    package starts a daemon that spends its life failing to reach a master
    that does not exist — logged as a failed unit on a host the console calls
    healthy."""
    d, log = _host(tmp_path, "apt-get", provides=("salt-minion",))
    r = _run(d)
    assert r.returncode == 0, r.stderr
    assert "disable --now salt-minion" in log.read_text(), log.read_text()


def test_a_host_without_systemd_still_installs(tmp_path):
    """Disabling a daemon that cannot exist must not fail the install."""
    d, _log = _host(tmp_path, "apt-get", provides=("salt-common",), systemctl=False)
    r = _run(d)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "Salt installed" in r.stdout


# ---------------------------------------------------------------------------
# the cascade matches the Controller's
# ---------------------------------------------------------------------------
def test_dnf_is_preferred_over_yum(tmp_path):
    """A RHEL host with both. The Controller's _pkgmgr.py prefers dnf and this
    is a deliberate copy of that cascade; the two disagreeing would mean one
    product installing through a manager the other does not."""
    d, log = _host(tmp_path, "dnf", provides=("salt",))
    _bin(d, "yum", 'echo "yum $*" >> %s\nexit 0' % (tmp_path / "log"))
    r = _run(d)
    assert r.returncode == 0, r.stderr
    got = _installs(log)
    assert got, "nothing was installed at all"
    assert not any(c.startswith("yum ") for c in got), f"it chose yum over dnf: {got}"
    assert any(c.startswith("dnf ") for c in got), got


def test_the_cascade_is_in_the_documented_order():
    """Pinned because every builder in this module shares this cascade: a host
    served by dnf for packages must be served by dnf for Salt."""
    c = pkgmgr_detect_fragment()
    order = [m for m in ("dnf", "yum", "zypper", "apt-get", "pacman")]
    seen = [m for m in sorted(order, key=lambda m: c.index(f"command -v {m} "))]
    assert seen == order, seen
