import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Host Enrollment — mirrors the desktop page: agent bundle
// (download + curl), enrolled hosts grouped by environment with multi-select,
// environment assignment, and sudo policy (per host + per-environment default).

function fmtSeen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

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

  function load() {
    api.agents().then((d) => setAgents(d.agents || [])).catch((e) => setErr(e.message));
    api.environments().then((d) => setEnvs(d.environments || [])).catch(() => {});
    api.envSudoDefaults().then((d) => setSudoDefaults(d || {})).catch(() => {});
    api.edition().then(setEdition).catch(() => {});
    api.portalStatus().then((s) => setPortal(s || {})).catch(() => {});
    api.controllerConfig().then((c) => setCfg(c || {})).catch(() => {});
  }
  useEffect(() => { load(); }, []);

  const groups = useMemo(() => {
    const m = {};
    for (const a of agents) (m[a.environment || "Unassigned"] ||= []).push(a);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [agents]);

  const idOf = (a) => a.host_id || a.id;
  const toggle = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);
  const checkedAgents = agents.filter((a) => checked.includes(idOf(a)));

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
  const curlPort = portal.configured_port || portal.port || 443;
  const curlUser = portal.credentials_configured ? portal.username : "<username>";
  const curlCmd =
    `curl -k -sS -f -u '${curlUser}:<password>' -o sysible-agent-bundle.zip ` +
    `"https://${curlHost}:${curlPort}/cli/bundle" ` +
    `&& unzip -o sysible-agent-bundle.zip -d sysible-agent-bundle ` +
    `&& cd sysible-agent-bundle && chmod +x run_agent.sh && sudo ./run_agent.sh`;

  const envDefault = assignEnv && assignEnv !== NEW_ENV ? sudoDefaults[assignEnv] : undefined;

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

        {/* RIGHT: actions */}
        <div className="he-actions">
          <fieldset className="tool-group-box"><legend>Agent Bundle</legend>
            <p className="faint" style={{ marginTop: 0 }}>
              The ready-to-run bundle, built on demand. Each download bakes in a fresh, one-time enrollment token.
            </p>
            <a className="btn sm" href={api.agentBundleUrl()}>Download Agent Bundle</a>
          </fieldset>

          <fieldset className="tool-group-box"><legend>Command-Line Bundle Download (curl)</legend>
            <p className="faint" style={{ marginTop: 0 }}>
              For headless hosts: downloads, unzips, and runs the installer in one shot, authenticating with the
              Webserver Portal login (curl -u). Replace <code>&lt;password&gt;</code> with the real portal password;
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
    </div>
  );
}
