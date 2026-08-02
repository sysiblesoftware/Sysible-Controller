import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostResults from "../components/HostResults.jsx";
import FileTransfer from "../components/FileTransfer.jsx";
import { hypervisorFleetWarning } from "../hypervisor.js";

// Sysible Connect — mirrors the desktop window: managed-hosts tree on the
// left; selected-host panel, terminals, fleet actions, file transfer, and
// SSH-to-new-host on the right.
export default function Connect() {
  const [hosts, setHosts] = useState([]);
  const [sel, setSel] = useState(null);
  const [checked, setChecked] = useState([]);
  const [err, setErr] = useState("");
  const [collapsed, setCollapsed] = useState({});
  const [checkin, setCheckin] = useState(null);
  const [busy, setBusy] = useState("");

  const toggleCheck = (id) =>
    setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));

  function loadHosts() {
    api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message));
  }
  useEffect(loadHosts, []);

  const groups = useMemo(() => {
    const m = {};
    for (const h of hosts) (m[h.environment || "Unassigned"] ||= []).push(h);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [hosts]);

  // Terminals pop out into their own external window,
  // rather than docking inside this page. Same-origin, so the pop-out shares the
  // session cookie. A per-host window name means re-opening the same host focuses
  // its existing window instead of spawning duplicates.
  function openTerm(h) {
    return window.open(
      `/?term=${encodeURIComponent(h.id)}&label=${encodeURIComponent(h.label)}`,
      `sysible_term_${h.id}`, "width=960,height=640");
  }

  // Open a terminal window for every CHECKED host (or the selected one if none
  // are checked) — no need to single-select first. Each host gets its own
  // window; a per-host window name means re-opening focuses the existing one.
  // Browsers block all but the first pop-up from a single click by default, so
  // detect that and tell the operator how to allow it.
  function openChecked() {
    const ids = checked.length ? checked : (sel ? [sel.id] : []);
    if (!ids.length) { setErr("Check one or more hosts (or select one) to open a terminal."); return; }
    const byId = Object.fromEntries(hosts.map((h) => [h.id, h]));
    let blocked = 0;
    ids.forEach((id) => { const h = byId[id]; if (h && !openTerm(h)) blocked++; });
    if (blocked > 0) {
      setErr(`Your browser blocked ${blocked} of ${ids.length} terminal window(s). Allow pop-ups for this ` +
        `site to open several at once (in Firefox, click “Preferences/Options” on the blocked-popup bar → ` +
        `Allow), then click Open Terminal again.`);
    } else {
      setErr("");
    }
  }

  const [checkinAt, setCheckinAt] = useState(0);
  const [showCheckin, setShowCheckin] = useState(false);
  useEffect(() => {
    if (!showCheckin) return;
    const onKey = (e) => { if (e.key === "Escape") setShowCheckin(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showCheckin]);
  async function runCheckin(targets) {
    setBusy("checkin"); setErr("");
    try { setCheckin((await api.checkin(targets || [])).results); setCheckinAt(Date.now()); setShowCheckin(true); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function removeAllSsh() {
    if (!window.confirm(
      "Remove ALL SSH host records?\n\n" +
      "This deletes every SSH connection — pure-SSH hosts AND the legacy “Agent+SSH” " +
      "shadow records — and un-pins their host keys. Agent enrollments are NOT affected. " +
      "You'll lose SSH terminal/ping access to any non-agent host.")) return;
    setBusy("wipe-ssh"); setErr(""); setSel(null);
    try {
      await api.deleteAllSshHosts();
      setChecked([]);
      loadHosts();
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div className="three-pane">
      {/* LEFT: managed hosts */}
      <div className="host-pane">
        <strong style={{ fontSize: 13 }}>Managed Hosts (agent + SSH)</strong>
        <div className="ctl-row" style={{ marginTop: 8 }}>
          <button className="btn sm" disabled={!checked.length && !sel}
                  title="Open a terminal window for each checked host"
                  onClick={openChecked}>
            {checked.length > 1 ? `Open ${checked.length} Terminals` : "Open Terminal"}
          </button>
          <button className="btn ghost sm" onClick={loadHosts}>Refresh</button>
          <button className="btn ghost sm" disabled={busy === "checkin"}
                  title={checked.length ? "Ping the checked hosts" : "Ping all hosts (check some to ping just those)"}
                  onClick={() => runCheckin(checked)}>
            {busy === "checkin" ? <span className="spin" />
              : checked.length ? `Ping ${checked.length} checked` : "Ping All"}
          </button>
        </div>
        <div className="ctl-row">
          <button className="btn ghost sm" onClick={() => setChecked(hosts.map((h) => h.id))}>Select All</button>
          <button className="btn ghost sm" onClick={() => setChecked([])}>Deselect All</button>
        </div>
        <div className="ctl-row">
          <button className="btn ghost sm" onClick={() =>
            setCollapsed(Object.fromEntries(groups.map(([e]) => [e, true])))}>Collapse All</button>
          <button className="btn ghost sm" onClick={() => setCollapsed({})}>Expand All</button>
        </div>
        {hosts.length > 0 && (
          <div className="ctl-row">
            <button className="btn ghost sm danger" disabled={busy === "wipe-ssh"}
                    title="Delete every SSH connection record (pure-SSH hosts and legacy Agent+SSH shadows). Agent enrollments are not affected."
                    onClick={removeAllSsh}>
              {busy === "wipe-ssh" ? <span className="spin" /> : "Remove all SSH hosts"}</button>
          </div>
        )}
        <div className="host-tree">
          {hosts.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled.</div>}
          {groups.map(([env, list]) => {
            const open = !collapsed[env];
            const groupIds = list.map((h) => h.id);
            const allInGroup = groupIds.every((id) => checked.includes(id));
            return (
              <div className="env-group" key={env}>
                <div className="env-head" role="button" tabIndex={0}
                     onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}
                     onKeyDown={(e) => {
                       if (e.key === "Enter" || e.key === " ") {
                         e.preventDefault();
                         setCollapsed((c) => ({ ...c, [env]: open }));
                       }
                     }}>
                  <input
                    type="checkbox"
                    className="env-check"
                    title={`Select all in ${env}`}
                    checked={allInGroup}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => {
                      e.stopPropagation();
                      setChecked((c) => allInGroup
                        ? c.filter((id) => !groupIds.includes(id))
                        : [...new Set([...c, ...groupIds])]);
                    }}
                  />
                  {open ? "▾" : "▸"} {env}
                </div>
                {open && list.map((h) => {
                  const ci = checkin && checkin.find((r) => r.id === h.id);
                  return (
                    <div className={"host-row" + (sel && sel.id === h.id ? " active" : "")}
                         key={h.id} style={{
                           background: sel && sel.id === h.id ? "var(--row-hover)" : "" }}>
                      <input type="checkbox" checked={checked.includes(h.id)}
                             onChange={() => toggleCheck(h.id)}
                             onClick={(e) => e.stopPropagation()} />
                      <span className={"dot " + (ci ? (ci.reachable ? "ok" : "bad")
                        : h.online === true ? "ok" : h.online === false ? "bad" : "")}
                        style={{ cursor: "pointer" }}
                        onClick={(e) => { e.stopPropagation(); runCheckin([h.id]); }}
                        title={"Click to ping this host" + (ci ? (ci.reachable ? " · last: reachable" : ` · last: unreachable${ci.detail ? " — " + ci.detail : ""}`) : "")} />
                      <span style={{ cursor: "pointer" }}
                            onClick={() => setSel(h)} onDoubleClick={() => openTerm(h)}
                            title="Click to select · double-click to open a terminal">{h.label}</span>
                      <span className="meta">{h.has_agent ? "Agent+SSH" : "SSH"} {h.address}
                        {h.online === false ? " · offline" : ""}</span>
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
        <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
          Check hosts and click “Open Terminal”, or double-click a single host — each opens in its own window.
        </div>
      </div>

      {/* RIGHT: selected host + collapsible actions on top, terminals below */}
      <div style={{ overflowY: "auto", paddingRight: 4 }}>
        {err && <div className="error-box" role="alert">{err}</div>}

        {sel && (
          <Section title="Selected Host">
            <SelectedHost host={sel} onTerminal={() => openTerm(sel)}
                          onChanged={(clearSel) => { if (clearSel) setSel(null); loadHosts(); }}
                          onErr={setErr} />
          </Section>
        )}

        <div className="connect-actions">
          <FleetActions hosts={hosts} checked={checked} onErr={setErr} />
          {sel && (
            <Collapsible title={`File Transfer — ${sel.label}`}>
              <FileTransfer host={sel} onErr={setErr} />
            </Collapsible>
          )}
          <SshEnroll onDone={loadHosts} onErr={setErr} />
        </div>
      </div>

      {showCheckin && checkin && (
        <div onClick={() => setShowCheckin(false)}
             style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 50,
                      display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
          <div onClick={(e) => e.stopPropagation()} className="card"
               role="dialog" aria-modal="true" aria-label="Check-in / ping results"
               style={{ width: "min(420px, 92vw)", maxHeight: "80vh", overflow: "auto" }}>
            <div className="spread" style={{ marginBottom: 8, alignItems: "center" }}>
              <strong>Check-In / Ping</strong>
              <button className="btn ghost sm" onClick={() => setShowCheckin(false)}>Close ✕</button>
            </div>
            <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
              <span style={{ color: checkin.every((r) => r.reachable) ? "var(--ok, #4ec07a)" : "#e0a83a" }}>
                {checkin.filter((r) => r.reachable).length} of {checkin.length} reachable</span>
              {checkinAt ? ` · ${new Date(checkinAt).toLocaleTimeString()}` : ""}
            </div>
            {[...checkin].sort((a, b) => (a.reachable === b.reachable ? 0 : a.reachable ? 1 : -1)).map((r) => (
              <div key={r.id || r.host} className="spread"
                   style={{ padding: "5px 0", borderTop: "1px solid var(--border)", gap: 8, alignItems: "center" }}>
                <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                  <span className={"dot " + (r.reachable ? "ok" : "bad")} />
                  <span>{r.host}</span>
                </span>
                <span className="faint" style={{ fontSize: 12, textAlign: "right",
                        color: r.reachable ? undefined : "#e06c6c" }}>
                  {r.reachable ? "reachable" : (r.detail || "unreachable")}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Collapsible panel for the secondary actions, collapsed by default so they
// don't push the terminals down the page.
function Collapsible({ title, children, defaultOpen = false }) {
  return (
    <details className="collapsible" {...(defaultOpen ? { open: true } : {})}>
      <summary>{title}</summary>
      <div className="card" style={{ marginTop: 8 }}>{children}</div>
    </details>
  );
}

function SelectedHost({ host, onTerminal, onChanged, onErr }) {
  const [env, setEnv] = useState(host.environment || "");
  // The environment currently shown in the header. Tracked separately from the
  // editable input so a successful Set Environment updates the header immediately —
  // the parent reloads the host LIST but the selected-host object keeps its old env.
  const [shownEnv, setShownEnv] = useState(host.environment || "");
  const [busy, setBusy] = useState("");
  useEffect(() => { setEnv(host.environment || ""); setShownEnv(host.environment || ""); }, [host]);

  async function disenroll() {
    // A pure-SSH host has no Sysible agent to tear down — the agent-oriented
    // removeHost() never touches the SSH connection record, which is why these
    // used to be undeletable. Route them to the dedicated SSH-forget path.
    if (!host.has_agent) {
      if (!window.confirm(
        `Forget SSH host ${host.label}? This removes the SSH connection record ` +
        `(and its pinned host key) from the controller. Nothing is changed on the ` +
        `host itself; you can re-connect it later from “SSH to a New Host”.`)) return;
      setBusy("rm"); onErr("");
      try { await api.deleteSshHost(host.id); onChanged(true); }
      catch (e) { onErr(e.message); }
      finally { setBusy(""); }
      return;
    }
    if (!window.confirm(
      `Disenroll ${host.label}? This removes the host from the controller. ` +
      `The agent on the host keeps running until you stop/uninstall it there, ` +
      `and can re-enroll unless you Force Delete (which purges its enrollment token).`)) return;
    setBusy("rm"); onErr("");
    try { await api.removeHost(host.id); onChanged(true); }
    catch (e) { onErr(e.message); }
    finally { setBusy(""); }
  }
  async function saveEnv() {
    setBusy("env"); onErr("");
    try { await api.setHostEnvironment(host.id, env.trim()); setShownEnv(env.trim()); onChanged(false); }
    catch (e) { onErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <strong>{host.label}</strong>{" "}
          <span className="badge">{host.has_agent ? "Agent + SSH" : "SSH"}</span>{" "}
          <span className="faint">{host.address} · {shownEnv || "Unassigned"}</span>
        </div>
        <div className="row">
          <button className="btn sm" onClick={onTerminal}>Open Terminal</button>
          <button className="btn sm danger" disabled={busy === "rm"} onClick={disenroll}>
            {busy === "rm" ? <span className="spin" /> : host.has_agent ? "Disenroll Host" : "Forget SSH Host"}
          </button>
        </div>
      </div>
      <div className="row" style={{ marginTop: 10, gap: 8 }}>
        <input style={{ maxWidth: 220 }} placeholder="Environment (e.g. Prod)" value={env}
               onChange={(e) => setEnv(e.target.value)} />
        <button className="btn sm ghost" disabled={busy === "env"} onClick={saveEnv}>Set Environment</button>
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div className="section-title" style={{ marginTop: 0 }}>{title}</div>
      <div className="card">{children}</div>
    </div>
  );
}

function FleetActions({ hosts, checked, onErr }) {
  const [running, setRunning] = useState("");
  const [results, setResults] = useState(null);
  const [script, setScript] = useState("");
  const [sudoPw, setSudoPw] = useState("");

  // Act on checked hosts; if none are checked, fall back to the whole fleet.
  const targets = checked.length ? checked : [];
  const scopeN = checked.length || hosts.length;
  const scopeLabel = checked.length ? `${checked.length} checked` : `all ${hosts.length}`;

  async function act(action, confirmMsg, command) {
    // Guard the script action with a clear message rather than a silently
    // disabled button: an operator who clicks "Run Script" before typing
    // anything should be told what's missing, not left wondering why nothing
    // happened.
    if (action === "script" && !(command || "").trim()) {
      onErr("Enter a script to run in the box above first.");
      return;
    }
    // Rebooting or powering off the ENTIRE fleet is the highest-blast-radius
    // action here — a plain OK is too easy to hit by accident. Require typing
    // the word, matching the strong guard used for system-critical paths.
    const critical = action === "reboot" || action === "poweroff";
    // Any hypervisor hosts in the target set? Warn that their guest VMs go down.
    const verb = action === "poweroff" ? "Powering off" : "Rebooting";
    const targetHosts = checked.length ? hosts.filter((h) => checked.includes(h.id)) : hosts;
    const vmWarn = critical ? hypervisorFleetWarning(targetHosts, verb) : "";
    const vmPre = vmWarn ? `${vmWarn}\n\n` : "";
    if (critical && checked.length === 0) {
      const word = action === "poweroff" ? "POWER OFF" : "REBOOT";
      const typed = window.prompt(
        vmPre +
        `You are about to ${word} ALL ${hosts.length} hosts in the fleet. ` +
        `Powered-off hosts will NOT come back until powered on out-of-band.\n\n` +
        `Type "${word}" to confirm:`);
      if ((typed || "").trim().toUpperCase() !== word) return;
    } else if (confirmMsg && !window.confirm(`${vmPre}${confirmMsg} (${scopeLabel} host${scopeN === 1 ? "" : "s"})`)) {
      return;
    }
    setRunning(action); setResults(null); onErr("");
    // The inline sudo password only makes sense for the script action (the
    // others run as root via the agent); send it only there.
    try { setResults(await api.fleet(action, targets, command, action === "script" ? sudoPw : "")); }
    catch (e) { onErr(e.message); }
    finally { setRunning(""); }
  }

  return (
    <Collapsible title={`Fleet Actions (${scopeLabel} host${scopeN === 1 ? "" : "s"})`}>
      <label className="field" style={{ marginTop: 0 }}>
        <span>Run a script on {checked.length ? `the ${checked.length} checked host(s)` : "all hosts"}</span>
        <textarea rows={2} value={script} onChange={(e) => setScript(e.target.value)}
                  placeholder="e.g. uname -a && uptime" />
        <span className="faint" style={{ fontSize: 12 }}>Tip: check hosts in the list to run on just those; with none checked it runs on all {hosts.length}.</span>
      </label>
      <label className="field" style={{ marginTop: 8 }}>
        <span>Sudo password <span className="faint">(optional — only for hosts that require one this run)</span></span>
        <input type="password" autoComplete="off" value={sudoPw}
               onChange={(e) => setSudoPw(e.target.value)}
               placeholder="Leave blank to use your stored sudo password" />
      </label>
      <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
        <button className="btn sm" disabled={!!running}
                onClick={() => act("script", "Run this script as root on", script)}>
          {running === "script" ? <span className="spin" /> : `Run Script on ${scopeLabel} host${scopeN === 1 ? "" : "s"}`}
        </button>
        {/* These actions target the SAME scope as Run Script (checked hosts, else all)
            — act() appends "(N checked/all hosts)" to the confirm text, so the button
            label + confirm reflect the real scope instead of always claiming "All". */}
        <button className="btn sm" disabled={running}
                onClick={() => act("restart_agent", "Restart the agent")}>
          Restart Agent on {scopeLabel} host{scopeN === 1 ? "" : "s"}</button>
        <button className="btn sm danger" disabled={running}
                onClick={() => act("reboot", "REBOOT")}>
          Reboot {scopeLabel} host{scopeN === 1 ? "" : "s"}</button>
        <button className="btn sm danger" disabled={running}
                onClick={() => act("poweroff", "POWER OFF")}>
          Power Off {scopeLabel} host{scopeN === 1 ? "" : "s"}</button>
      </div>
      {results && (
        <div className="result" style={{ marginTop: 10 }}>
          <HostResults rows={results.results} />
        </div>
      )}
    </Collapsible>
  );
}

function SshEnroll({ onDone, onErr }) {
  const [f, setF] = useState({ name: "", ip: "", username: "", password: "", environment: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [key, setKey] = useState("");

  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.value }));

  async function connect(e) {
    e.preventDefault(); setBusy(true); setMsg(""); onErr("");
    try {
      await api.enrollSsh(f);
      setMsg(`Enrolled ${f.name || f.ip}.`);
      setF({ name: "", ip: "", username: "", password: "", environment: "" });
      onDone();
    } catch (e2) { onErr(e2.message); }
    finally { setBusy(false); }
  }
  async function showKey() {
    try { setKey((await api.controllerKey()).public_key || ""); }
    catch (e) { onErr(e.message); }
  }

  return (
    <Collapsible title="SSH to a New Host (Not Yet Joined)">
      <p className="faint" style={{ marginTop: 0 }}>
        Only needed once per host. The password installs the controller key, then is discarded.
      </p>
      <form onSubmit={connect} className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input style={{ flex: 1, minWidth: 120 }} aria-label="Host name" placeholder="Host name" value={f.name} onChange={set("name")} />
        <input style={{ flex: 1, minWidth: 120 }} aria-label="IP address" placeholder="IP address" value={f.ip} onChange={set("ip")} />
        <input style={{ flex: 1, minWidth: 120 }} aria-label="Username" placeholder="Username" value={f.username} onChange={set("username")} />
        <input style={{ flex: 1, minWidth: 120 }} type="password" aria-label="SSH password" placeholder="SSH password" value={f.password} onChange={set("password")} />
        <input style={{ flex: 1, minWidth: 100 }} aria-label="Environment" placeholder="Environment" value={f.environment} onChange={set("environment")} />
        <button className="btn sm" disabled={busy || !f.ip || !f.username}>
          {busy ? <span className="spin" /> : "Connect Host"}
        </button>
        <button type="button" className="btn sm ghost" onClick={showKey}>Show Controller Public Key</button>
      </form>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {key && <div className="cmd-preview" style={{ marginTop: 8 }}>{key}</div>}
    </Collapsible>
  );
}
