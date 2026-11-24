"""Deleting a VM, and the verb that does not.

    "I should be able to delete the VMs from the VM panel"
    "The destroy action doesn't actually destroy the VM"

Both are the same gap seen from two sides. The panel could start, stop,
force-off and inspect a VM but never remove one, and the option that READS
like removal -- `destroy` -- is libvirt's word for a force power-off. The verb
that deletes a domain is `undefine`, which was not exposed at all.

So the panel gets a real Delete VM, and `destroy` stops claiming to be one.
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
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(not SH, reason="no sh on this machine")


def _virsh(tmp_path, domains, running=(), nvram=()):
    """A virsh with real `domblklist --details` columns, so the disk listing is
    exercised against the layout it actually has to parse."""
    b = tmp_path / "bin"; b.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "did"
    run = " ".join(shlex.quote(r) for r in running) or "__none__"
    nv = " ".join(shlex.quote(r) for r in nvram) or "__none__"
    names = " ".join(shlex.quote(d) for d in domains) or "''"
    (b / "virsh").write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  list) printf '%s\\n' {names}; exit 0;;\n"
        "  domstate)\n"
        f"     for r in {run}; do [ \"$2\" = \"$r\" ] && {{ echo running; exit 0; }}; done\n"
        "     echo 'shut off'; exit 0;;\n"
        "  domblklist)\n"
        "     echo ' Type   Device   Target   Source'\n"
        "     echo ' file   disk     vda      /images/'\"$2\"'.qcow2'\n"
        "     echo ' file   cdrom    sda      -'\n"
        "     exit 0;;\n"
        "  undefine)\n"
        "     vm=$2; shift 2\n"
        f"     for u in {nv}; do\n"
        "       if [ \"$vm\" = \"$u\" ] && ! echo \"$@\" | grep -q nvram; then\n"
        "         echo 'error: cannot delete inactive domain with nvram modules' >&2; exit 1\n"
        "       fi\n"
        "     done\n"
        "     echo \"undefine $vm $@\" >> " + shlex.quote(str(log)) + "\n"
        "     echo \"Domain '$vm' has been undefined\"; exit 0;;\n"
        "esac\nexit 0\n")
    (b / "virsh").chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ.get('PATH','/usr/bin:/bin')}"}, log


def _run(cmd, env, log):
    p = subprocess.run([SH, "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    did = [x for x in (log.read_text().split("\n") if log.exists() else []) if x.strip()]
    return p.returncode, p.stdout + p.stderr, did


def test_a_vm_is_actually_removed(tmp_path):
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["vm-1", "vm-2"])
    rc, out, did = _run(c.cmd_vm_delete("vm-1"), env, log)
    assert rc == 0, out
    assert did == ["undefine vm-1 "], did
    assert "deleted" in out


def test_a_running_domain_is_refused_not_half_deleted(tmp_path):
    """THE case this has to get right. `undefine` does not stop a running
    domain -- it removes the definition and leaves it running as a TRANSIENT
    domain, which disappears at the next host reboot with no config to bring
    it back. Quietly doing that is worse than refusing."""
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["vm-1"], running=["vm-1"])
    rc, out, did = _run(c.cmd_vm_delete("all"), env, log)
    assert did == [], "a running domain was undefined anyway"
    assert rc != 0, "skipping every VM was reported as success"
    assert "stop it first" in out, "the refusal does not say what to do"


def test_one_running_does_not_block_the_others(tmp_path):
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["a-1", "busy-1", "z-1"], running=["busy-1"])
    rc, out, did = _run(c.cmd_vm_delete("all"), env, log)
    assert did == ["undefine a-1 ", "undefine z-1 "], did
    assert rc != 0, "a skipped VM must not read as a clean sweep"


def test_disks_are_kept_unless_asked_for(tmp_path):
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["vm-1"])
    _, out, did = _run(c.cmd_vm_delete("vm-1"), env, log)
    assert "--remove-all-storage" not in did[0], "disks were deleted without being asked for"
    assert "disks:" not in out, "it lists disks it is not deleting"


def test_deleting_disks_names_them_first(tmp_path):
    """Afterwards there is no domain left to ask what it had."""
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["vm-1"])
    rc, out, did = _run(c.cmd_vm_delete("vm-1", True), env, log)
    assert "--remove-all-storage" in did[0], did
    assert "/images/vm-1.qcow2" in out, "the disks being deleted are not named"
    assert "cdrom" not in out and " sda" not in out, "it lists a cdrom as a disk to delete"


def test_a_uefi_domain_is_retried_with_nvram(tmp_path):
    """Without --nvram it fails with an error most people read as permissions."""
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["uefi-1"], nvram=["uefi-1"])
    rc, out, did = _run(c.cmd_vm_delete("uefi-1"), env, log)
    assert rc == 0, out
    assert did == ["undefine uefi-1 --nvram"], did


def test_a_name_no_host_has_is_not_a_silent_success(tmp_path):
    from client import _api_containers as c
    env, log = _virsh(tmp_path, ["vm-1"])
    rc, out, did = _run(c.cmd_vm_delete("elsewhere-1"), env, log)
    assert did == [] and rc != 0
    assert "not on this host" in out


# ---- the action, and the verb that misled ----------------------------------
def test_the_panel_offers_it():
    from webgui import actions
    a = actions.get("vm_delete")
    assert a.danger is True, "deleting a VM runs with no confirmation"
    src = (REPO / "webgui" / "actions.py").read_text(encoding="utf-8")
    grp = src[src.index('"Containers & VMs": ['):]
    grp = grp[:grp.index("],\n")]
    assert "vm_delete" in grp, "the action exists but is not on the VM panel"


def test_deleting_the_disks_is_opt_in():
    from webgui import actions
    pr = next(p for p in actions.get("vm_delete").params if p.name == "storage")
    assert pr.type == "checkbox" and pr.default is False, \
        "deleting a VM takes its disks with it by default"


def test_destroy_no_longer_claims_to_delete():
    """Reported as "the destroy action doesn't actually destroy the VM". It
    does not: it is a force power-off. The VALUE still has to be `destroy` --
    that is what goes to virsh, and saved schedules hold it -- so only the
    label changes."""
    from webgui import actions
    pr = next(p for p in actions.get("vm_action").params if p.name == "action")
    assert "destroy" in pr.options, "the value sent to virsh must not change"
    shown = pr.option_labels.get("destroy", "destroy")
    assert shown != "destroy", "the misleading word is still what the UI shows"
    assert "force off" in shown.lower(), f"the label does not say what it does: {shown!r}"
    # and the panel now has a real delete to point at
    assert actions.get("vm_delete") is not None


def test_the_labels_reach_the_browser_and_the_value_does_not_change():
    src = (REPO / "webgui" / "actions.py").read_text(encoding="utf-8")
    assert '"option_labels": pr.option_labels' in src, "the catalog drops the labels"
    field = (REPO / "webgui" / "frontend" / "src" / "components" / "ParamField.jsx").read_text(encoding="utf-8")
    sel = field[field.index('p.type === "select"'):]
    sel = sel[:sel.index("</select>")]
    assert "value={o}" in sel, "the option's VALUE must stay the raw one"
    assert "option_labels" in sel, "the UI does not use the label"
