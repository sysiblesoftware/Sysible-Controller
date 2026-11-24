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


def test_a_field_marked_multi_is_backed_by_something_that_takes_several():
    """The flag is a PROMISE to the browser: tick as many as you like. It used
    to be set per action, so the field could offer more than the command behind
    it could accept -- three ticked boxes arriving as one quoted name.

    vm_info was the single-value one, and because the group renders ONE field
    for the name, its narrowness decided what vm_action got too. It now takes
    several; this checks the promise and the command still agree, for every
    action, rather than re-stating today's answer for two of them.
    """
    from webgui import actions
    for name in ("vm_action", "vm_info"):
        spec = actions.get(name)
        pr = next(x for x in spec.params if x.name == "name")
        assert pr.multi is True, f"{name} narrows the field the whole group shares"
        args = {p.name: ("start" if p.name == "action" else "a-1 b-2 c-3") for p in spec.params}
        built = spec.build(args)
        assert "for vm in a-1 b-2 c-3" in built, \
            f"{name} offers a multi field but builds a command for one name:\n{built}"
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

# ---- when the checked hosts are not hypervisors ------------------------------
def test_a_host_without_libvirt_is_told_apart_from_a_failure():
    """Three arch hosts checked, and the field said "Could not list VMs on
    arch-01, arch-02, arch-03. Type a name instead." Both halves were wrong:
    _VIRSH exits with "virsh (libvirt) is not installed on this host", so the
    reason was known and thrown away -- and typing a name is useless advice
    when no checked host runs VMs at all."""
    block = FIELD[FIELD.index("// ---- typing, or nothing to list"):]
    block = block[:block.index("// ---- the list")]
    assert "noHyp" in block, "the no-hypervisor case is not told apart"
    assert "not running libvirt" in block, "it does not say what is actually wrong"
    assert "Check a host that does" in block, \
        "it still advises typing a name no checked host could match"


def test_a_real_failure_still_shows_its_reason():
    """A host that HAS libvirt and failed for some other reason must not be
    folded into the tidy message."""
    block = FIELD[FIELD.index("// ---- typing, or nothing to list"):]
    block = block[:block.index("// ---- the list")]
    assert "otherFail" in block
    assert "failed[otherFail[0]]" in block, "a real failure loses its reason"


def test_three_vms_at_once_is_what_the_field_produces():
    """The direct question: how do I shut down three at a time. Ticking three
    boxes yields three names, and _split_targets takes them."""
    from client import _api_containers as c
    cmd = c.cmd_vm_action("shutdown", "web-1 db-1 cache-1")
    assert "for vm in web-1 db-1 cache-1" in cmd, cmd
    # ...and each host only acts on the ones it has, so three VMs spread over
    # two hypervisors is one run, not two failures.
    assert "grep -Fxq" in cmd


def test_the_all_vms_control_is_gated_on_the_param_not_guessed():
    block = FIELD[FIELD.index("// ---- typing, or nothing to list"):]
    block = block[:block.index("// ---- the list")]
    assert "p.multi === true" in block, \
        "the All VMs control is offered on fields that cannot hold a list"


