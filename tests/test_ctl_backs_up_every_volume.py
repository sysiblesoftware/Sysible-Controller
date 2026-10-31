"""`sysiblectl <p> backup` must not quietly skip a store.

Reported from a real update, in its own success line:

    Sysible SLOP gateway updated to ed84e1c.
      Volume 'slop-idp-data slop-caddy-data slop-caddy-config flashback-data'
      preserved.

Five named volumes exist in that stack. Four are listed. The missing one is
baseline-data, which holds every declared state and every version of it — and
`p_backup` iterates exactly this list, one tarball per volume. So an operator
could run the backup, watch four tarballs appear, be told it succeeded, and
have no copy of the one store in the stack that cannot be rebuilt from anything
else. That is the failure Flashback exists to prevent, happening to the tool
that is supposed to prevent it.

The cause is structural, not a typo: the list was hard-coded here and the
compose file that actually defines those volumes lives in a DIFFERENT
repository. A list copied across a repo boundary is a list that drifts, and
nothing on either side could notice.

So the list is now asked of compose, and the hard-coded table is only the
fallback for when compose cannot be reached.
"""
import os
import re
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysiblectl")
# The SLOP checkout, when it happens to sit alongside this one.
SLOP_COMPOSE = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                            "slop", "docker-compose.yml")


@pytest.fixture
def lib(tmp_path):
    src = open(CTL, encoding="utf-8").read()
    p = tmp_path / "ctl.lib.sh"
    p.write_text(re.sub(r'^main "\$@"\s*$', "", src, flags=re.M))
    return str(p)


def run(lib, snippet):
    script = f'. "{lib}"\n' + textwrap.dedent(snippet)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                        "HOME": os.environ.get("HOME", "/root")})
    return r.returncode, r.stdout.strip(), r.stderr


def test_the_volume_list_comes_from_compose_when_compose_can_answer(lib):
    """Whatever the stack actually declares, including something this file has
    never heard of."""
    rc, out, err = run(lib, """
        _discover() { return 0; }
        _compose() { printf 'a-data\\nb-data\\nsomething-new-data\\n'; }
        _p_volume slop
    """)
    assert rc == 0, err
    assert out == "a-data b-data something-new-data", out


def test_it_falls_back_to_the_table_when_compose_cannot_be_reached(lib):
    """A product that is not deployed on this host still has to produce a
    sensible answer — `backup` should say which volumes it expected."""
    rc, out, err = run(lib, """
        _discover() { return 1; }
        _p_volume slop
    """)
    assert rc == 0, err
    assert "baseline-data" in out, out
    assert "flashback-data" in out, out


def test_the_fallback_table_lists_baselines_store(lib):
    """The specific omission that was shipped."""
    rc, out, err = run(lib, "_p_volume_declared slop")
    assert "baseline-data" in out, \
        f"Baseline's store is missing from the fallback list again: {out}"


