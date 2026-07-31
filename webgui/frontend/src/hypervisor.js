// Hypervisor awareness for reboot / power-off warnings.
//
// The agent reports, on heartbeat, whether a host is a VM HOST (running guests)
// and how many guests are running (see _detect_hypervisor in host_agent/agent.py).
// The console surfaces that as `hypervisor` (a short role) + `vms` (count) on the
// host inventory, the per-host posture, and the fleet-health reading. These
// helpers turn that into human labels and a warning shown before any action that
// would take the host — and therefore every VM on it — down.

const ROLE_LABEL = {
  kvm: "KVM hypervisor",
  qemu: "QEMU hypervisor",
  proxmox: "Proxmox VE host",
  "xen-dom0": "Xen dom0 (control domain)",
  virtualbox: "VirtualBox host",
  vmware: "VMware host",
};

// A host record with { hypervisor, vms } (any of the console's host-shaped
// objects). Returns true when it looks like a VM host.
export function isHypervisor(h) {
  return !!(h && h.hypervisor);
}

export function hypervisorLabel(role) {
  return ROLE_LABEL[role] || "hypervisor";
}

// Short badge text, e.g. "KVM hypervisor · 4 VMs" (or "· VM host" when the guest
// count is unknown/zero). Returns "" for non-hypervisors.
export function hypervisorBadge(h) {
  if (!isHypervisor(h)) return "";
  const n = Number(h.vms);
  const count = Number.isFinite(n) && n > 0 ? `${n} VM${n === 1 ? "" : "s"}` : "VM host";
  return `${hypervisorLabel(h.hypervisor)} · ${count}`;
}

// Warning line for a disruptive action on ONE host (reboot / power off). Empty
// string when the host isn't a hypervisor, so callers can just prepend it.
// `verb` defaults to "Rebooting".
export function hypervisorActionWarning(h, verb = "Rebooting") {
  if (!isHypervisor(h)) return "";
  const n = Number(h.vms);
  const guests = Number.isFinite(n) && n > 0
    ? `${n} running VM${n === 1 ? "" : "s"}`
    : "its VMs";
  return `⚠ ${h.label || "This host"} is a ${hypervisorLabel(h.hypervisor)}. ` +
    `${verb} it will take down ${guests}.`;
}

// Warning for a FLEET action over many host records. Returns "" if none of them
// are hypervisors; otherwise a summary naming the VM hosts and total guest count.
export function hypervisorFleetWarning(hosts, verb = "Rebooting") {
  const hyps = (hosts || []).filter(isHypervisor);
  if (hyps.length === 0) return "";
  const totalVms = hyps.reduce((n, h) => n + (Number(h.vms) > 0 ? Number(h.vms) : 0), 0);
  const names = hyps.map((h) => h.label).filter(Boolean).join(", ");
  const vmPart = totalVms > 0 ? ` running ${totalVms} VM${totalVms === 1 ? "" : "s"} in total` : "";
  return `⚠ This includes ${hyps.length} hypervisor host${hyps.length === 1 ? "" : "s"}` +
    `${vmPart} (${names}). ${verb} them will take every guest VM down with them.`;
}
