"""
The VM-name picker: the Virtual Machines tool's "name" field is a live dropdown
populated from the selected host. This locks in the contract the frontend relies
on:

  * `vm_names` is a registered, dispatchable action (so api.runTool can fetch the
    per-host list) but is HIDDEN from the tool catalog (never a button);
  * the vm_action / vm_info "name" param is a `select-remote` sourced from it;
  * cmd_list_vm_names emits the bare one-per-line domain list the picker parses.
"""
import webgui.actions as actions


def test_vm_names_is_dispatchable_but_hidden_from_catalog():
    # Registered → run_tool can dispatch it.
    assert actions.get("vm_names") is not None
    # But never rendered as a button.
    catalog_names = {a["name"] for tool in actions.catalog() for a in tool["actions"]}
    assert "vm_names" not in catalog_names


def test_vm_action_name_is_a_remote_select_sourced_from_vm_names():
    for name in ("vm_action", "vm_info"):
        spec = actions.get(name)
        p = next(pr for pr in spec.params if pr.name == "name")
        assert p.type == "select-remote"
        assert p.source == "vm_names"


def test_catalog_serializes_select_remote_source():
    cat = actions.catalog()
    va = next(a for tool in cat for a in tool["actions"] if a["name"] == "vm_action")
    p = next(pr for pr in va["params"] if pr["name"] == "name")
    assert p["type"] == "select-remote"
    assert p["source"] == "vm_names"


def test_cmd_list_vm_names_emits_bare_name_list():
    cmd = actions.api.cmd_list_vm_names()
    assert "virsh list --all --name" in cmd   # one domain per line, no header
    assert "qemu:///system" in cmd            # pinned to the system hypervisor
