import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Full-width operational warning banner shown above every view (superuser-only).
// Surfaces the outage class that otherwise looks like "everything silently
// stopped working" — a regenerated/mismatched controller TLS cert that every
// agent pinned against (agents can't check in until re-deployed), and the
// fleet-wide symptom of a high fraction of hosts going quiet at once. Renders
// NOTHING when healthy or unresolved, so a clean controller shows no chrome.
// Polls on mount and every 60s; dismissals last for the session per warning id.
const SEV = {
  critical: { bg: "#3a1620", border: "#c0392b", fg: "#ffd7d0", icon: "⛔" },
  warning:  { bg: "#332711", border: "#e0a83a", fg: "#ffe6b0", icon: "⚠" },
};

export default function HealthBanner() {
  const [warnings, setWarnings] = useState([]);
  const [dismissed, setDismissed] = useState(() => {
    try { return new Set(JSON.parse(sessionStorage.getItem("sysible_health_dismissed") || "[]")); }
    catch { return new Set(); }
  });

  const check = useCallback(() => {
    api.healthWarnings()
      .then((r) => setWarnings(Array.isArray(r?.warnings) ? r.warnings : []))
      .catch(() => { /* stay silent — a warning banner must never be the thing that breaks */ });
  }, []);

  useEffect(() => {
    check();
    const t = setInterval(check, 60_000);
    return () => clearInterval(t);
  }, [check]);

  const dismiss = (id) => {
    setDismissed((prev) => {
      const next = new Set(prev); next.add(id);
      try { sessionStorage.setItem("sysible_health_dismissed", JSON.stringify([...next])); } catch { /* ignore */ }
      return next;
    });
  };

  const shown = warnings.filter((w) => w && w.id && !dismissed.has(w.id));
  if (shown.length === 0) return null;

  return (
    <div className="health-banners" style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
      {shown.map((w) => {
        const s = SEV[w.severity] || SEV.warning;
        return (
          <div key={w.id} role="alert"
            style={{ background: s.bg, border: `1px solid ${s.border}`, borderRadius: 8,
                     padding: "10px 12px", display: "flex", gap: 12, alignItems: "flex-start", color: s.fg }}>
            <span aria-hidden style={{ fontSize: 16, lineHeight: "20px" }}>{s.icon}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 700, lineHeight: "20px" }}>{w.title}</div>
              {w.detail && <div style={{ fontSize: 13, marginTop: 2, opacity: 0.95 }}>{w.detail}</div>}
              {w.hint && <div style={{ fontSize: 12.5, marginTop: 4, opacity: 0.85 }}><strong>Fix:</strong> {w.hint}</div>}
            </div>
            <button className="iconbtn" title="Dismiss for this session" onClick={() => dismiss(w.id)}
              style={{ color: s.fg, flex: "0 0 auto" }}>✕</button>
          </div>
        );
      })}
    </div>
  );
}
