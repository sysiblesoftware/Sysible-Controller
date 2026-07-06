import React, { useEffect, useRef, useState } from "react";
import TerminalSession from "./TerminalSession.jsx";
import FileTransfer from "./FileTransfer.jsx";
import { api } from "../api.js";

// Full-window single terminal, loaded when the SPA is opened with ?term=<hostId>
// in a pop-out window — each terminal opens in its own window. Relies on the
// same session cookie (same origin). Carries the same toolbar the inline dock
// had (sudo, interrupt, find, zoom, save, clear), plus per-terminal file
// transfer (upload/download to/from this host), so a popped-out terminal is a
// first-class shell, not a stripped-down one.
export default function StandaloneTerminal({ hostId, label }) {
  const handle = useRef(null);
  const [find, setFind] = useState("");
  const [status, setStatus] = useState("connecting");
  const [showXfer, setShowXfer] = useState(false);
  // Per-account opt-in (granted by a superuser) for the Send Sudo Password
  // button. Off by default; the server also enforces this on the ws side.
  const [canSudo, setCanSudo] = useState(false);
  useEffect(() => { api.me().then((d) => setCanSudo(!!d.sudo_connect)).catch(() => {}); }, []);
  useEffect(() => { document.title = `Sysible — ${label || hostId}`; }, [label, hostId]);

  const act = (fn) => () => { if (handle.current) fn(handle.current); };

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column",
                  background: "var(--bg)", color: "var(--text)" }}>
      <div className="term-toolbar" style={{ margin: 0, padding: "8px 12px",
             background: "var(--panel)", borderBottom: "1px solid var(--border)" }}>
        <span style={{ color: "var(--text)", fontSize: 13, marginRight: "auto",
                       display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span className={"dot " + (status === "connected" ? "ok"
            : (status?.startsWith("error") || status === "closed") ? "bad" : "")}
            role="img" title={`Connection: ${status}`} aria-label={`Connection: ${status}`} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label || hostId}</span>
        </span>
        <button className={"btn sm" + (showXfer ? "" : " ghost")} onClick={() => setShowXfer((v) => !v)}
                title="Upload a file to / download a file from this host">
          Files {showXfer ? "▴" : "▾"}
        </button>
        <button className="btn sm" disabled={!canSudo} onClick={act((h) => h.sendSudo())}
                title={canSudo
                  ? "Type your stored sudo password + Enter into this shell"
                  : "Disabled: a superuser must grant your account 'Sudo on Connect' (Settings → Administrators)."}>
          Send Sudo Password
        </button>
        <button className="btn ghost sm" onClick={act((h) => h.sendCtrlC())} title="Interrupt the running command">Send Ctrl+C</button>
        <input className="term-find" placeholder="Find…" value={find}
               onChange={(e) => setFind(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") act((h) => h.find(find, e.shiftKey))(); }} />
        <button className="btn ghost sm" onClick={act((h) => h.find(find, false))} title="Find next (Enter)">▾</button>
        <button className="btn ghost sm" onClick={act((h) => h.find(find, true))} title="Find previous (Shift+Enter)">▴</button>
        <button className="btn ghost sm" onClick={act((h) => h.saveOutput())}>Save Output</button>
        <button className="btn ghost sm" onClick={act((h) => h.zoom(-1))} title="Smaller font">A−</button>
        <button className="btn ghost sm" onClick={act((h) => h.zoom(1))} title="Larger font">A+</button>
        <button className="btn ghost sm" onClick={act((h) => h.clear())}>Clear</button>
      </div>
      {showXfer && (
        <div style={{ padding: "10px 12px", background: "var(--panel)",
                      borderBottom: "1px solid var(--border)" }}>
          <FileTransfer host={{ id: hostId, label: label || hostId }} />
        </div>
      )}
      <div style={{ flex: 1, minHeight: 0, padding: 6 }}>
        <TerminalSession ref={handle} hostId={hostId} label={label} active={true} onStatus={setStatus} />
      </div>
    </div>
  );
}