# ---- "if I select sysible-virt it shows them but's awful" -------------------
# The list finally appeared and was unreadable: every tickbox was stretched the
# width of the field with the VM name shoved against the far edge, and the
# tickboxes themselves were radios -- one VM at a time -- in a field whose whole
# point was picking several.
TOOLPAGE = (REPO / "webgui" / "frontend" / "src" / "views" / "ToolPage.jsx").read_text(encoding="utf-8")
CSS = (REPO / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")


def _rules(css):
    """(selector, body) for every rule, comments stripped. Good enough for a
    flat stylesheet with no nesting, which is what this one is."""
    import re
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def test_a_tickbox_is_never_collapsed_to_a_sliver():
    """`.target-chip input { width: auto }` was written to escape the
    `input, select, textarea { width: 100% }` rule -- but it ALSO beat the
    custom checkbox's own `width: 15px` (same specificity, later in the file),
    leaving a 0-wide content box with only its 1.5px borders drawn. Every host
    checkbox was a 2px sliver sitting on top of its own label.

    The base rules already opt tickboxes out of the full-width rule, so no rule
    further down may set `width` on something that can match one.
    """
    import re
    rules = _rules(CSS)
    # the two rules that legitimately SIZE a tickbox; everything after them that
    # touches width is overriding the size rather than setting it.
    canonical = ('input[type="checkbox"]', 'input[type="radio"]')
    base = max(i for i, (sel, _) in enumerate(rules) if sel in canonical)
    offenders = []
    for sel, body in rules[base + 1:]:
        # a selector that can match a tickbox: ends at a bare `input`, or names one
        if not re.search(r'(^|[\s>+~])input\s*$|input\[type="(checkbox|radio)"\]', sel):
            continue
        if re.search(r"(^|;)\s*width\s*:", body):
            offenders.append(f"{sel} {{{body.strip()}}}")
    assert not offenders, (
        "these re-introduce a width on a tickbox and will collapse or stretch it:\n  "
        + "\n  ".join(offenders))


def test_a_radio_is_sized_too():
    """Only checkboxes had been opted out of `width: 100%`. A radio took the
    full width of its field, which is exactly what the VM list looked like."""
    sizes = [b for s, b in _rules(CSS) if s == 'input[type="radio"]']
    assert sizes, 'input[type="radio"] has no rule, so it inherits width: 100%'
    assert "width" in sizes[0], "the radio rule must pin a width or the 100% above wins"


def test_a_field_two_actions_share_is_as_wide_as_the_widest():
    """One field is rendered for a param several actions share, so a first-wins
    merge let whichever action was registered first decide what everyone got.
    The VM field came out single-select for the whole group because one action
    on it took a single name."""
    import re
    body = TOOLPAGE[TOOLPAGE.index("const sharedParams"):]
    body = body[:body.index("}, [acts])")]
    body = "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))
    assert "if (!seen.has(p.name)) seen.set(p.name, p)" not in body, \
        "first-wins is back: a narrow action silently narrows the shared field"
    assert re.search(r"multi:\s*had\.multi === true \|\| p\.multi === true", body), \
        "the merged field must take the WIDER multi of the actions sharing it"


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_asking_about_three_vms_asks_about_three(tmp_path):
    """vm_info took exactly one name, which is what made the shared field
    single-select. Widening the field is only safe if the thing behind it can
    take what the field now holds."""
    from client import _api_containers as c
    bin_dir, log = _fake_virsh(tmp_path, ["web-1", "db-1", "cache-1"])
    cmd = c.cmd_vm_info("web-1 db-1 cache-1")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    p = subprocess.run([SH, "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    acted = [a for a in log.read_text().split("\n") if a.strip()] if log.exists() else []
    assert p.returncode == 0, p.stdout + p.stderr
    assert acted == ["dominfo web-1", "dominfo db-1", "dominfo cache-1"], acted


@pytest.mark.skipif(not SH, reason="no sh on this machine")
def test_vm_info_skips_the_vms_this_host_does_not_have(tmp_path):
    """Same rule as vm_action: a host answers for what it owns and says which it
    skipped, rather than erroring once per VM that was never its to describe."""
    from client import _api_containers as c
    bin_dir, log = _fake_virsh(tmp_path, ["web-1"])
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    p = subprocess.run([SH, "-c", c.cmd_vm_info("web-1 elsewhere-1")],
                       capture_output=True, text=True, env=env, timeout=60)
    acted = [a for a in log.read_text().split("\n") if a.strip()] if log.exists() else []
    assert acted == ["dominfo web-1"], acted
    assert "not on this host: elsewhere-1" in p.stdout
    assert p.returncode == 0, "it had one of them, so it is not a failure"
