import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Webserver Portal Configuration: run the host-facing portal (agent downloads
// + file transfers), set its port, and manage its login credentials.
export default function Portal() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [port, setPort] = useState("");
  const [pu, setPu] = useState(""); const [pp, setPp] = useState(""); const [cur, setCur] = useState("");

  function load() {
    api.portalStatus().then((s) => { setStatus(s); if (s && s.port) setPort(String(s.port)); })
      .catch((e) => setErr(e.message));
  }
  useEffect(() => { load(); }, []);

  async function run(name, fn) {
    setBusy(name); setErr(""); setMsg("");
    try { await fn(); load(); setMsg("Done."); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  const running = status && (status.running || status.status === "running");

  return (
    <div style={{ maxWidth: 560 }}>
      {err && <div className="error-box">{err}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="spread">
          <div>
            <strong>Portal</strong>{" "}
            <span className={"badge " + (running ? "green" : "")}>{running ? "running" : "stopped"}</span>
            {status && status.port ? <span className="faint"> · port {status.port}</span> : null}
          </div>
          <div className="row">
            <button className="btn sm" disabled={busy || running} onClick={() => run("start", api.portalStart)}>
              {busy === "start" ? <span className="spin" /> : "Start"}
            </button>
            <button className="btn sm ghost" disabled={busy || !running} onClick={() => run("stop", api.portalStop)}>Stop</button>
          </div>
        </div>
        {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <strong>Port</strong>
        <div className="row" style={{ marginTop: 10 }}>
          <input style={{ maxWidth: 140 }} type="number" value={port} onChange={(e) => setPort(e.target.value)} />
          <button className="btn sm" disabled={busy || !port} onClick={() => run("port", () => api.portalSetPort(Number(port)))}>Save Port</button>
        </div>
      </div>

      <div className="card">
        <strong>Portal Login Credentials</strong>
        <p className="faint" style={{ marginTop: 4 }}>The username/password a host operator types into the portal.</p>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <input style={{ flex: 1, minWidth: 130 }} placeholder="Username" value={pu} onChange={(e) => setPu(e.target.value)} />
          <input style={{ flex: 1, minWidth: 130 }} type="password" placeholder="New password" value={pp} onChange={(e) => setPp(e.target.value)} />
          <input style={{ flex: 1, minWidth: 130 }} type="password" placeholder="Current (if changing)" value={cur} onChange={(e) => setCur(e.target.value)} />
          <button className="btn sm" disabled={busy || !pu || !pp}
                  onClick={() => run("creds", () => api.portalSetCreds(pu, pp, cur))}>Save Credentials</button>
        </div>
      </div>
    </div>
  );
}
