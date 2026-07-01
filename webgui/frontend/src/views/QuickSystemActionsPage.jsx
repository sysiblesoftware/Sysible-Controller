import React, { useMemo, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ResultsPane from "../components/ResultsPane.jsx";

// Quick System Actions — bespoke page. Beyond the one-click fixes, it has a
// service browser: list the running (or installed) services on a selected host,
// click one to select it, then Restart / Start / Stop. Everything runs across
// the checked target hosts.
export default function QuickSystemActionsPage({ hosts = [], onRefreshHosts }) {
  const [targets, setTargets] = useState([]);
  const [name, setName] = useState("");
  const [services, setServices] = useState([]);
  const [listHost, setListHost] = useState("");
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState([]);
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState(false);

  const filtered = useMemo(() => {
    const f = name.trim().toLowerCase();
    return f ? services.filter((s) => s.toLowerCase().includes(f)) : services;
  }, [services, name]);

  async function listServices(running) {
    const host = listHost || targets[0];
    if (!host) { setErr("Check a host first — services are read from one host."); return; }
    setBusy(running ? "running" : "installed"); setErr("");
    try { const d = await api.servicesList(host, running); setServices(d.services || []); setListHost(host); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function run(action, params, label) {
    if (targets.length === 0) { setErr("Check one or more target hosts first."); return; }
    setErr(""); setBusy(action);
    try {
      const r = await api.runTool(action, targets, params || {});
      setResults((prev) => [{ label, ...r, at: Date.now() }, ...prev]);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }
  const svc = (action, label) => run(action, { name }, label);
  const confirmRun = (action, label, prompt) => { if (window.confirm(prompt)) run(action, {}, label); };

  // One-click groups → [action name, button label, danger?]
  const COMMON = [
    ["qsa_restart_networkmanager", "Restart NetworkManager"],
    ["qsa_flush_dns", "Flush DNS cache"],
    ["qsa_restart_ssh", "Restart SSH server"],
    ["qsa_restart_timesync", "Restart time sync"],
    ["qsa_sync_time_now", "Sync clock now"],
    ["qsa_restart_docker", "Restart Docker"],
    ["qsa_restart_agent", "Restart Sysible agent"],
  ];
  const FREE = [
    ["qsa_drop_caches", "Free memory (drop caches)"],
    ["qsa_clean_pkg_cache", "Clean package cache"],
    ["qsa_vacuum_journal", "Vacuum journal logs"],
    ["qsa_fstrim", "Trim filesystems (fstrim)"],
  ];
  const HOUSE = [
    ["qsa_reset_failed", "Clear failed units"],
    ["qsa_daemon_reload", "Reload systemd (daemon-reload)"],
  ];

  return (
    <div className="tool-flex">
      {!expanded && <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check hosts to act on; the service list is read from one host." />}

      {!expanded && (
      <div className="tool-actions-col"><div className="tool-actions-scroll">
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Service (by name)</legend>
          <label className="field" style={{ marginTop: 0 }}>
            <span>Service name (also filters the list below)</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. nginx, docker, postgresql" />
          </label>
          <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 8 }}>
            <button className="btn sm" disabled={busy === "running"} onClick={() => listServices(true)}>
              {busy === "running" ? <span className="spin" /> : "List Running Services"}</button>
            <button className="btn sm" disabled={busy === "installed"} onClick={() => listServices(false)}>
              {busy === "installed" ? <span className="spin" /> : "List Installed Services"}</button>
          </div>
          <div className="section-title" style={{ marginTop: 10 }}>
            Services {listHost ? `(on ${listHost})` : ""} — click to select
          </div>
          <div className="card" style={{ maxHeight: 200, overflowY: "auto", padding: 6 }}>
            {filtered.length === 0
              ? <div className="faint" style={{ padding: 8 }}>List a host's services to populate.</div>
              : filtered.map((s) => (
                  <div key={s} className={"host-row" + (s === name ? " active" : "")}
                       style={{ cursor: "pointer", paddingLeft: 6,
                                background: s === name ? "var(--panel-2)" : undefined }}
                       onClick={() => setName(s)}>{s}</div>
                ))}
          </div>
          <div className="group-buttons" style={{ marginTop: 10 }}>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_restart", `Restart ${name}`)}>Restart</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_start", `Start ${name}`)}>Start</button>
            <button className="btn sm danger" disabled={busy || !name} onClick={() => svc("svc_stop", `Stop ${name}`)}>Stop</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_status", `Status ${name}`)}>Check Status</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Common services</legend>
          <div className="group-buttons">
            {COMMON.map(([a, l]) => (
              <button key={a} className="btn sm" disabled={busy} onClick={() => run(a, {}, l)}>{l}</button>
            ))}
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Free up resources</legend>
          <div className="group-buttons">
            {FREE.map(([a, l]) => (
              <button key={a} className="btn sm" disabled={busy} onClick={() => run(a, {}, l)}>{l}</button>
            ))}
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Systemd housekeeping</legend>
          <div className="group-buttons">
            {HOUSE.map(([a, l]) => (
              <button key={a} className="btn sm" disabled={busy} onClick={() => run(a, {}, l)}>{l}</button>
            ))}
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Power (careful)</legend>
          <div className="group-buttons">
            <button className="btn sm danger" disabled={busy}
                    onClick={() => confirmRun("qsa_reboot", "Reboot host", "Reboot every checked host now?")}>
              Reboot host</button>
            <button className="btn sm danger" disabled={busy}
                    onClick={() => confirmRun("qsa_poweroff", "Power off host",
                      "Power off every checked host now? They will NOT come back until powered on out-of-band.")}>
              Power off host</button>
          </div>
        </fieldset>
        {err && <div className="error-box">{err}</div>}
      </div></div>
      )}

      <ResultsPane results={results} setResults={setResults} expanded={expanded}
                   onToggleExpand={() => setExpanded((v) => !v)}
                   empty="Run an action — output appears here." />
    </div>
  );
}
