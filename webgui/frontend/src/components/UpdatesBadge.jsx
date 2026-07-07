import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Compact "Updates available" pill for the dashboard header (superuser-only —
// /api/update-status is superuser-gated and does a live git fetch, so it's
// fetched async/non-blocking). Renders NOTHING until it resolves and only when
// there's actually an update, so a healthy header stays clean. Clicking opens
// Settings → Software updates. Auto-checks on mount; also polls hourly so a
// long-lived session eventually notices a new release without a reload.
export default function UpdatesBadge({ onOpen }) {
  const [avail, setAvail] = useState(null);

  const check = useCallback(() => {
    api.updateStatus().then(setAvail).catch(() => { /* stay silent on error */ });
  }, []);
  useEffect(() => {
    check();
    const t = setInterval(check, 3600_000);   // re-check hourly
    return () => clearInterval(t);
  }, [check]);

  if (!avail) return null;
  const c = avail.controller || {};
  const a = avail.agents || {};
  const ctrlBehind = !!(c.checked && c.available);
  const agentsBehind = (a.outdated_count || 0) > 0;
  if (!ctrlBehind && !agentsBehind) return null;

  const bits = [];
  if (ctrlBehind) bits.push(`controller ${c.behind} commit${c.behind === 1 ? "" : "s"} behind`);
  if (agentsBehind) bits.push(`${a.outdated_count} agent${a.outdated_count === 1 ? "" : "s"} on an older build`);

  return (
    <button className="btn sm" onClick={onOpen}
      title={`Updates available — ${bits.join("; ")}. Open Software updates.`}
      style={{ borderColor: "#e0a83a", color: "#e0a83a", whiteSpace: "nowrap",
               display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span aria-hidden style={{ fontSize: 13, lineHeight: 1 }}>⬆</span>
      Updates available
      <span style={{ fontSize: 11, fontWeight: 700, background: "#e0a83a", color: "#1a2130",
                     borderRadius: 9, padding: "0 6px", lineHeight: "16px" }}>
        {(ctrlBehind ? 1 : 0) + (agentsBehind ? 1 : 0)}
      </span>
    </button>
  );
}
