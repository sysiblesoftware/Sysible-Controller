"""Two reports about the same screen, one about noise and one about danger.

    "== destroy arch-01 ==
     error: Failed to destroy domain 'arch-01'
     error: Requested operation is not valid: domain is not running"
    ...fifteen times... "Why is this failing?"

It wasn't. `virsh destroy` is libvirt's FORCE POWER OFF -- not a delete -- and
it only applies to a running domain. The fleet was already shut down, so every
row came back red. The meaning of that output is "they were all already off",
and nothing in it says so.

    "there should be a warning before deleting a container/VM or any other
     critical infra"

There wasn't. `danger` is per-ACTION and prompts before running; the action
that force-powers off a VM is the same one that starts it, so the flag was
never set on it. `destroy` across a fleet and `rm` on a container both ran on
one click, and the prompt they would have got -- "Run action on 3 host(s)?" --
names neither the operation nor what it does.
"""
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
TOOLPAGE = (REPO / "webgui" / "frontend" / "src" / "views" / "ToolPage.jsx").read_text(encoding="utf-8")
SH = shutil.which("sh")
NODE = shutil.which("node")


def _virsh(tmp_path, domains, fail_with=None, ok=()):
    """A virsh whose domains are all defined but SHUT OFF, as theirs were."""
    b = tmp_path / "bin"; b.mkdir(parents=True, exist_ok=True)
    err = fail_with or "error: Requested operation is not valid: domain is not running"
    names = " ".join(shlex.quote(d) for d in domains) or "''"
    okset = " ".join(shlex.quote(o) for o in ok)
    (b / "virsh").write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"list\" ]; then printf '%s\\n' {names}; exit 0; fi\n"
        f'for good in {okset or "__none__"}; do\n'
        '  if [ "$2" = "$good" ]; then echo "Domain \'$2\' acted"; exit 0; fi\n'
        'done\n'
        f'echo {shlex.quote(err)} >&2\n'
        "exit 1\n")
    (b / "virsh").chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ.get('PATH', '/usr/bin:/bin')}"}


