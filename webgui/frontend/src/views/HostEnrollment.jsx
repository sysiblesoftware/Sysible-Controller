import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Host Enrollment — agent bundle (download + curl), enrolled hosts grouped by
// environment with multi-select, environment assignment, sudo policy, AND the
// full Webserver Portal administration (status/port/credentials/sessions/files),
// since the portal exists to serve enrollment. One page, mirrors the desktop.

function fmtSeen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}
const fmtTime = fmtSeen;

const NEW_ENV = "+ New environment…";

export default function HostEnrollment() {
  const [agents, setAgents] = useState([]);
  const [envs, setEnvs] = useState([]);
  const [sudoDefaults, setSudoDefaults] = useState({});
  const [edition, setEdition] = useState({});
  const [portal, setPortal] = useState({});
  const [cfg, setCfg] = useState({});
  const [checked, setChecked] = useState([]);
  const [collapsed, setCollapsed] = useState({});
  const [assignEnv, setAssignEnv] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [copied, setCopied] = useState(false);
  const [portPort, setPortPort] = useState("");
  const [portalBusy, setPortalBusy] = useState("");
  // Portal administration
  const [history, setHistory] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [uploads, setUploads] = useState([]);
  const [downloads, setDownloads] = useState([]);
  const [cur, setCur] = useState(""); const [nu, setNu] = useState("");
  const [np, setNp] = useState(""); const [np2, setNp2] = useState("");

  function loadPortal() {
    api.portalStatus().then((s) => {
      setPortal(s || {});
      const p = s && (s.configured_port || s.port);
      if (p) setPortPort(String(p));
    }).catch(() => {});
  }
  function loadHistory() { api.portalLoginHistory().then((d) => setHistory(d.history || [])).catch(() => {}); }
  function loadSessions() { api.portalSessions().then((d) => setSessions(d.sessions || [])).catch(() => {}); }
  function loadUploads() { api.portalUploads().then((d) => setUploads(d.files || [])).catch(() => {}); }
  function loadDownloads() { api.portalDownloads().then((d) => setDownloads(d.files || [])).catch(() => {}); }
  function load() {
    api.agents().then((d) => setAgents(d.agents || [])).catch((e) => setErr(e.message));
    api.environments().then((d) => setEnvs(d.environments || [])).catch(() => {});
    api.envSudoDefaults().then((d) => setSudoDefaults(d || {})).catch(() => {});
    api.edition().then(setEdition).catch(() => {});
    api.controllerConfig().then((c) => setCfg(c || {})).catch(() => {});
    loadPortal(); loadHistory(); loadSessions(); loadUploads(); loadDownloads();
  }
  useEffect(() => { load(); }, []);

  async function portalAct(key, fn, okMsg) {
    setPortalBusy(key); setErr(""); setMsg("");
    try { await fn(); if (okMsg) setMsg(okMsg); loadPortal(); }
    catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function saveCreds() {
    setErr(""); setMsg("");
    if (np !== np2) { setErr("New portal passwords don't match."); return; }
    setPortalBusy("creds");
    try {
      await api.portalSetCreds(nu.trim(), np, cur);
      setMsg("Portal credentials saved."); setCur(""); setNu(""); setNp(""); setNp2("");
      loadPortal(); loadHistory(); loadSessions();
    } catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function removeCreds() {
    if (!window.confirm("Remove portal login access? Nobody can log in until new credentials are saved.")) return;
    setPortalBusy("remove"); setErr(""); setMsg("");
    try { await api.portalRemoveCreds(cur); setMsg("Portal login access removed."); setCur(""); loadPortal(); }
    catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function revoke(s) {
    const id = s.id ?? s.session_id;
    try { await api.portalRevokeSession(id); loadSessions(); } catch (e) { setErr(e.message); }
  }
  async function stageDownload(e) {
    const file = e.target.files[0]; if (!file) return;
    setErr(""); try { await api.portalStageDownload(file); loadDownloads(); } catch (e2) { setErr(e2.message); }
    e.target.value = "";
  }
  const fname = (f) => (typeof f === "string" ? f : (f.name || f.filename || ""));

  const groups = useMemo(() => {
    const m = {};
    for (const a of agents) (m[a.environment || "Unassigned"] ||= []).push(a);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [agents]);

  const idOf = (a) => a.host_id || a.id;
  const toggle = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  async function run(fn, okMsg) {
    setErr(""); setMsg("");
    try { await fn(); if (okMsg) setMsg(okMsg); load(); }
    catch (e) { setErr(e.message); }
  }

  async function assignEnvironment() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    let env = assignEnv;
    if (env === NEW_ENV) {
      env = (window.prompt("New environment name:") || "").trim();
      if (!env) return;
      try { await api.createEnvironment(env); } catch (e) { setErr(e.message); return; }
    }
    await run(async () => { for (const id of checked) await api.setHostEnvironment(id, env); },
      `Assigned ${checked.length} host(s) to ${env || "(unassigned)"}.`);
  }

  async function setSudo(required) {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    await run(async () => { for (const id of checked) await api.setHostSudo(id, required); },
      `Set ${checked.length} host(s) to ${required ? "password sudo" : "passwordless (NOPASSWD)"}.`);
  }

  async function disenrollChecked() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    if (!window.confirm(`Disenroll ${checked.length} host(s)? Their agents keep running but stop being managed.`)) return;
    await run(async () => { for (const id of checked) await api.removeHost(id); setChecked([]); },
      `Disenrolled ${checked.length} host(s).`);
  }

  // curl one-liner (built from portal status + controller config)
  const curlHost = cfg.address || "<this machine's address>";
  const curlPort = portPort || portal.configured_port || portal.port || 8090;
  const curlUser = portal.credentials_configured ? portal.username : "<username>";
  const curlCmd =
    `curl -k -sS -f -u '${curlUser}:<password>' -o sysible-agent-bundle.zip ` +
    `"https://${curlHost}:${curlPort}/cli/bundle" ` +
    `&& unzip -o sysible-agent-bundle.zip -d sysible-agent-bundle ` +
    `&& cd sysible-agent-bundle && chmod +x run_agent.sh && sudo ./run_agent.sh`;

  const envDefault = assignEnv && assignEnv !== NEW_ENV ? sudoDefaults[assignEnv] : undefined;
  const reachable = `https://${cfg.address || "<controller>"}:${curlPort}`;

  return (
    <div>
      {edition.host_limit != null && (
        <div className="muted" style={{ marginBottom: 12 }}>
          {(edition.edition || "Community")} edition — {edition.host_count ?? agents.length}/{edition.host_limit} hosts used
        </div>
      )}
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      <div className="he-2col">
        {/* LEFT: enrolled hosts */}
        <fieldset className="tool-group-box he-hosts">
          <legend>Enrolled Hosts (grouped by environment)</legend>
          <div className="ctl-row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
            <button className="btn ghost sm" onClick={() => setChecked(agents.map(idOf))}>Select All</button>
            <button className="btn ghost sm" onClick={() => setChecked([])}>Deselect All</button>
            <button className="btn ghost sm" onClick={() => setCollapsed(Object.fromEntries(groups.map(([e]) => [e, true])))}>Collapse All</button>
            <button className="btn ghost sm" onClick={() => setCollapsed({})}>Expand All</button>
            <button className="btn ghost sm" onClick={load}>Refresh</button>
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {agents.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled yet.</div>}
            {groups.map(([env, list]) => {
              const open = !collapsed[env];
              return (
                <div className="env-group" key={env}>
                  <div className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}>
                    {open ? "▾" : "▸"} {env}
                  </div>
                  {open && list.map((a) => (
                    <label className="host-row" key={idOf(a)}>
                      <input type="checkbox" checked={checked.includes(idOf(a))} onChange={() => toggle(idOf(a))} />
                      <span>{a.hostname || a.host_id}</span>
                      <span className="meta">{a.address || a.ip || ""}
                        {a.requires_sudo_password ? " · pw-sudo" : ""}
                        {a.last_seen != null ? ` · seen ${fmtSeen(a.last_seen)}` : ""}</span>
                    </label>
                  ))}
                </div>
              );
            })}
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn danger sm" onClick={disenrollChecked}>Disenroll Host(s)</button>
            <span className="faint">{checked.length} checked</span>
          </div>
        </fieldset>

        {/* RIGHT: enrollment actions */}
        <div className="he-actions">
          <fieldset className="tool-group-box"><legend>Agent Bundle</legend>
            <p className="faint" style={{ marginTop: 0 }}>
              The ready-to-run bundle, built on demand. Each download bakes in a fresh, one-time enrollment token.
            </p>
            <a className="btn sm" href={api.agentBundleUrl()}>Download Agent Bundle</a>
          </fieldset>

          <fieldset className="tool-group-box"><legend>Webserver Portal</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <span className={"badge" + (portal.running ? " green" : "")}>{portal.running ? "Running" : "Stopped"}</span>
              <button className="btn sm" disabled={portalBusy || portal.running}
                      onClick={() => portalAct("start", () => api.portalStart(), "Portal started.")}>
                {portalBusy === "start" ? <span className="spin" /> : "Start Portal"}</button>
              <button className="btn sm ghost" disabled={portalBusy || !portal.running}
                      onClick={() => portalAct("stop", () => api.portalStop(), "Portal stopped.")}>Stop Portal</button>
            </div>
            {portal.running && <div className="faint" style={{ marginTop: 6 }}>Reachable at: {reachable}</div>}
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center", marginTop: 10 }}>
              <span className="faint">Port</span>
              <input type="number" style={{ maxWidth: 120 }} value={portPort} onChange={(e) => setPortPort(e.target.value)} />
              <button className="btn sm" disabled={portalBusy || !portPort}
                      onClick={() => portalAct("port", () => api.portalSetPort(Number(portPort)),
                        "Port saved — restart the portal if it's running.")}>Save Port</button>
            </div>
            {!portal.credentials_configured &&
              <p className="faint" style={{ marginTop: 8 }}>No portal login set yet — set one under “Portal login” below so the curl download can authenticate.</p>}
          </fieldset>

          <fieldset className="tool-group-box"><legend>Command-Line Bundle Download (curl)</legend>
            <p className="faint" style={{ marginTop: 0 }}>
              For headless hosts: downloads, unzips, and runs the installer in one shot, authenticating with the
              portal login (curl -u). Replace <code>&lt;password&gt;</code> with the real portal password;
              <code> -k</code> skips the self-signed-cert check; the install step needs sudo.
            </p>
            <div className="cmd-preview" style={{ whiteSpace: "pre-wrap" }}>{curlCmd}</div>
            <button className="btn sm ghost" style={{ marginTop: 8 }}
                    onClick={() => navigator.clipboard?.writeText(curlCmd).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
              {copied ? "Copied ✓" : "Copy to Clipboard"}
            </button>
          </fieldset>

          <fieldset className="tool-group-box"><legend>Environment</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <select value={assignEnv} onChange={(e) => setAssignEnv(e.target.value)} style={{ maxWidth: 220 }}>
                <option value="">(unassigned)</option>
                {envs.map((e) => <option key={e} value={e}>{e}</option>)}
                <option value={NEW_ENV}>{NEW_ENV}</option>
              </select>
              <button className="btn sm" onClick={assignEnvironment}>Set Environment</button>
              <span className="faint">Applies to {checked.length} checked host(s).</span>
            </div>
          </fieldset>

          <fieldset className="tool-group-box"><legend>Sudo policy (selected host / environment)</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <span className="faint">Selected host(s):</span>
              <button className="btn sm" onClick={() => setSudo(true)}>Requires Password</button>
              <button className="btn sm" onClick={() => setSudo(false)}>Passwordless (NOPASSWD)</button>
            </div>
            {assignEnv && assignEnv !== NEW_ENV && (
              <div className="row" style={{ marginTop: 10, gap: 8, flexWrap: "wrap" }}>
                <span className="faint">
                  Environment '{assignEnv}' default: {envDefault ? "requires sudo password" : "passwordless"}
                </span>
                <button className="btn sm ghost" onClick={() => run(() => api.setEnvSudoDefault(assignEnv, !envDefault),
                  `Set ${assignEnv} default to ${!envDefault ? "password sudo" : "passwordless"}.`)}>
                  Set Environment's Sudo Default
                </button>
              </div>
            )}
            <p className="faint" style={{ marginTop: 8 }}>
              For password-sudo hosts, store your own sudo password from the “Sudo Password” button in the header.
            </p>
          </fieldset>
        </div>
      </div>

      {/* ===== Webserver Portal administration (credentials, sessions, files) ===== */}
      <h3 className="he-section-title">Webserver Portal — login &amp; files</h3>
      <p className="faint" style={{ marginTop: 0, maxWidth: 880 }}>
        The portal serves HTTPS using the controller's self-signed cert, so a host operator's browser shows an
        untrusted-certificate warning the first time — that's expected. Host operators sign in with the portal
        login below to download the agent bundle or exchange files. Only run the portal while provisioning, on a
        network you trust.
      </p>

      <div className="he-portal-grid">
        <fieldset className="tool-group-box"><legend>Current portal login</legend>
          {portal.credentials_configured ? (
            <div className="muted" style={{ fontSize: 13 }}>
              Username: <strong>{portal.username}</strong><br />
              Last successful login: {fmtTime(portal.last_login)}<br />
              Credentials last changed: {fmtTime(portal.last_changed)}
            </div>
          ) : <span className="faint">No portal credentials configured yet.</span>}
        </fieldset>

        <fieldset className="tool-group-box"><legend>Portal login (set / reset)</legend>
          <p className="faint" style={{ marginTop: 0 }}>Enter the current password to confirm a change (leave blank if none is set yet).</p>
          <label className="field"><span>Current password</span><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} /></label>
          <label className="field"><span>New username</span><input value={nu} onChange={(e) => setNu(e.target.value)} /></label>
          <label className="field"><span>New password</span><input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label>
          <label className="field"><span>Confirm new password</span><input type="password" value={np2} onChange={(e) => setNp2(e.target.value)} /></label>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn sm" disabled={portalBusy === "creds" || !nu || !np} onClick={saveCreds}>
              {portalBusy === "creds" ? <span className="spin" /> : "Save Credentials"}</button>
            <button className="btn sm danger" disabled={portalBusy === "remove"} onClick={removeCreds}>Remove Login Access</button>
          </div>
        </fieldset>
      </div>

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
                    <td className="faint mono">{fmtTime(h.timestamp ?? h.time ?? h.created_at)}</td>
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
                  <td className="faint mono">{fmtTime(s.created_at ?? s.logged_in ?? s.started_at)}</td>
                  <td className="faint mono">{fmtTime(s.expires_at ?? s.expires)}</td>
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

      <div className="he-portal-grid">
        <fieldset className="tool-group-box"><legend>Files Uploaded By Hosts</legend>
          <div className="spread" style={{ marginBottom: 8 }}>
            <span className="faint">Files host operators uploaded through the portal.</span>
            <button className="btn ghost sm" onClick={loadUploads}>Refresh</button>
          </div>
          {uploads.length === 0 ? <div className="empty">No uploaded files.</div> : (
            <table>
              <tbody>
                {uploads.map((f, i) => { const n = fname(f); return (
                  <tr key={n || i}>
                    <td>{n}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <a className="btn ghost sm" href={api.portalUploadUrl(n)}>Save</a>{" "}
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
            <span className="faint">Files staged for host operators to download.</span>
            <div className="row">
              <label className="btn ghost sm" style={{ cursor: "pointer" }}>
                Add File…<input type="file" style={{ display: "none" }} onChange={stageDownload} />
              </label>
              <button className="btn ghost sm" onClick={loadDownloads}>Refresh</button>
            </div>
          </div>
          {downloads.length === 0 ? <div className="empty">No staged files.</div> : (
            <table>
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
    </div>
  );
}
