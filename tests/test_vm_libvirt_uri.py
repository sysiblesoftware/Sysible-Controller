"""Libvirt VM command builders must target the system instance.

Dispatched commands run non-interactively (no login shell), so a host's
LIBVIRT_DEFAULT_URI exported from /etc/profile.d is never in scope. Bare
`virsh` then defaults to the empty per-user qemu:///session instead of
qemu:///system where host VMs run — so "List VMs" silently returns nothing.
The builders pin qemu:///system unless an operator set the URI explicitly.
"""
import subprocess

from client import _api_containers as c


def _run(cmd, env=None):
    return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env)


def test_list_vms_pins_system_uri():
    cmd = c.cmd_list_vms()
    assert "LIBVIRT_DEFAULT_URI" in cmd
    assert "qemu:///system" in cmd
    assert "virsh list --all" in cmd


def test_vm_action_and_info_pin_system_uri():
    for cmd in (c.cmd_vm_action("start", "vm1"), c.cmd_vm_info("vm1")):
        assert "qemu:///system" in cmd
        assert "LIBVIRT_DEFAULT_URI" in cmd


def test_uri_defaults_only_when_unset(monkeypatch):
    # Isolate the assignment snippet the builders share and prove its semantics:
    # default to system when unset, but never clobber an explicit URI.
    snippet = ': "${LIBVIRT_DEFAULT_URI:=qemu:///system}"; echo "[$LIBVIRT_DEFAULT_URI]"'
    r = _run(snippet, env={})
    assert r.stdout.strip() == "[qemu:///system]"
    r = _run(snippet, env={"LIBVIRT_DEFAULT_URI": "xen:///system"})
    assert r.stdout.strip() == "[xen:///system]"


def test_missing_virsh_still_reports_clearly():
    # The preamble must still fail fast with a human message when virsh is absent,
    # regardless of the URI pinning that follows it.
    cmd = c.cmd_list_vms()
    assert "virsh (libvirt) is not installed" in cmd
