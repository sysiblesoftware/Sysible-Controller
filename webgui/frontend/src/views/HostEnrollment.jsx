import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Host Enrollment: hand out the agent bundle / enroll
// token and see the enrolled agent fleet.
export default function HostEnrollment() {
  const [agents, setAgents] = useState([]);
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  function load() {
    api.agents().then((d) => setAgents(d.agents || [])).catch((e) => setErr(e.message));
  }
  useEffect(() => { load(); }, []);

  async function genToken() {
    setBusy("token"); setErr("");
    try {
      const r = await api.enrollToken();
      setToken(r.token || r.enroll_token || JSON.stringify(r));
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
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
          <thead><tr><th>Host</th><th>Address</th><th>Environment</th><th>Last seen</th></tr></thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.host_id || a.id || a.label}>
                <td style={{ fontWeight: 600 }}>{a.label || a.host_id || a.name}</td>
                <td className="faint">{a.address || a.ip || ""}</td>
                <td>{a.environment ? <span className="badge">{a.environment}</span> : <span className="faint">—</span>}</td>
                <td className="faint mono">{a.last_seen || a.last_heartbeat || a.updated_at || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