def _run(cmd, env):
    p = subprocess.run([SH, "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, (p.stdout + p.stderr)


# ---- "Why is this failing?" -------------------------------------------------
@pytest.mark.skipif(not SH, reason="no sh on this machine")
@pytest.mark.parametrize("target", ["all", "arch-01 arch-02 arch-03"])
def test_a_fleet_that_is_already_off_is_not_fifteen_errors(tmp_path, target):
    from client import _api_containers as c
    env = _virsh(tmp_path, ["arch-01", "arch-02", "arch-03"])
    rc, out = _run(c.cmd_vm_action("destroy", target), env)
    assert rc == 0, f"asking for a state the fleet is already in is not a failure:\n{out}"
    assert out.count("already stopped") == 3, out
    assert "is not valid" not in out, "the raw libvirt error is still being shown"
    assert "3 already there" in out, "the summary does not say what happened"


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_a_real_failure_still_fails_and_still_shows_its_reason(tmp_path):
    """The point of telling them apart. Swallowing a genuine error would be far
    worse than the noise this removes."""
    from client import _api_containers as c
    env = _virsh(tmp_path, ["vm-1"],
                 fail_with="error: access denied: client is not authorized")
    rc, out = _run(c.cmd_vm_action("destroy", "all"), env)
    assert rc != 0, "a real failure was reported as success"
    assert "access denied" in out, "the reason was swallowed"
    assert "already" not in out, "a real failure was filed as 'already there'"


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_one_already_off_and_one_broken_do_not_cancel_out(tmp_path):
    from client import _api_containers as c
    env = _virsh(tmp_path, ["off-1", "broken-1"])
    # off-1 reports not-running; broken-1 is made to fail for a real reason
    b = tmp_path / "bin"
    (b / "virsh").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"list\" ]; then printf '%s\\n' off-1 broken-1; exit 0; fi\n"
        'if [ "$2" = "off-1" ]; then\n'
        '  echo "error: Requested operation is not valid: domain is not running" >&2; exit 1\n'
        'fi\n'
        'echo "error: access denied" >&2\nexit 1\n')
    (b / "virsh").chmod(0o755)
    rc, out = _run(c.cmd_vm_action("destroy", "all"), env)
    assert rc != 0, "a real failure was masked by an already-off one"
    assert "already stopped: off-1" in out
    assert "access denied" in out
    assert "some failed" in out, "the summary hides that something failed"


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_start_on_a_running_domain_is_also_not_an_error(tmp_path):
    from client import _api_containers as c
    env = _virsh(tmp_path, ["vm-1"], fail_with="error: Domain is already active")
    rc, out = _run(c.cmd_vm_action("start", "all"), env)
    assert rc == 0, out
    assert "already running" in out


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_a_working_action_is_untouched(tmp_path):
    from client import _api_containers as c
    env = _virsh(tmp_path, ["vm-1", "vm-2"], ok=["vm-1", "vm-2"])
    rc, out = _run(c.cmd_vm_action("start", "all"), env)
    assert rc == 0 and out.count("== start") == 2, out
    assert "already" not in out


# ---- "there should be a warning before deleting a container/VM" -------------
def test_the_destructive_choices_are_marked_and_say_what_they_do():
    """Per VALUE, not per action: the action that force-powers off a VM is the
    same one that starts it, so an all-or-nothing flag either prompts before
    `start` -- training people to click through -- or never prompts at all."""
    from webgui import actions
    for name, value in (("vm_action", "destroy"), ("cont_action", "rm")):
        pr = next(p for p in actions.get(name).params if p.name == "action")
        assert value in pr.danger_values, f"{name}/{value} runs with no warning"
        text = pr.danger_values[value]
        assert len(text) > 40, f"{name}/{value} warns without saying what it does"
        assert value not in text.lower().split(), \
            f"{name}/{value} just repeats the verb instead of the consequence"
    # the one that is most often misread
    d = next(p for p in actions.get("vm_action").params if p.name == "action").danger_values
    assert "NOT delete" in d["destroy"], \
        "the warning does not correct the obvious misreading of the word 'destroy'"


def test_the_flag_reaches_the_browser():
    src = (REPO / "webgui" / "actions.py").read_text(encoding="utf-8")
    assert '"danger_values": pr.danger_values' in src, "the catalog drops it"


def test_the_safe_choices_are_not_marked():
    """A prompt before `start` is how people learn to dismiss prompts."""
    from webgui import actions
    pr = next(p for p in actions.get("vm_action").params if p.name == "action")
    for safe in ("start", "shutdown", "suspend", "resume"):
        assert safe not in pr.danger_values, f"{safe} is not destructive"


def test_the_confirmation_reads_the_actions_own_params():
    """sharedParams lives in GroupSection, a different component -- reading it
    from run() throws at runtime, and no bundler says so."""
    body = TOOLPAGE[TOOLPAGE.index("const perilous = ["):]
    body = body[:body.index("Proceed?")]
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))
    assert "for (const p of action.params)" in code, \
        "the confirmation iterates something that is not in scope here"
    assert "sharedParams" not in code


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
@pytest.mark.parametrize("chosen,prompts", [
    ("destroy", True), ("rm", True), ("start", False), ("shutdown", False),
])
def test_only_a_destructive_choice_prompts(tmp_path, chosen, prompts):
    """Driven, not read: the real branch, with the real message."""
    import json
    body = TOOLPAGE[TOOLPAGE.index("const perilous = ["):]
    body = body[:body.index("Proceed?`)) return;") + len("Proceed?`)) return;\n    }")]
    body = body.replace("if (!window.confirm(", "MSG = (").replace(")) return;", ");")
    js = tmp_path / "t.mjs"
    js.write_text(
        "const action = " + json.dumps({"params": [
            {"name": "action", "danger_values": {
                "destroy": "Cuts the power to the VM immediately, like pulling the plug. "
                           "(It does NOT delete the VM.)",
                "rm": "DELETES the container and anything written inside it.",
            }}, {"name": "name"}]}) + ";\n"
        "const built = " + json.dumps({"action": chosen, "name": "a b c"}) + ";\n"
        "const targets = ['h1','h2'];\n"
        "let MSG = null;\n" + body + "\n"
        "console.log(JSON.stringify(MSG));\n")
    out = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    msg = __import__("json").loads(out.stdout.strip())
    if not prompts:
        assert msg is None, f"{chosen} is not destructive but prompted:\n{msg}"
        return
    assert msg, f"{chosen} is destructive and did not prompt"
    assert chosen.upper() in msg, "the prompt does not name the operation"
    assert "3 selected" in msg, "the prompt does not say how many it will hit"
    assert "h1, h2" in msg, "the prompt does not name the hosts"


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_the_prompt_says_everything_when_the_target_is_all(tmp_path):
    """'all' is the case worth being loudest about."""
    import json
    body = TOOLPAGE[TOOLPAGE.index("const perilous = ["):]
    body = body[:body.index("Proceed?`)) return;") + len("Proceed?`)) return;\n    }")]
    body = body.replace("if (!window.confirm(", "MSG = (").replace(")) return;", ");")
    js = tmp_path / "t.mjs"
    js.write_text(
        'const action = {params:[{name:"action",danger_values:{destroy:"Cuts the power."}},{name:"name"}]};\n'
        'const built = {action:"destroy", name:"all"};\n'
        "const targets = ['only-host'];\n"
        "let MSG = null;\n" + body + "\nconsole.log(JSON.stringify(MSG));\n")
    out = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    msg = json.loads(out.stdout.strip())
    assert "EVERY ONE" in msg, f"'all' did not read as everything:\n{msg}"
    assert "1 host (only-host)" in msg, f"singular host reads wrong:\n{msg}"
