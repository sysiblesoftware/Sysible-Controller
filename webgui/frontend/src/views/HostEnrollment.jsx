import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Host Enrollment: hand out the agent bundle / enroll
// token and see the enrolled agent fleet.

// Format a "last seen" value (epoch seconds, ms, or ISO string) for display.
function fmtSeen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v);
    if (n < 1e12) n *= 1000; // seconds -> ms
    d = new Date(n);
  } else {
    d = new Date(v);
  }
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

export default function HostEnrollment() {
  const [agents, setAgents] = useState([]);
  const [envs, setEnvs] = useState([]);
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  function load() {
    api.agents().then((d) => setAgents(d.agents || [])).catch((e) => setErr(e.message));
    api.environments().then((d) => setEnvs(d.environments || [])).catch(() => {});
  }
  useEffect(() => { load(); }, []);

  const NEW_ENV = "+ New environment…";
  async function assignEnv(a, value) {
    const id = a.host_id || a.id;
    setErr("");
    let env = value;
    if (value === NEW_ENV) {
      env = (window.prompt("New environment name:") || "").trim();
      if (!env) return;
      try { await api.createEnvironment(env); } catch (e) { setErr(e.message); return; }
    }
    try { await api.setHostEnvironment(id, env); load(); }
    catch (e) { setErr(e.message); }
  }

  async function genToken() {
    setBusy("token"); setErr("");
    try {
      const r = await api.enrollToken();
      setToken(r.token || r.enroll_token || JSON.stringify(r));
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function disenroll(a) {
    const id = a.host_id || a.id;
    const label = a.label || a.host_id || a.name;
    if (!window.confirm(`Disenroll ${label}? Its agent keeps running but stops being managed.`)) return;
    setErr("");
    try { await api.removeHost(id); load(); }
    catch (e) { setErr(e.message); }
  }

  return (
    <div>
      {err && <div className="error-box">{err}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <strong>Enroll a new agent host</strong>
        <p className="faint" style={{ marginTop: 4 }}>
          Download the agent bundle onto the target host and run its installer, or use a
          single-use enrollment token. The bundle is pre-configured for this controller.
        </p>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <a className="btn sm" href={api.agentBundleUrl()}>Download Agent Bundle</a>
          <button className="btn sm ghost" disabled={busy === "token"} onClick={genToken}>
            {busy === "token" ? <span className="spin" /> : "Generate Enrollment Token"}
          </button>
        </div>
        {token && (
          <>
            <div className="faint" style={{ marginTop: 10 }}>Single-use token (expires shortly):</div>
            <div className="cmd-preview">{token}</div>
          </>
        )}
      </div>

      <div className="spread" style={{ marginBottom: 8 }}>
        <strong>Enrolled agents ({agents.length})</strong>
        <button className="btn ghost sm" onClick={load}>Refresh</button>
      </div>
      {agents.length === 0 ? (
        <div className="empty">No agents enrolled yet.</div>
      ) : (
        <table>
          <thead><tr><th>Host</th><th>Address</th><th>Environment</th><th>Last seen</th><th></th></tr></thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.host_id || a.id || a.label}>
                <td style={{ fontWeight: 600 }}>{a.hostname || a.label || a.host_id || a.name}</td>
                <td className="faint">{a.address || a.ip || ""}</td>
                <td>
                  <select value={a.environment || ""} onChange={(e) => assignEnv(a, e.target.value)}
                          style={{ maxWidth: 180 }}>
                    <option value="">(unassigned)</option>
                    {envs.map((e) => <option key={e} value={e}>{e}</option>)}
                    <option value={NEW_ENV}>{NEW_ENV}</option>
                  </select>
                </td>
                <td className="faint mono">{fmtSeen(a.last_seen ?? a.last_heartbeat ?? a.updated_at)}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn ghost sm" onClick={() => disenroll(a)}>Disenroll</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
