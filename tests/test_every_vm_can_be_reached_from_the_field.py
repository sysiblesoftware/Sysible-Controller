"""Choosing VMs is a selection over the hosts you checked, not a name you type.

Three reports against the same field:

  "There is no way to shutdown all the vms from the single field."
  "How do I select all VMs except 1?"
  "The field box doesn't correlate to the host list on the left in any way."

One cause. It was a dropdown fed from ONE host -- the single selected one --
and a free-text box the rest of the time. So with sixteen hosts checked it
stopped fetching anything and became a text box, and the thing it asked for (a
VM) had no visible relationship to the things that have VMs (the hosts).
Excluding one VM meant typing the names of all the others.

api.runTool already took a LIST of hosts and returned a row per host; the field
asked it for one and read results[0]. The inventory was one argument away.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
FIELD = (REPO / "webgui" / "frontend" / "src" / "components" / "ParamField.jsx").read_text(encoding="utf-8")
SH = shutil.which("sh")


# ---- the command, EXECUTED, not pattern-matched -----------------------------
def _fake_virsh(tmp_path, domains):
    """A virsh that owns `domains` and records what it was told to do."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "acted"
    # printf '%s\n' ARG... -- one line per domain. NOT printf %s "a\nb": the
    # escapes would sit in the ARGUMENT, which printf does not interpret, so the
    # fake would answer with one line containing literal backslash-n.
    import shlex as _sh
    args = " ".join(_sh.quote(d) for d in domains) or "''"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "list" ]; then printf \'%s\\n\' ' + args + '; exit 0; fi\n'
        'echo "$1 $2" >> ' + _sh.quote(str(log)) + "\n"
        "exit 0\n"
    )
    (bin_dir / "virsh").write_text(script)
    (bin_dir / "virsh").chmod(0o755)
    return bin_dir, log


def _run(tmp_path, domains, action, names):
    from client import _api_containers as c
    bin_dir, log = _fake_virsh(tmp_path, domains)
    cmd = c.cmd_vm_action(action, names)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    p = subprocess.run([SH, "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    acted = log.read_text().split("\n") if log.exists() else []
    return p.returncode, p.stdout + p.stderr, [a for a in acted if a.strip()]


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_a_host_acts_only_on_the_vms_it_has(tmp_path):
    """ONE command goes to EVERY selected host, and the chosen VMs are spread
    across them. Acting blind made each host fail for every VM it does not own,
    so a run that did exactly what was asked came back red on both."""
    rc, out, acted = _run(tmp_path, ["web-1", "db-1"], "shutdown", "web-1 other-1")
    assert acted == ["shutdown web-1"], acted
    assert "not on this host: other-1" in out
    assert rc == 0, "the host did what it could; that is not a failure"


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_all_except_one(tmp_path):
    """The gesture the picker produces: tick everything, untick one."""
    rc, out, acted = _run(tmp_path, ["a", "b", "c"], "shutdown", "a c")
    assert acted == ["shutdown a", "shutdown c"], acted
    assert "b" not in out.replace("not on this host", "")
    assert rc == 0


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_a_name_no_host_has_is_a_failure(tmp_path):
    """Skipping must not become a way to silently do nothing."""
    rc, out, acted = _run(tmp_path, ["a"], "start", "typo-1")
    assert acted == []
    assert rc != 0
    assert "0 of 1" in out


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_the_match_is_exact(tmp_path):
    """`web` must not act on `web-1`. A substring match here powers off the
    wrong machine."""
    rc, out, acted = _run(tmp_path, ["web-1"], "shutdown", "web")
    assert acted == []
    assert rc != 0


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_a_domain_whose_name_starts_with_a_dash(tmp_path):
    """Passed to grep without `--` it reads as a flag."""
    rc, out, acted = _run(tmp_path, ["-odd"], "start", "-odd")
    assert acted == ["start -odd"], acted


def test_all_still_means_every_vm_on_the_host():
    from client import _api_containers as c
    cmd = c.cmd_vm_action("shutdown", "all")
    assert "virsh list --all --name" in cmd
    assert "|| rc=1" in cmd, "one failing domain aborts the rest"


@pytest.mark.parametrize("word", ["all", "ALL", "*"])
def test_the_spellings_the_hint_has_always_promised(word):
    from client import _api_containers as c
    assert "virsh list --all --name" in c.cmd_vm_action("shutdown", word)


# ---- the field ---------------------------------------------------------------
def test_it_asks_every_checked_host_not_just_one():
    """The whole correlation problem in one argument."""
    assert "api.runTool(p.source, targets, {})" in FIELD, \
        "the picker still fetches from a single host"
    # Comments are not code -- the explanation above the component names the old
    # expression, and matching the whole file would find it there.
    code = "\n".join(l for l in FIELD.splitlines() if not l.strip().startswith("//"))
    assert "results[0]" not in code, "it still reads only the first host's row"


def test_the_vms_are_grouped_under_their_host():
    assert "byHost" in FIELD
    block = FIELD[FIELD.index("// ---- the list ---"):]
    assert "hosts.map" in block, "the list is not grouped by host"
    assert "byHost[h].map" in block, "a host's VMs are not listed under it"


def test_a_vm_is_a_checkbox_when_the_field_takes_several():
    block = FIELD[FIELD.index("// ---- the list ---"):]
    assert 'type={multi ? "checkbox" : "radio"}' in block, \
        "there is no way to select more than one, or to unselect one of many"


def test_a_single_value_field_cannot_be_handed_a_list():
    """vm_info takes exactly one domain; three ticked boxes would be quoted as
    one name. The answer is on the param, not inferred from its help text."""
    from webgui import actions
    spec = actions.get("vm_action")
    info = actions.get("vm_info")
    assert next(x for x in spec.params if x.name == "name").multi is True
    assert next(x for x in info.params if x.name == "name").multi is False
    assert '"multi": pr.multi' in (REPO / "webgui" / "actions.py").read_text(encoding="utf-8"), \
        "the flag never reaches the browser"


def test_unticking_one_leaves_the_rest_as_an_explicit_list():
    """Everything ticked collapses to the word; anything less is the names."""
    block = FIELD[FIELD.index("function commit("):]
    block = block[:block.index("const toggle")]
    assert 'onChange(p.name, "all")' in block, "a full selection is not collapsed"
    assert 'arr.join(" ")' in block, "a partial selection is not sent as names"


def test_with_no_hosts_checked_it_says_so_and_offers_nothing():
    """It read "Every VM on all 0 selected hosts." under a field that could not
    do anything, beside an error telling the operator to check a host."""
    # The early RETURN, not the reset inside useEffect -- the same words appear
    # in both, and the first match spans the whole fetch.
    block = FIELD[FIELD.index("\n  if (!targets.length) {"):]
    block = block[:block.index("// ---- typing")]
    assert "Check one or more hosts on the left" in block
    assert '"all"' not in block, "it still offers 'all' with nothing selected"


def test_a_host_that_could_not_be_asked_is_not_shown_as_empty():
    """"No VMs" and "could not look" are different facts about a host."""
    assert "failed" in FIELD
    # Anchored on the hook's own line: "useEffect" alone matches the import.
    block = FIELD[FIELD.index("async function load()"):FIELD.index("\n  useEffect(")]
    assert "row.ok" in block, "a failed listing is treated as an empty one"
