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
  const [tab, setTab] = useState("hosts");
  const [sudoEnv, setSudoEnv] = useState("");
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
    if (!window.confirm(`Disenroll ${checked.length} host(s)? If a host is online its agent service is stopped and removed first; if it's offline the enrollment is dropped here and you'll need to run disenroll_agent.sh on it directly.`)) return;
    await run(async () => {
      const warnings = [];
      for (const id of checked) {
        const r = await api.removeHost(id);
        const t = r && r.teardown;
        if (t && !t.ok) warnings.push(`${t.host || id}: ${t.error || t.stderr || "agent service teardown did not confirm"}`);
      }
      setChecked([]);
      if (warnings.length) throw new Error("Disenrolled, but service teardown was not confirmed on:\n" + warnings.join("\n"));
    }, `Disenrolled ${checked.length} host(s).`);
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

  const reachable = `https://${cfg.address || "<controller>"}:${curlPort}`;

  const TABS = [["hosts", "Enrolled Hosts"], ["enroll", "Enroll a Host"], ["portal", "Webserver Portal"]];

  return (
    <div>
      {edition.host_limit != null && (
        <div className="muted" style={{ marginBottom: 12 }}>
          {(edition.edition || "Community")} edition — {edition.host_count ?? agents.length}/{edition.host_limit} hosts used
        </div>
      )}

      <div className="tabs" style={{ marginBottom: 16 }}>
        {TABS.map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>

      {/* On the Enrolled Hosts tab the status banner is rendered down in the
          actions column instead (under the buttons that triggered it); other
          tabs keep it here at the top. */}
      {tab !== "hosts" && err && <div className="error-box">{err}</div>}
      {tab !== "hosts" && msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      {/* ============================ TAB: ENROLLED HOSTS ============================ */}
      {tab === "hosts" && (
      <div className="he-2col">
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
            {agents.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled yet — use the “Enroll a Host” tab.</div>}
            {groups.map(([env, list]) => {
              const open = !collapsed[env];
              return (
                <div className="env-group" key={env}>
                  <div className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}>
                    {open ? "▾" : "▸"} {env}
                  </div>
                  {open && list.map((a) => (
                    <label className="host-row he-host" key={idOf(a)}>
                      <input type="checkbox" checked={checked.includes(idOf(a))} onChange={() => toggle(idOf(a))} />
                      <span className={"dot " + (a.online === false ? "bad" : a.online === true ? "ok" : "")}
                            title={a.online === false ? "Offline" : a.online === true ? "Online" : ""} />
                      <span className="he-host-body">
                        <span className="he-host-name">{a.hostname || a.host_id}
                          {a.requires_sudo_password && <span className="badge" style={{ marginLeft: 6, fontSize: 10 }}>pw-sudo</span>}
                        </span>
                        <span className="he-host-meta">{a.address || a.ip || "—"}
                          {a.last_seen != null ? ` · seen ${fmtSeen(a.last_seen)}` : ""}</span>
                      </span>
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

        <div className="he-actions">
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

          <fieldset className="tool-group-box"><legend>Sudo policy</legend>
            <div className="section-title" style={{ marginTop: 0 }}>For the checked host(s) — {checked.length} selected</div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button className="btn sm" disabled={checked.length === 0} onClick={() => setSudo(true)}>Requires Password</button>
              <button className="btn sm" disabled={checked.length === 0} onClick={() => setSudo(false)}>Passwordless (NOPASSWD)</button>
            </div>

            <div className="section-title">For an entire environment (new hosts inherit this)</div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <select value={sudoEnv} onChange={(e) => setSudoEnv(e.target.value)} style={{ maxWidth: 200 }}>
                <option value="">Choose environment…</option>
                {envs.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
              {sudoEnv && (
                <span className="faint">
                  currently {sudoDefaults[sudoEnv] ? "requires password" : "passwordless"}
                </span>
              )}
            </div>
            {sudoEnv && (
              <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
                <button className="btn sm" onClick={() => run(() => api.setEnvSudoDefault(sudoEnv, true),
                  `Set ${sudoEnv} default to password sudo.`)}>Requires Password</button>
                <button className="btn sm" onClick={() => run(() => api.setEnvSudoDefault(sudoEnv, false),
                  `Set ${sudoEnv} default to passwordless.`)}>Passwordless (NOPASSWD)</button>
              </div>
            )}

            <p className="faint" style={{ marginTop: 10 }}>
              For password-sudo hosts, store your own sudo password from the “Sudo Password” button in the header.
            </p>
          </fieldset>

          {/* Status for the actions in this column shows here, under the buttons,
              rather than at the top of the page — easier to notice where you clicked. */}
          {err && <div className="error-box">{err}</div>}
          {msg && <div className="ok-text" style={{ marginTop: 4 }}>{msg}</div>}
        </div>
      </div>
      )}

      {/* ============================ TAB: ENROLL A HOST ============================ */}
      {tab === "enroll" && (
      <div style={{ maxWidth: 760 }}>
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Download the agent bundle</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            The ready-to-run bundle, built on demand — each download bakes in a fresh, one-time enrollment token.
            Copy it to the target host, unzip, and run <code>./run_agent.sh</code>.
          </p>
          <a className="btn sm" href={api.agentBundleUrl()}>Download Agent Bundle</a>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Headless install (curl one-liner)</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            For terminal-only hosts: downloads, unzips, and runs the installer in one shot, authenticating with the
            portal login (curl -u). Replace <code>&lt;password&gt;</code> with the real portal password; <code>-k</code> skips
            the self-signed-cert check; the install step needs sudo.
          </p>
          <div className="cmd-preview" style={{ whiteSpace: "pre-wrap" }}>{curlCmd}</div>
          <button className="btn sm ghost" style={{ marginTop: 8 }}
                  onClick={() => navigator.clipboard?.writeText(curlCmd).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
            {copied ? "Copied ✓" : "Copy to Clipboard"}
          </button>
          <p className="faint" style={{ marginTop: 10 }}>
            The curl download needs the Webserver Portal {portal.running
              ? <>running (it is) with a login set.</>
              : <>running — start it and set a login on the <button className="linklike" onClick={() => setTab("portal")}>Webserver Portal</button> tab.</>}
          </p>
        </fieldset>
      </div>
      )}

      {/* ============================ TAB: WEBSERVER PORTAL ============================ */}
      {tab === "portal" && (
      <div>
        <div className="he-section-head" style={{ marginTop: 0, paddingTop: 0, borderTop: "none" }}>
          <h3>Webserver Portal</h3>
          <button className="btn ghost sm" onClick={() => { loadPortal(); loadHistory(); loadSessions(); loadUploads(); loadDownloads(); }}>Refresh</button>
        </div>
        <p className="faint" style={{ marginTop: 0, maxWidth: 880 }}>
          Host operators sign in with this login to download the agent bundle or exchange files. Run the portal only
          while provisioning, on a trusted network.
        </p>

        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Status &amp; port</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <span className={"badge" + (portal.running ? " green" : "")}>{portal.running ? "Running" : "Stopped"}</span>
              <button className="btn sm" disabled={portalBusy || portal.running}
                      onClick={() => portalAct("start", async () => {
                        const r = await api.portalStart();
                        if (r && r.running === false) throw new Error(r.error || "The portal failed to start.");
                        return r;
                      }, "Portal started.")}>
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
          </fieldset>

        <div className="he-portal-grid">
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Portal login</legend>
          <div className="muted" style={{ fontSize: 13, marginBottom: 10 }}>
            {portal.credentials_configured
              ? <>Current user: <strong>{portal.username}</strong> · last login {fmtTime(portal.last_login)}</>
              : <span className="faint">No portal login set yet.</span>}
          </div>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>New username</span>
              <input value={nu} onChange={(e) => setNu(e.target.value)} /></label></div>
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Current password (if set)</span>
              <input type="password" value={cur} onChange={(e) => setCur(e.target.value)} /></label></div>
          </div>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>New password</span>
              <input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label></div>
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Confirm new password</span>
              <input type="password" value={np2} onChange={(e) => setNp2(e.target.value)} /></label></div>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn sm" disabled={portalBusy === "creds" || !nu || !np} onClick={saveCreds}>
              {portalBusy === "creds" ? <span className="spin" /> : "Save Login"}</button>
            <button className="btn sm danger" disabled={portalBusy === "remove" || !portal.credentials_configured} onClick={removeCreds}>Remove Login</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Active Sessions</legend>
          <p className="faint" style={{ marginTop: 0 }}>Operators currently signed in. Revoking one logs that browser out immediately.</p>
          {sessions.length === 0 ? <div className="empty" style={{ padding: 20 }}>No active sessions.</div> : (
            <table>
              <thead><tr><th>Logged In</th><th>IP</th><th></th></tr></thead>
              <tbody>
                {sessions.map((s, i) => (
                  <tr key={s.id ?? s.session_id ?? i}>
                    <td className="faint mono">{fmtTime(s.created_at ?? s.logged_in ?? s.started_at)}</td>
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
      </div>

      <fieldset className="tool-group-box"><legend>Login History</legend>
        {history.length === 0 ? <div className="empty" style={{ padding: 16 }}>No login history.</div> : (
          <div style={{ maxHeight: 200, overflowY: "auto" }}>
            <table>
              <thead><tr><th>Time</th><th>Event</th><th>Username</th><th>IP</th></tr></thead>
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

      <div className="he-portal-grid">
        <fieldset className="tool-group-box"><legend>Files Uploaded By Hosts</legend>
          {uploads.length === 0 ? <div className="empty" style={{ padding: 16 }}>No uploaded files.</div> : (
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
          <div style={{ marginBottom: 8 }}>
            <label className="btn ghost sm" style={{ cursor: "pointer" }}>
              Add File…<input type="file" style={{ display: "none" }} onChange={stageDownload} />
            </label>
          </div>
          {downloads.length === 0 ? <div className="empty" style={{ padding: 16 }}>No staged files.</div> : (
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
      )}
    </div>
  );
}
