"""Acting on every VM is something you can SEE, not a word you have to know.

Reported: "there is no way to shutdown all the vms from the single field."

That was true of the interface and never true of the API. cmd_vm_action has
branched on "all" since the field was written — it loops over
`virsh list --all --name` and reports each domain. The only route to it was to
type the literal word into a box whose entire visible affordance is a menu of
individual VM names, with the hint sitting in 11px grey text underneath. A
capability reachable only by knowing the magic word is not a capability the
operator has.

Two shapes, because the field has two: a menu when exactly one host is selected
and its VMs could be listed, and free text when it could not (16 hosts have no
one list to choose from — and that is precisely the case where "every VM" is
most wanted and was least reachable).
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FIELD = (REPO / "webgui" / "frontend" / "src" / "components" / "ParamField.jsx").read_text(encoding="utf-8")


def test_the_backend_really_does_take_all():
    """The premise. If this ever stopped being true the control below would be
    offering something that fails, which is worse than not offering it."""
    from client import _api_containers as c
    cmd = c.cmd_vm_action("shutdown", "all")
    assert "virsh list --all --name" in cmd, cmd
    assert "shutdown" in cmd
    # And it must not stop at the first VM that refuses.
    assert "|| rc=1" in cmd, "one failing domain aborts the rest"


@pytest.mark.parametrize("word", ["all", "ALL", "*"])
def test_the_spellings_the_hint_has_always_promised(word):
    from client import _api_containers as c
    assert "virsh list --all --name" in c.cmd_vm_action("shutdown", word)


def test_the_menu_offers_every_vm_as_a_choice():
    block = FIELD[FIELD.index("— choose a VM —"):]
    block = block[:block.index("</select>")]
    assert 'value="all"' in block, "the menu has no option for every VM"
    assert "All VMs on this host" in block


def test_the_menu_does_not_offer_it_for_a_single_vm():
    """"All VMs" next to a list of one is noise, and makes the operator wonder
    whether it means something different from the one name above it."""
    # Sliced RELATIVE to the VM menu: ParamField renders an ordinary select for
    # plain `type="select"` params earlier in the file, so an absolute index of
    # "</select>" lands before this block starts and silently yields "".
    block = FIELD[FIELD.index("— choose a VM —"):]
    block = block[:block.index("</select>")]
    assert "options.length > 1" in block, \
        "the all-VMs option shows even when there is only one VM to act on"


def test_the_free_text_field_has_a_control_too():
    """The case with no menu — several hosts — is the one where this matters
    most and was hardest to reach."""
    block = FIELD[FIELD.index("if (useManual)"):FIELD.index("return (\n    <div className=\"row\"")]
    assert re.search(r'onChange\(p\.name,\s*"all"\)', block), \
        "no control fills the field with 'all' when the menu cannot be shown"
    assert "All VMs" in block


def test_choosing_every_vm_says_how_far_it_reaches():
    """'all' across 16 selected hosts is every VM on every one of them. The
    operator should not have to infer the blast radius from a field that still
    reads like it is asking for one name."""
    block = FIELD[FIELD.index("if (useManual)"):FIELD.index("return (\n    <div className=\"row\"")]
    assert 'cur === "all"' in block, "nothing changes when every VM is selected"
    assert "targets.length" in block, "the reach is not stated in terms of the selected hosts"
