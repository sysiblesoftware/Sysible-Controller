import React from "react";
import TerminalSession from "./TerminalSession.jsx";

// Full-window single terminal, loaded when the SPA is opened with ?term=<hostId>
// in a pop-out window — mirrors the desktop opening each terminal in its own
// window. Relies on the same session cookie (same origin).
export default function StandaloneTerminal({ hostId, label }) {
  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "#000" }}>
      <div style={{ padding: "6px 12px", color: "#e6edf3", fontSize: 13,
                    borderBottom: "1px solid #283142", background: "#151a22" }}>
        Sysible — {label || hostId}
      </div>
      <div style={{ flex: 1, minHeight: 0, padding: 6 }}>
        <TerminalSession hostId={hostId} active={true} onStatus={() => {}} />
      </div>
    </div>
  );
}
