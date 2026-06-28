import React, { useMemo, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ResultsPane from "../components/ResultsPane.jsx";

// Bespoke Service Management page, mirroring the desktop: a single service-name
// field, a live clickable "Installed services" list (click to fill the field),
// grouped action buttons, a Create/Configure Service form, and process tools.
export default function ServiceManagementPage({ hosts = [], onRefreshHosts }) {
  const [targets, setTargets] = useState([]);
  const [name, setName] = useState("");
  const [services, setServices] = useState([]);
  const [listHost, setListHost] = useState("");
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState([]);
  const [err, setErr] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [expanded, setExpanded] = useState(false);

  // Create/Configure fields
  const [cs, setCs] = useState({ name: "", description: "", exec_start: "", working_directory: "",
    run_as_user: "root", restart_policy: "on-failure", after: "network.target", enable_now: true });

  const filtered = useMemo(() => {
    const f = name.trim().toLowerCase();
    return f ? services.filter((s) => s.toLowerCase().includes(f)) : services;
  }, [services, name]);

  async function listServices(running) {
    const host = listHost || targets[0];
    if (!host) { setErr("Check a host first (services are listed from one host)."); return; }
    setBusy(running ? "running" : "installed"); setErr("");
    try { const d = await api.servicesList(host, running); setServices(d.services || []); setListHost(host); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function run(action, params, label) {
    if (targets.length === 0) { setErr("Check one or more hosts first."); return; }
    setErr(""); setBusy(action);
    try {
      const r = await api.runTool(action, targets, params);
      setResults((prev) => [{ label, ...r, at: Date.now() }, ...prev]);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }
  const svc = (action, label) => run(action, { name }, label);

  return (
    <div className="tool-flex">
      {!expanded && <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check hosts to act on; the installed-services list is read from one host." />}

      {!expanded && (
      <div className="tool-actions-col"><div className="tool-actions-scroll">
        <label className="field" style={{ marginTop: 0 }}>
          <span>Service name (also filters the list below)</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. sshd" />
        </label>
        <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 8 }}>
          <button className="btn sm" disabled={busy === "installed"} onClick={() => listServices(false)}>
            {busy === "installed" ? <span className="spin" /> : "List Installed Services"}</button>
          <button className="btn sm" disabled={busy === "running"} onClick={() => listServices(true)}>
            {busy === "running" ? <span className="spin" /> : "List Running Services"}</button>
        </div>

        <div className="section-title">Installed services {listHost ? `(on ${listHost})` : ""} — click to fill</div>
        <div className="card" style={{ maxHeight: 200, overflowY: "auto", padding: 6 }}>
          {filtered.length === 0 ? <div className="faint" style={{ padding: 8 }}>List a host's services to populate.</div>
            : filtered.map((s) => (
              <div key={s} className="host-row" style={{ cursor: "pointer", paddingLeft: 6 }}
                   onClick={() => setName(s)}>{s}</div>
            ))}
        </div>

        <fieldset className="tool-group-box" style={{ marginTop: 14 }}><legend>Service control</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_start", `Start ${name}`)}>Start</button>
            <button className="btn sm danger" disabled={busy || !name} onClick={() => svc("svc_stop", `Stop ${name}`)}>Stop</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_restart", `Restart ${name}`)}>Restart</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_reload", `Reload ${name}`)}>Reload</button>
          </div>
        </fieldset>
        <fieldset className="tool-group-box"><legend>Boot & status</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_enable", `Enable ${name}`)}>Enable At Boot</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_disable", `Disable ${name}`)}>Disable At Boot</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_status", `Status ${name}`)}>Check Status</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => run("svc_logs", { name, lines: 200 }, `Logs ${name}`)}>View Logs</button>
          </div>
        </fieldset>
        <fieldset className="tool-group-box"><legend>Diagnostics</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_troubleshoot", `Troubleshoot ${name}`)}>Troubleshoot This Service</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_dependencies", `Dependencies ${name}`)}>View Dependencies</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Processes</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy} onClick={() => run("proc_high_load", {}, "Investigate high load")}>Investigate High Load</button>
            <button className="btn sm" disabled={busy} onClick={() => run("proc_zombies", {}, "Zombie processes")}>Zombie Processes</button>
          </div>
        </fieldset>

        <button className="btn ghost sm" onClick={() => setShowCreate((v) => !v)} style={{ marginTop: 4 }}>
          {showCreate ? "▾" : "▸"} Create / Configure Service
        </button>
        {showCreate && (
          <fieldset className="tool-group-box" style={{ marginTop: 8 }}><legend>Create systemd service</legend>
            {[["name", "Unit name"], ["description", "Description"], ["exec_start", "ExecStart"],
              ["working_directory", "Working dir"], ["run_as_user", "Run as user"], ["after", "After"]].map(([k, l]) => (
              <label className="field" key={k}><span>{l}</span>
                <input value={cs[k]} onChange={(e) => setCs({ ...cs, [k]: e.target.value })} /></label>
            ))}
            <div className="checkrow"><input id="en" type="checkbox" checked={cs.enable_now}
              onChange={(e) => setCs({ ...cs, enable_now: e.target.checked })} /><label htmlFor="en">Enable now</label></div>
            <button className="btn" style={{ marginTop: 12 }} disabled={busy || !cs.name || !cs.exec_start}
                    onClick={() => run("svc_create", cs, `Create service ${cs.name}`)}>Create Service</button>
          </fieldset>
        )}
        {err && <div className="error-box">{err}</div>}
      </div></div>
      )}

      <ResultsPane results={results} setResults={setResults} expanded={expanded}
                   onToggleExpand={() => setExpanded((v) => !v)}
                   empty="Run an action — output appears here." />
    </div>
  );
}
