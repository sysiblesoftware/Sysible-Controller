import React from "react";
import { isHypervisor, hypervisorBadge, hypervisorLabel } from "../hypervisor.js";

// A small amber "VM host" marker shown wherever a hypervisor host is listed, so
// it's obvious at a glance that rebooting / powering it off is disruptive to its
// guests. Renders nothing for non-hypervisors.
//   compact — just the 🖥 icon + guest count (for dense rows / pickers).
//   default — the labelled chip, e.g. "🖥 KVM hypervisor · 4 VMs".
const AMBER = "#e0a83a";

export default function HypervisorBadge({ host, compact = false, style }) {
  if (!isHypervisor(host)) return null;
  const full = hypervisorBadge(host);            // "KVM hypervisor · 4 VMs"
  const n = Number(host.vms);
  const title = `${hypervisorLabel(host.hypervisor)}${Number.isFinite(n) && n > 0
    ? ` running ${n} VM${n === 1 ? "" : "s"}` : ""} — rebooting or powering it off takes its guests down.`;
  if (compact) {
    return (
      <span title={title}
            style={{ fontSize: 11, color: AMBER, whiteSpace: "nowrap", flex: "0 0 auto", ...style }}>
        🖥{Number.isFinite(n) && n > 0 ? ` ${n}` : ""}
      </span>
    );
  }
  return (
    <span title={title}
          style={{ fontSize: 11.5, fontWeight: 600, color: AMBER, border: `1px solid ${AMBER}`,
                   borderRadius: 10, padding: "1px 8px", whiteSpace: "nowrap", ...style }}>
      🖥 {full}
    </span>
  );
}
