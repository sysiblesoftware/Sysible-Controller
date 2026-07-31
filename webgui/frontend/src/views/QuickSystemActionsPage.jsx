import React, { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ResultsPane from "../components/ResultsPane.jsx";
import { hypervisorFleetWarning } from "../hypervisor.js";

// Quick System Actions — bespoke page. Beyond the one-click fixes, it has a
// service browser: list the running (or installed) services on a selected host,
// click one to select it, then Restart / Start / Stop. Everything runs across
// the checked target hosts.
//
// `prefill` ({name?, host?}) lets a "Fix in Quick System Actions →" deep-link
// (e.g. a failed unit on the host posture page) arrive with the Service field
// and target host already filled in.
export default function QuickSystemActionsPage({ hosts = [], onRefreshHosts, prefill }) {
  const [targets, setTargets] = useState(prefill?.host ? [prefill.host] : []);
  const [name, setName] = useState(prefill?.name || "");
  // Re-apply if a fresh deep-link arrives while the page is already open.
  const lastPrefill = useRef(null);
  useEffect(() => {
    if (!prefill) return;
    const sig = `${prefill.host || ""}|${prefill.name || ""}`;
    if (sig === lastPrefill.current) return;
    lastPrefill.current = sig;
    if (prefill.name) setName(prefill.name);
    if (prefill.host) setTargets((t) => (t.includes(prefill.host) ? t : [...t, prefill.host]));
  }, [prefill]);
  const [services, setServices] = useState([]);
  const [listHost, setListHost] = useState("");
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState([]);
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [script, setScript] = useState("");
  const [scriptSudo, setScriptSudo] = useState("");

  const filtered = useMemo(() => {
    const f = name.trim().toLowerCase();
    return f ? services.filter((s) => s.toLowerCase().includes(f)) : services;
  }, [services, name]);

  async function listServices(running) {
    // Prefer the currently-checked host over the last-listed one (stale-host fix).
    const host = targets[0] || listHost;
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
  // Reboot / power-off: if any checked host is a hypervisor, warn that its guest
  // VMs go down with it before confirming.
  const confirmPower = (action, label, prompt, verb) => {
    const targetHosts = hosts.filter((h) => targets.includes(h.label));
    const warn = hypervisorFleetWarning(targetHosts, verb);
    confirmRun(action, label, warn ? `${warn}\n\n${prompt}` : prompt);
  };

  async function runScript() {
    if (targets.length === 0) { setErr("Check one or more target hosts first."); return; }
    if (!script.trim()) { setErr("Enter a script to run first."); return; }
    if (!window.confirm(`Run this script as root on the ${targets.length} checked host(s)?`)) return;
    setErr(""); setBusy("script");
    try {
      // Reuse the fleet dispatch with an EXPLICIT target list (the checked
      // hosts), so this runs on exactly the hosts you chose — not the whole fleet.
      const r = await api.fleet("script", targets, script, scriptSudo);
      setResults((prev) => [{ label: "Run script", ...r, at: Date.now() }, ...prev]);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  // One-click groups → [action name, button label, danger?]
  // Third field = confirm before running: these restart services that can sever
  // the operator's own management path (SSH / networking / the agent itself).
  const COMMON = [
    ["qsa_restart_networkmanager", "Restart NetworkManager", true],
    ["qsa_flush_dns", "Flush DNS cache"],
    ["qsa_restart_ssh", "Restart SSH server", true],
    ["qsa_restart_timesync", "Restart time sync"],
    ["qsa_sync_time_now", "Sync clock now"],
    ["qsa_restart_docker", "Restart Docker"],
    ["qsa_restart_agent", "Restart Sysible agent", true],
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
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Reachability</legend>
          <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
            Confirm the checked hosts are reachable and their agents are answering — a green OK per host means it responded.
          </div>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy} onClick={() => run("qsa_ping", {}, "Ping (reachability)")}>Ping</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Run a script (on the checked hosts)</legend>
          <label className="field" style={{ marginTop: 0 }}>
            <span>Shell script — runs as root on each checked host</span>
            <textarea rows={3} value={script} onChange={(e) => setScript(e.target.value)}
                      placeholder="e.g. uname -a && uptime" />
          </label>
          <label className="field" style={{ marginTop: 8 }}>
            <span>Sudo password <span className="faint">(optional — only for hosts that require one this run)</span></span>
            <input type="password" autoComplete="off" value={scriptSudo}
                   onChange={(e) => setScriptSudo(e.target.value)}
                   placeholder="Leave blank to use your stored sudo password" />
          </label>
          <div className="group-buttons" style={{ marginTop: 10 }}>
            <button className="btn sm" disabled={busy} onClick={runScript}>
              {busy === "script" ? <span className="spin" /> : `Run Script on ${targets.length || "checked"} host(s)`}
            </button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Service (by name)</legend>
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
          {/* The service list is an OPTIONAL browser — you can act on a typed
              name without it. Only show the (potentially empty) list once the
              operator has actually listed a host's services, so an empty box
              doesn't read like a required step. */}
          {services.length === 0 ? (
            <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
              Type a service name above and act — or use “List … Services” to browse and pick one.
            </div>
          ) : (
            <>
              <div className="section-title" style={{ marginTop: 10 }}>
                Services {listHost ? `(on ${listHost})` : ""} — click to select
              </div>
              <div className="card" style={{ maxHeight: 200, overflowY: "auto", padding: 6 }}>
                {filtered.length === 0
                  ? <div className="faint" style={{ padding: 8 }}>No services match “{name}”.</div>
                  : filtered.map((s) => (
                      <div key={s} className={"host-row" + (s === name ? " active" : "")}
                           style={{ cursor: "pointer", paddingLeft: 6,
                                    background: s === name ? "var(--panel-2)" : undefined }}
                           onClick={() => setName(s)}>{s}</div>
                    ))}
              </div>
            </>
          )}
          <div className="group-buttons" style={{ marginTop: 10 }}>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_restart", `Restart ${name}`)}>Restart</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_start", `Start ${name}`)}>Start</button>
            <button className="btn sm danger" disabled={busy || !name} onClick={() => svc("svc_stop", `Stop ${name}`)}>Stop</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => svc("svc_status", `Status ${name}`)}>Check Status</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Common services</legend>
          <div className="group-buttons">
            {COMMON.map(([a, l, needsConfirm]) => (
              <button key={a} className="btn sm" disabled={busy} onClick={() =>
                needsConfirm
                  ? confirmRun(a, l, `${l} on the ${targets.length} checked host(s)? This can interrupt your own connection to them.`)
                  : run(a, {}, l)}>{l}</button>
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
                    onClick={() => confirmPower("qsa_reboot", "Reboot host", "Reboot every checked host now?", "Rebooting")}>
              Reboot host</button>
            <button className="btn sm danger" disabled={busy}
                    onClick={() => confirmPower("qsa_poweroff", "Power off host",
                      "Power off every checked host now? They will NOT come back until powered on out-of-band.", "Powering off")}>
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
