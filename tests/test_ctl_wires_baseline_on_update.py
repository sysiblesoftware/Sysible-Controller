"""An update has to repair the wiring the installer writes, Baseline's included.

Reported, pressing Apply in Baseline on a platform that had been updated rather
than reinstalled: sixteen hosts, sixteen failures, every one of them

    Desired-state configuration is not configured on this controller
    (SYSIBLE_BASELINE_URL and SYSIBLE_BASELINE_AGENT_TOKEN unset).

Those two settings live in the CONTROLLER's .env and only install.sh writes
them. `update` — from the Updates page or sysiblectl — pulls code and rebuilds
images; it never re-runs the installer. So a platform installed before Baseline
was wired up keeps working in every other respect and has nothing here, and the
first anyone hears of it is every apply failing at once.

_slop_seed_cross_app_env already existed to close exactly this gap and already
repaired the SSO secret, each app's trust flag and FLASHBACK's two settings on
every SLOP update. Baseline shipped later and was never added to it.

The correction matters as much as the seeding: an installer from before the
wiring fix wrote Flashback's url and token UNDER Baseline's names, so on those
installs the keys are present and point at the wrong service — which a
seed-if-absent would leave exactly as it found it.
"""
import os
import re
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysiblectl")

FB_PORT, BL_PORT = "8770", "8771"
FB_URL = f"http://host.docker.internal:{FB_PORT}"
BL_URL = f"http://host.docker.internal:{BL_PORT}"
FB_TOK, BL_TOK = "f" * 64, "b" * 64


@pytest.fixture
def box(tmp_path):
    src = open(CTL, encoding="utf-8").read()
    lib = tmp_path / "ctl.lib.sh"
    lib.write_text(re.sub(r'^main "\$@"\s*$', "", src, flags=re.M))

    slop = tmp_path / "slop"
    ctrl = tmp_path / "controller"
    slop.mkdir()
    ctrl.mkdir()
    (slop / ".env").write_text(
        f"SYSIBLE_SSO_SHARED_SECRET=s{'s' * 31}\n"
        f"SYSIBLE_FLASHBACK_AGENT_TOKEN={FB_TOK}\n"
        f"SYSIBLE_FLASHBACK_AGENT_PORT={FB_PORT}\n"
        f"SYSIBLE_BASELINE_AGENT_TOKEN={BL_TOK}\n"
        f"SYSIBLE_BASELINE_AGENT_PORT={BL_PORT}\n")
    return {"lib": str(lib), "slop": str(slop), "ctrl": str(ctrl),
            "cenv": ctrl / ".env"}