def test_backup_writes_one_tarball_per_volume(lib, tmp_path):
    """The list is not cosmetic — it is the loop p_backup runs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$1" in volume) exit 0 ;; run) printf "%s\\n" "$*" >> "$TARLOG"; exit 0 ;; esac\n'
        "exit 0\n")
    (bindir / "docker").chmod(0o755)
    tarlog = tmp_path / "tar.log"
    script = (f'. "{lib}"\n'
              '_discover() { return 1; }\n'      # force the fallback table
              'cd "$WORK"; p_backup slop >/dev/null 2>&1\n')
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                   env={"PATH": str(bindir) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
                        "HOME": os.environ.get("HOME", "/root"),
                        "TARLOG": str(tarlog), "WORK": str(tmp_path)})
    calls = tarlog.read_text() if tarlog.exists() else ""
    for vol in ("slop-idp-data", "flashback-data", "baseline-data"):
        assert vol in calls, f"backup never touched {vol}:\n{calls}"


@pytest.mark.skipif(not os.path.exists(SLOP_COMPOSE),
                    reason="the SLOP checkout is not alongside this one")
def test_the_fallback_matches_the_real_slop_compose_file(lib):
    """When both repositories are checked out together — a developer machine,
    and the host the installer lays down — the two can be compared directly.
    This is the drift that actually happened, in the one place it can be seen.
    """
    text = open(SLOP_COMPOSE, encoding="utf-8").read()
    body = text.split("\nvolumes:", 1)[1]
    body = re.split(r"\n(?=[a-zA-Z])", body)[0]
    declared = set(re.findall(r"^  ([a-z0-9][a-z0-9_-]*):", body, re.M))
    rc, out, err = run(lib, "_p_volume_declared slop")
    listed = set(out.split())
    assert declared, "no volumes parsed out of the SLOP compose file"
    assert declared == listed, (
        f"the fallback list and docker-compose.yml disagree — "
        f"only in compose: {sorted(declared - listed)}; "
        f"only in sysiblectl: {sorted(listed - declared)}")


# ---------------------------------------------------------------------------
# …and the name it asks docker for has to be the name docker has
# ---------------------------------------------------------------------------
# `compose config --volumes` prints the DECLARED name (`slep-data`). The volume
# compose actually creates is `<project>_slep-data`, and the project name
# defaults to the basename of the directory holding the compose file — `deploy`
# for SLEP, whose compose file lives in deploy/. _compose passes no -p, so that
# default is what every deployment used.
#
# So `docker volume inspect slep-data` failed and p_backup printed
#
#     volume 'slep-data' not found — skipping (is Sysible Linux Engineering
#     Platform deployed here?)
#
# on a host where SLEP plainly is deployed, and wrote no tarball. Same shape as
# the drift above: the backup reports success and there is no copy.
def test_a_declared_name_resolves_to_the_prefixed_volume_that_exists(lib):
    rc, out, err = run(lib, """
        CFG=/srv/slep/deploy/docker-compose.yml
        docker() { [[ "$1 $2" == "volume inspect" && "$3" == "deploy_slep-data" ]]; }
        _volume_real slep-data
    """)
    assert rc == 0, err
    assert out == "deploy_slep-data", out


def test_an_unprefixed_volume_is_still_found(lib):
    """An external volume, or one from an older layout, carries no prefix."""
    rc, out, err = run(lib, """
        CFG=/srv/slep/deploy/docker-compose.yml
        docker() { [[ "$1 $2" == "volume inspect" && "$3" == "slep-data" ]]; }
        _volume_real slep-data
    """)
    assert rc == 0, err
    assert out == "slep-data", out


def test_the_prefixed_one_wins_when_both_exist(lib):
    """compose made the prefixed one; a stray unprefixed volume of the same name
    must not be backed up instead of the live store."""
    rc, out, err = run(lib, """
        CFG=/srv/slep/deploy/docker-compose.yml
        docker() { [[ "$1 $2" == "volume inspect" ]]; }
        _volume_real slep-data
    """)
    assert rc == 0, err
    assert out == "deploy_slep-data", out


def test_composes_own_label_is_the_last_resort(lib):
    """Neither guess exists. Rather than report "not found" for a volume that is
    right there under a project name we did not predict, ask docker which volume
    compose labelled for this declaration."""
    rc, out, err = run(lib, """
        CFG=/srv/slep/deploy/docker-compose.yml
        docker() {
          if [[ "$1 $2" == "volume inspect" ]]; then return 1; fi
          if [[ "$1 $2" == "volume ls" ]]; then echo "someproject_slep-data"; return 0; fi
          return 1
        }
        _volume_real slep-data
    """)
    assert rc == 0, err
    assert out == "someproject_slep-data", out


def test_it_falls_back_to_the_declared_name_when_nothing_is_found(lib):
    """Not deployed here at all: keep today's behaviour and let p_backup say so,
    naming the volume the operator would recognise."""
    rc, out, err = run(lib, """
        CFG=/srv/slep/deploy/docker-compose.yml
        docker() { return 1; }
        _volume_real slep-data
    """)
    assert rc == 0, err
    assert out == "slep-data", out


def test_the_project_name_is_derived_the_way_compose_derives_it(lib):
    for cfg, want in [("/srv/slep/deploy/docker-compose.yml", "deploy"),
                      ("/srv/Sysible-Controller/docker-compose.yml", "sysible-controller"),
                      ("/srv/My_App 2/docker-compose.yml", "my_app2")]:
        rc, out, err = run(lib, f"""
            CFG={cfg!r}
            _compose_project
        """.replace("'", '"'))
        assert rc == 0, err
        assert out == want, f"{cfg} -> {out!r}, wanted {want!r}"


def test_the_volume_list_is_resolved_end_to_end(lib):
    """What p_backup actually iterates: declared names in, real names out."""
    rc, out, err = run(lib, """
        _discover() { CFG=/srv/slep/deploy/docker-compose.yml; return 0; }
        _compose() { printf 'slep-data\\nother-data\\n'; }
        docker() { [[ "$1 $2" == "volume inspect" && "$3" == deploy_* ]]; }
        _p_volume slep
    """)
    assert rc == 0, err
    assert out == "deploy_slep-data deploy_other-data", out


def test_backup_mounts_the_resolved_volume(lib, tmp_path):
    """The whole point, through p_backup itself."""
    rc, out, err = run(lib, f"""
        cd {tmp_path}
        _hdr() {{ :; }}; _ok() {{ :; }}; _warn() {{ echo "WARN $*"; }}
        _p_pretty() {{ echo SLEP; }}
        _p_volume() {{ echo deploy_slep-data; }}
        docker() {{
          if [[ "$1 $2" == "volume inspect" ]]; then [[ "$3" == "deploy_slep-data" ]]; return; fi
          if [[ "$1" == "run" ]]; then echo "RAN $*"; return 0; fi
          return 1
        }}
        p_backup slep
    """)
    assert rc == 0, err
    assert "WARN" not in out, out
    assert "deploy_slep-data:/data:ro" in out, out
