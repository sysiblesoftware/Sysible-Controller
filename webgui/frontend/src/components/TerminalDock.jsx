import React, { useRef, useState, useImperativeHandle, forwardRef } from "react";
import TerminalSession from "./TerminalSession.jsx";

// Multi-terminal dock: several concurrent host shells as tabs, each an
// independent persistent TerminalSession. A toolbar drives the active session
// (send stored sudo password, font zoom, clear). Tabs can pop out to their own
// browser window. Inactive sessions are hidden with `visibility` so they keep
// a layout box and stay sized.
const TerminalDock = forwardRef(function TerminalDock(_props, ref) {
  const [sessions, setSessions] = useState([]); // {id, hostId, label, status}
  const [activeId, setActiveId] = useState(null);
  const nextId = useRef(1);
  const handles = useRef({}); // id -> imperative handle

  function open(hostId, label) {
    const id = nextId.current++;
    setSessions((s) => [...s, { id, hostId, label, status: "connecting" }]);
    setActiveId(id);
  }
  useImperativeHandle(ref, () => ({ open }));

  function close(id) {
    delete handles.current[id];
    setSessions((prev) => {
      const rest = prev.filter((x) => x.id !== id);
      setActiveId((cur) => (cur === id ? (rest.length ? rest[rest.length - 1].id : null) : cur));
      return rest;
    });
  }
  function setStatus(id, status) {
    setSessions((s) => s.map((x) => (x.id === id ? { ...x, status } : x)));
  }
  function popOut(s) {
    window.open(`/?term=${encodeURIComponent(s.hostId)}&label=${encodeURIComponent(s.label)}`,
      `sysible_term_${s.hostId}_${s.id}`, "width=900,height=600");
  }
  const act = (fn) => () => { const h = handles.current[activeId]; if (h) fn(h); };

  if (sessions.length === 0) {
    return <div className="empty">No terminals open. Double-click a host, or use “Open Terminal”.</div>;
  }

  return (
    <div>
      <div className="term-tabs">
        {sessions.map((s) => (
          <div key={s.id} className={"term-tab" + (s.id === activeId ? " active" : "")}
               onClick={() => setActiveId(s.id)}>
            <span className={"dot " + (s.status === "connected" ? "ok"
              : (s.status?.startsWith("error") || s.status === "closed") ? "bad" : "")} />
            <span>{s.label}</span>
            <span className="x" title="Pop out to a new window" onClick={(e) => { e.stopPropagation(); popOut(s); }}>⤢</span>
            <span className="x" title="Close" onClick={(e) => { e.stopPropagation(); close(s.id); }}>✕</span>
          </div>
        ))}
      </div>

      <div className="term-toolbar">
        <button className="btn sm" onClick={act((h) => h.sendSudo())} title="Type your stored sudo password + Enter into this shell">
          Send Sudo Password
        </button>
        <button className="btn ghost sm" onClick={act((h) => h.zoom(-1))} title="Smaller font">A−</button>
        <button className="btn ghost sm" onClick={act((h) => h.zoom(1))} title="Larger font">A+</button>
        <button className="btn ghost sm" onClick={act((h) => h.clear())}>Clear</button>
      </div>

      <div style={{ position: "relative", height: "58vh" }}>
        {sessions.map((s) => (
          <div key={s.id} style={{ position: "absolute", inset: 0, visibility: s.id === activeId ? "visible" : "hidden" }}>
            <TerminalSession ref={(h) => { if (h) handles.current[s.id] = h; }}
                             hostId={s.hostId} label={s.label} active={s.id === activeId}
                             onStatus={(st) => setStatus(s.id, st)} />
          </div>
        ))}
      </div>
    </div>
  );
});

export default TerminalDock;