def seed(box, controller_env=""):
    """Run the seeding with SLOP's checkout as WD and a stub Controller dir."""
    (box["cenv"]).write_text(controller_env)
    script = f'''. "{box['lib']}"
        WD="{box['slop']}"
        _git_root() {{ echo "{box['slop']}"; }}
        _product_compose_dir() {{ [[ "$1" == controller ]] && echo "{box['ctrl']}" || return 1; }}
        _recreate_product() {{ echo "RECREATED $1"; }}
        _slop_seed_cross_app_env
    '''
    r = subprocess.run(["bash", "-c", textwrap.dedent(script)],
                       capture_output=True, text=True, timeout=120,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HOME": os.environ.get("HOME", "/root")})
    return r.returncode, r.stdout + r.stderr, _kv(box["cenv"].read_text())


def _kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
def test_a_controller_with_nothing_gets_both_settings(box):
    """The reported case: updated, never reinstalled, so the keys are absent."""
    rc, out, env = seed(box)
    assert rc == 0, out
    assert env.get("SYSIBLE_BASELINE_URL") == BL_URL, env
    assert env.get("SYSIBLE_BASELINE_AGENT_TOKEN") == BL_TOK, env


def test_flashback_is_still_wired_too(box):
    """Baseline is added BESIDE it, not instead of it."""
    _rc, _out, env = seed(box)
    assert env.get("SYSIBLE_FLASHBACK_URL") == FB_URL, env
    assert env.get("SYSIBLE_FLASHBACK_AGENT_TOKEN") == FB_TOK, env


def test_the_two_services_do_not_share_a_token_or_a_port(box):
    """One token for both means a compromise of either is a compromise of both,
    and one port means the desired-state client talks to the backup tool."""
    _rc, _out, env = seed(box)
    assert env["SYSIBLE_BASELINE_AGENT_TOKEN"] != env["SYSIBLE_FLASHBACK_AGENT_TOKEN"]
    assert env["SYSIBLE_BASELINE_URL"] != env["SYSIBLE_FLASHBACK_URL"]


def test_an_installer_that_wrote_flashbacks_values_is_corrected(box):
    """The pre-fix installer put FB_URL and FB_TOKEN under Baseline's names. The
    keys are PRESENT and wrong, which seed-if-absent would leave alone."""
    rc, out, env = seed(
        box,
        f"SYSIBLE_BASELINE_URL={FB_URL}\nSYSIBLE_BASELINE_AGENT_TOKEN={FB_TOK}\n")
    assert rc == 0, out
    assert env["SYSIBLE_BASELINE_URL"] == BL_URL, env
    assert env["SYSIBLE_BASELINE_AGENT_TOKEN"] == BL_TOK, env
    assert "WRONG address" in out or "DIFFERENT Baseline agent token" in out, out


def test_a_deliberate_address_is_left_alone(box):
    """Baseline running somewhere else is a real deployment, and overwriting it
    on every update would be the tool fighting the operator."""
    mine = "http://baseline.internal:9999"
    _rc, _out, env = seed(box, f"SYSIBLE_BASELINE_URL={mine}\n"
                               f"SYSIBLE_BASELINE_AGENT_TOKEN={BL_TOK}\n")
    assert env["SYSIBLE_BASELINE_URL"] == mine, env


def test_a_stale_token_is_corrected(box):
    """SLOP's .env mints it and Baseline itself reads it from there, so a
    Controller that disagrees is a Controller that gets 401 on every apply —
    indistinguishable from an absent one, from the browser."""
    _rc, out, env = seed(box, f"SYSIBLE_BASELINE_AGENT_TOKEN={'9' * 64}\n")
    assert env["SYSIBLE_BASELINE_AGENT_TOKEN"] == BL_TOK, env
    assert "DIFFERENT Baseline agent token" in out, out


def test_nothing_is_rewritten_when_it_is_already_right(box):
    """An update that changes nothing must not recreate the Controller."""
    _rc, out, _env = seed(
        box, f"SYSIBLE_SSO_SHARED_SECRET=s{'s' * 31}\n"
             "SYSIBLE_WEBGUI_TRUST_SSO=1\n"
             f"SYSIBLE_FLASHBACK_URL={FB_URL}\n"
             f"SYSIBLE_FLASHBACK_AGENT_TOKEN={FB_TOK}\n"
             f"SYSIBLE_BASELINE_URL={BL_URL}\n"
             f"SYSIBLE_BASELINE_AGENT_TOKEN={BL_TOK}\n")
    assert "RECREATED controller" not in out, out


def test_it_recreates_the_controller_after_writing(box):
    """A value in .env is not a value the process has: docker reads .env when it
    creates the container."""
    _rc, out, _env = seed(box)
    assert "RECREATED controller" in out, out


def test_a_slop_env_with_no_baseline_token_says_so(box):
    (box["cenv"]).write_text("")
    import pathlib
    envf = pathlib.Path(box["slop"]) / ".env"
    envf.write_text(envf.read_text().replace(f"SYSIBLE_BASELINE_AGENT_TOKEN={BL_TOK}\n", ""))
    _rc, out, env = seed(box)
    assert "SYSIBLE_BASELINE_AGENT_TOKEN" not in env, env
    assert "Re-run the SLOP installer" in out, out
