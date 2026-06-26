import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import TerminalSession from "../components/TerminalSession.jsx";

// Sysible Connect terminals: open several host shells at once as tabs. Each
// tab is an independent, persistent TerminalSession (xterm + websocket).
export default function Terminal() {
  const [hosts, setHosts] = useState([]);
  const [pick, setPick] = useState("");
  const [sessions, setSessions] = useState([]); // {id, hostId, label, status}
  const [activeId, setActiveId] = useState(null);
  const [err, setErr] = useState("");
  const nextId = useRef(1);

  useEffect(() => {
    api.hosts()
      .then((d) => { setHosts(d.hosts || []); if (d.hosts && d.hosts[0]) setPick(d.hosts[0].id); })
      .catch((e) => setErr(e.message));
  }, []);

  function openSession() {
    if (!pick) return;
    const host = hosts.find((h) => h.id === pick);
    const id = nextId.current++;
    setSessions((s) => [...s, { id, hostId: pick, label: host ? host.label : pick, status: "connecting" }]);
    setActiveId(id);
  }

  function closeSession(id) {
    setSessions((s) => s.filter((x) => x.id !== id));
    setActiveId((cur) => {
      if (cur !== id) return cur;
      const rest = sessions.filter((x) => x.id !== id);
      return rest.length ? rest[rest.length - 1].id : null;
    });
  }

  function setStatus(id, status) {
    setSessions((s) => s.map((x) => (x.id === id ? { ...x, status } : x)));
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={pick} onChange={(e) => setPick(e.target.value)} style={{ maxWidth: 320 }}>
          {hosts.length === 0 && <option value="">No hosts</option>}
          {hosts.map((h) => (
            <option key={h.id} value={h.id}>{h.label}{!h.has_agent ? " (ssh)" : ""}</option>
          ))}
        </select>
        <button className="btn" onClick={openSession} disabled={!pick}>Open Terminal</button>
        <span className="faint">Open as many hosts as you like — each is its own tab.</span>
      </div>

      {err && <div className="error-box">{err}</div>}

      {sessions.length === 0 ? (
        <div className="empty">No terminals open. Pick a host and click “Open Terminal”.</div>
      ) : (
        <>
          <div className="term-tabs">
            {sessions.map((s) => (
              <div key={s.id} className={"term-tab" + (s.id === activeId ? " active" : "")}
                   onClick={() => setActiveId(s.id)}>
                <span className={"dot " + (s.status === "connected" ? "ok" : s.status?.startsWith("error") || s.status === "closed" ? "bad" : "")} />
                <span>{s.label}</span>
                <span className="x" onClick={(e) => { e.stopPropagation(); closeSession(s.id); }}>✕</span>
              </div>
            ))}
          </div>
          {/* All sessions stay mounted; only the active one is visible. */}
          {sessions.map((s) => (
            <div key={s.id} style={{ display: s.id === activeId ? "block" : "none" }}>
              <TerminalSession
                hostId={s.hostId}
                active={s.id === activeId}
                onStatus={(st) => setStatus(s.id, st)}
              />
            </div>
          ))}
        </>
      )}
    </div>
  );
}
