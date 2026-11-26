"""A `$VAR` that is never assigned is a crash, not a blank, under `set -u`.

sysiblectl runs under `set -euo pipefail`. A reference to a variable nothing
assigns does not expand to the empty string there -- bash prints
"VAR: unbound variable" and exits. So one such reference is a hard failure of
whatever branch contains it.

This was shipped:

    if _images_all_present "$cfg"; then
      _bld=()
      _ok "  every image is already in the local store -- starting without a rebuild."
      _warn "  (to rebuild from source anyway: $ME $p update)"     # <-- nothing sets ME
    fi

It sat inside the branch that runs only when every image is ALREADY on the
host, which is precisely the airgapped install the surrounding commit was
written to enable. So `sysiblectl <product> rebuild` exited instantly on the
one machine the feature existed for, every product failed to come up, and on
any host that still had to build -- i.e. every host with a network, including
every test -- the branch never ran and nothing noticed.

`bash -n` does not catch it: the syntax is valid. Only running that branch, or
this, does.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Shell-provided or environment-provided; never assigned in the script itself.
PROVIDED = set("""
0 1 2 3 4 5 6 7 8 9 @ * # ? - $ ! _ IFS PATH HOME PWD OLDPWD SHELL USER LOGNAME
TERM LANG LC_ALL HOSTNAME UID EUID PPID RANDOM SECONDS LINENO BASH BASH_SOURCE
BASH_VERSION FUNCNAME PIPESTATUS REPLY OPTARG OPTIND SHLVL TMPDIR EDITOR VISUAL
PAGER COLUMNS LINES SUDO_USER SUDO_UID SUDO_GID DEBIAN_FRONTEND NEWT_COLORS
BASH_REMATCH GLOBIGNORE HOSTTYPE OSTYPE MACHTYPE DIRSTACK CDPATH
""".split())

ASSIGN = re.compile(r"""(?mx)
    (?:^|[;&|(){}\s])
    (?:local\s+|declare\s+(?:-\w+\s+)?|export\s+|readonly\s+|typeset\s+)?
    ([A-Za-z_][A-Za-z0-9_]*)
    (?:\[[^\]]*\])?
    \+?=
""")
LOOPVAR = re.compile(r"(?m)\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\b")
READVAR = re.compile(r"(?m)\bread\s+(?:-\w+\s+)*((?:[A-Za-z_][A-Za-z0-9_]*\s*)+)")
MAPFILE = re.compile(r"(?m)\bmapfile\s+(?:-\w+\s+)*([A-Za-z_][A-Za-z0-9_]*)")
GETOPTS = re.compile(r"(?m)\bgetopts\s+\S+\s+([A-Za-z_][A-Za-z0-9_]*)")

# ${X:-y}, ${X:+y}, ${X##y} and friends are all safe under set -u. A bare $X or
# a plain ${X} is not.
REF_PLAIN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
REF_GUARDED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-=+?]|##|%%|#|%|//|/|\^|,)")

SCRIPTS = [p for p in (ROOT / "deploy/sysiblectl",) if p.exists()]


def _unbound(path):
    src = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^set\s+-\w*u", src, re.M):
        return []
    assigned = set(PROVIDED)
    for rx in (ASSIGN, LOOPVAR, MAPFILE, GETOPTS):
        assigned |= {m.group(1) for m in rx.finditer(src)}
    for m in READVAR.finditer(src):
        assigned |= set(m.group(1).split())
    # A name used safely ANYWHERE is treated as intentionally optional.
    assigned |= {m.group(1) for m in REF_GUARDED.finditer(src)}

    out = []
    for n, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        code = re.sub(r"(?<!\\)#.*$", "", line)
        for m in REF_PLAIN.finditer(code):
            name = m.group(1) or m.group(2)
            if name not in assigned:
                out.append((n, name, line.strip()[:100]))
    return out


def test_there_is_a_script_to_check():
    assert SCRIPTS, "deploy/sysiblectl is missing — this test is checking nothing"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_variable_is_referenced_without_ever_being_assigned(script):
    bad = _unbound(script)
    assert not bad, "under `set -u` each of these exits the script:\n" + "\n".join(
        f"  {script.name}:{n}: ${name}\n      {line}" for n, name, line in bad
    )
