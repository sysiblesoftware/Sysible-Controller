import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Webserver Portal Configuration — mirrors the desktop page: status + start/stop,
// reachable URL, port, current credentials, reset/remove credentials, login
// history, and active sessions with revoke.

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

export default function Portal() {
  const [status, setStatus] = useState(null);
  const [cfg, setCfg] = useState({});
  const [history, setHistory] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [port, setPort] = useState("");
  const [cur, setCur] = useState("");
  const [nu, setNu] = useState(""); const [np, setNp] = useState(""); const [np2, setNp2] = useState("");

  function loadStatus() {
    api.portalStatus().then((s) => { setStatus(s); if (s && (s.configured_port || s.port)) setPort(String(s.configured_port || s.port)); })
      .catch((e) => setErr(e.message));
    api.controllerConfig().then(setCfg).catch(() => {});
  }
  function loadHistory() { api.portalLoginHistory().then((d) => setHistory(d.history || [])).catch((e) => setErr(e.message)); }
  function loadSessions() { api.portalSessions().then((d) => setSessions(d.sessions || [])).catch((e) => setErr(e.message)); }
  const [uploads, setUploads] = useState([]); const [downloads, setDownloads] = useState([]);
  function loadUploads() { api.portalUploads().then((d) => setUploads(d.files || [])).catch(() => {}); }
  function loadDownloads() { api.portalDownloads().then((d) => setDownloads(d.files || [])).catch(() => {}); }
  useEffect(() => { loadStatus(); loadHistory(); loadSessions(); loadUploads(); loadDownloads(); }, []);

  function fname(f) { return typeof f === "string" ? f : (f.name || f.filename || ""); }
  async function stageDownload(e) {
    const file = e.target.files[0]; if (!file) return;
    setErr(""); try { await api.portalStageDownload(file); loadDownloads(); } catch (e2) { setErr(e2.message); }
    e.target.value = "";
  }

  const running = status && (status.running || status.status === "running");
  const scheme = (status && status.scheme) || "https";
  const portNum = status && (status.port || status.configured_port);
  const reachable = `${scheme}://${cfg.address || "<controller>"}:${portNum || port || 8090}`;

  async function act(name, fn, okMsg) {
    setBusy(name); setErr(""); setMsg("");
    try { await fn(); if (okMsg) setMsg(okMsg); loadStatus(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function saveCreds() {
    setErr(""); setMsg("");
    if (np !== np2) { setErr("New passwords don't match."); return; }
    setBusy("creds");
    try { await api.portalSetCreds(nu.trim(), np, cur); setMsg("Credentials saved."); setCur(""); setNu(""); setNp(""); setNp2(""); loadStatus(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }
  async function removeCreds() {
    if (!window.confirm("Remove portal login access? Nobody can log in until new credentials are saved.")) return;
    setBusy("remove"); setErr(""); setMsg("");
    try { await api.portalRemoveCreds(cur); setMsg("Login access removed."); setCur(""); loadStatus(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }
  async function revoke(s) {
    const id = s.id ?? s.session_id;
    try { await api.portalRevokeSession(id); loadSessions(); }
    catch (e) { setErr(e.message); }
  }

  return (
    <div style={{ maxWidth: 1000 }}>
      <p className="faint" style={{ marginTop: 0 }}>
        The portal serves HTTPS using the same self-signed cert as the Sysible Controller. A host operator's
        browser will show an untrusted-certificate warning the first time — that's expected and safe to click
        through. Only start it while actively provisioning hosts, on a network you trust.
      </p>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      <fieldset className="tool-group-box"><legend>Status</legend>
        <div style={{ fontWeight: 700, color: running ? "var(--green-bright)" : "var(--text-dim)" }}>
          {running ? `Running (port ${portNum})` : "Stopped"}
        </div>
        {running && <div className="faint" style={{ marginTop: 4 }}>Reachable at: {reachable}</div>}
        <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
          <button className="btn ghost sm" onClick={loadStatus}>Refresh</button>
          <button className="btn sm" disabled={busy || running} onClick={() => act("start", api.portalStart, "Portal started.")}>
            {busy === "start" ? <span className="spin" /> : "Start Portal"}</button>
          <button className="btn sm ghost" disabled={busy || !running} onClick={() => act("stop", api.portalStop, "Portal stopped.")}>Stop Portal</button>
        </div>
        <p className="faint" style={{ marginTop: 8 }}>Tip: the one-line curl command to enroll a headless host now lives on the Host Enrollment page.</p>
      </fieldset>

      <fieldset className="tool-group-box"><legend>Portal Port</legend>
        <p className="faint" style={{ marginTop: 0 }}>Which port the portal listens on. Takes effect next start — restart it after saving if already running.</p>
        <div className="row">
          <input style={{ maxWidth: 140 }} type="number" value={port} onChange={(e) => setPort(e.target.value)} />
          <button className="btn sm" disabled={busy || !port} onClick={() => act("port", () => api.portalSetPort(Number(port)), "Port saved.")}>Save Port</button>
        </div>
      </fieldset>

      <fieldset className="tool-group-box"><legend>Current Credentials</legend>
        {status && status.credentials_configured ? (
          <div className="muted" style={{ fontSize: 13 }}>
            Username: <strong>{status.username}</strong><br />
            Last successful login: {fmt(status.last_login)}<br />
            Credentials last changed: {fmt(status.last_changed)}
          </div>
        ) : <span className="faint">No portal credentials configured yet.</span>}
      </fieldset>

      <fieldset className="tool-group-box"><legend>Reset Login Credentials</legend>
        <p className="faint" style={{ marginTop: 0 }}>Enter the current password to confirm this change.</p>
        <label className="field"><span>Current password</span><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} /></label>
        <label className="field"><span>New username</span><input value={nu} onChange={(e) => setNu(e.target.value)} /></label>
        <label className="field"><span>New password</span><input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label>
        <label className="field"><span>Confirm new password</span><input type="password" value={np2} onChange={(e) => setNp2(e.target.value)} /></label>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn sm" disabled={busy === "creds" || !nu || !np} onClick={saveCreds}>Save Credentials</button>
          <button className="btn sm danger" disabled={busy === "remove"} onClick={removeCreds}>Remove Login Access</button>
        </div>
      </fieldset>

      <fieldset className="tool-group-box"><legend>Login History</legend>
        <div className="spread" style={{ marginBottom: 8 }}>
          <span className="faint">Every login attempt against the portal account, plus credential-reset events.</span>
          <button className="btn ghost sm" onClick={loadHistory}>Refresh</button>
        </div>
        {history.length === 0 ? <div className="empty">No login history.</div> : (
          <div style={{ maxHeight: 220, overflowY: "auto" }}>
            <table>
              <thead><tr><th>Time</th><th>Event</th><th>Username</th><th>IP Address</th></tr></thead>
              <tbody>
                {history.map((h, i) => (
                  <tr key={h.id ?? i}>
                    <td className="faint mono">{fmt(h.timestamp ?? h.time ?? h.created_at)}</td>
                    <td>{h.event}</td><td>{h.username}</td>
                    <td className="faint">{h.ip || h.ip_address || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </fieldset>

      <fieldset className="tool-group-box"><legend>Active Sessions</legend>
        <div className="spread" style={{ marginBottom: 8 }}>
          <span className="faint">Host operators currently logged into the portal. Revoking one logs that browser out immediately.</span>
          <button className="btn ghost sm" onClick={loadSessions}>Refresh</button>
        </div>
        {sessions.length === 0 ? <div className="empty">No active sessions.</div> : (
          <table>
            <thead><tr><th>Logged In</th><th>Expires</th><th>IP Address</th><th></th></tr></thead>
            <tbody>
              {sessions.map((s, i) => (
                <tr key={s.id ?? s.session_id ?? i}>
                  <td className="faint mono">{fmt(s.created_at ?? s.logged_in ?? s.started_at)}</td>
                  <td className="faint mono">{fmt(s.expires_at ?? s.expires)}</td>
                  <td className="faint">{s.ip || s.ip_address || ""}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn ghost sm" onClick={() => revoke(s)}>Revoke</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </fieldset>

      <fieldset className="tool-group-box"><legend>Files Uploaded By Hosts</legend>
        <div className="spread" style={{ marginBottom: 8 }}>
          <span className="faint">Files host operators uploaded through the portal.</span>
          <button className="btn ghost sm" onClick={loadUploads}>Refresh List</button>
        </div>
        {uploads.length === 0 ? <div className="empty">No uploaded files.</div> : (
          <table>
            <thead><tr><th>File</th><th></th></tr></thead>
            <tbody>
              {uploads.map((f, i) => { const n = fname(f); return (
                <tr key={n || i}>
                  <td>{n}</td>
                  <td style={{ textAlign: "right" }}>
                    <a className="btn ghost sm" href={api.portalUploadUrl(n)}>Save To Computer</a>{" "}
                    <button className="btn ghost sm" onClick={async () => { await api.portalUploadDelete(n); loadUploads(); }}>Delete</button>
                  </td>
                </tr>
              ); })}
            </tbody>
          </table>
        )}
      </fieldset>

      <fieldset className="tool-group-box"><legend>Files Staged For Download</legend>
        <div className="spread" style={{ marginBottom: 8 }}>
          <span className="faint">Files you've staged for host operators to download via the portal.</span>
          <div className="row">
            <label className="btn ghost sm" style={{ cursor: "pointer" }}>
              Add File…<input type="file" style={{ display: "none" }} onChange={stageDownload} />
            </label>
            <button className="btn ghost sm" onClick={loadDownloads}>Refresh List</button>
          </div>
        </div>
        {downloads.length === 0 ? <div className="empty">No staged files.</div> : (
          <table>
            <thead><tr><th>File</th><th></th></tr></thead>
            <tbody>
              {downloads.map((f, i) => { const n = fname(f); return (
                <tr key={n || i}>
                  <td>{n}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn ghost sm" onClick={async () => { await api.portalDownloadDelete(n); loadDownloads(); }}>Delete</button>
                  </td>
                </tr>
              ); })}
            </tbody>
          </table>
        )}
      </fieldset>
    </div>
  );
}
