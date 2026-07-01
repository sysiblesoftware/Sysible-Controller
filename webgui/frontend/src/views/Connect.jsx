import React, { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import TerminalDock from "../components/TerminalDock.jsx";
import HostResults from "../components/HostResults.jsx";

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
  const dock = useRef(null);

  const toggleCheck = (id) =>
    setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));

  function loadHosts() {
    api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message));
  }
  useEffect(loadHosts, []);

  const groups = useMemo(() => {
    const m = {};
    for (const h of hosts) (m[h.environment || "Ungrouped"] ||= []).push(h);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [hosts]);

  function openTerm(h) { dock.current && dock.current.open(h.id, h.label); }

  const [checkinAt, setCheckinAt] = useState(0);
  const [showCheckin, setShowCheckin] = useState(false);
  async function runCheckin(targets) {
    setBusy("checkin"); setErr("");
    try { setCheckin((await api.checkin(targets || [])).results); setCheckinAt(Date.now()); setShowCheckin(true); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div className="three-pane">
      {/* LEFT: managed hosts */}
      <div className="host-pane">
        <strong style={{ fontSize: 13 }}>Managed Hosts (agent + SSH)</strong>
        <div className="ctl-row" style={{ marginTop: 8 }}>
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
        <div className="host-tree">
          {hosts.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled.</div>}
          {groups.map(([env, list]) => {
            const open = !collapsed[env];
            return (
              <div className="env-group" key={env}>
                <div className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}>
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
          Double-click a host to open its terminal.
        </div>
      </div>

      {/* RIGHT: selected host + collapsible actions on top, terminals below */}
      <div style={{ overflowY: "auto", paddingRight: 4 }}>
        {err && <div className="error-box">{err}</div>}

        {sel && (
          <Section title="Selected Host">
            <SelectedHost host={sel} onTerminal={() => openTerm(sel)}
                          onChanged={(clearSel) => { if (clearSel) setSel(null); loadHosts(); }}
                          onErr={setErr} />
          </Section>
        )}

        <div className="connect-actions">
          <FleetActions hosts={hosts} checked={checked} onErr={setErr} />
          {sel && <FileTransfer host={sel} onErr={setErr} />}
          <SshEnroll onDone={loadHosts} onErr={setErr} />
        </div>

        <Section title="Terminals">
          <TerminalDock ref={dock} />
        </Section>
      </div>

      {showCheckin && checkin && (
        <div onClick={() => setShowCheckin(false)}
             style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 50,
                      display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
          <div onClick={(e) => e.stopPropagation()} className="card"
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
  const [busy, setBusy] = useState("");
  useEffect(() => { setEnv(host.environment || ""); }, [host]);

  async function disenroll() {
    if (!window.confirm(
      `Disenroll ${host.label}? The host's agent keeps running but stops being managed ` +
      `by this controller. You can re-enroll it later.`)) return;
    setBusy("rm"); onErr("");
    try { await api.removeHost(host.id); onChanged(true); }
    catch (e) { onErr(e.message); }
    finally { setBusy(""); }
  }
  async function saveEnv() {
    setBusy("env"); onErr("");
    try { await api.setHostEnvironment(host.id, env.trim()); onChanged(false); }
    catch (e) { onErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <strong>{host.label}</strong>{" "}
          <span className="badge">{host.has_agent ? "Agent + SSH" : "SSH"}</span>{" "}
          <span className="faint">{host.address} · {host.environment || "Ungrouped"}</span>
        </div>
        <div className="row">
          <button className="btn sm" onClick={onTerminal}>Open Terminal</button>
          <button className="btn sm danger" disabled={busy === "rm"} onClick={disenroll}>
            {busy === "rm" ? <span className="spin" /> : "Disenroll Host"}
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
    if (confirmMsg && !window.confirm(`${confirmMsg} (${scopeLabel} host${scopeN === 1 ? "" : "s"})`)) return;
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
        <span>Run a script on all hosts</span>
        <textarea rows={2} value={script} onChange={(e) => setScript(e.target.value)}
                  placeholder="e.g. uname -a && uptime" />
      </label>
      <label className="field" style={{ marginTop: 8 }}>
        <span>Sudo password <span className="faint">(optional — only for hosts that require one this run)</span></span>
        <input type="password" autoComplete="off" value={sudoPw}
               onChange={(e) => setSudoPw(e.target.value)}
               placeholder="Leave blank to use your stored sudo password" />
      </label>
      <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
        <button className="btn sm" disabled={running || !script.trim()}
                onClick={() => act("script", null, script)}>
          {running === "script" ? <span className="spin" /> : "Run Script on All Hosts"}
        </button>
        <button className="btn sm" disabled={running}
                onClick={() => act("restart_agent", "Restart the agent on all hosts?")}>Restart Agent on All</button>
        <button className="btn sm danger" disabled={running}
                onClick={() => act("reboot", "REBOOT all hosts?")}>Reboot All</button>
        <button className="btn sm danger" disabled={running}
                onClick={() => act("poweroff", "POWER OFF all hosts?")}>Power Off All</button>
      </div>
      {results && (
        <div className="result" style={{ marginTop: 10 }}>
          <HostResults rows={results.results} />
        </div>
      )}
    </Collapsible>
  );
}

function FileTransfer({ host, onErr }) {
  const [remotePath, setRemotePath] = useState("");
  const [file, setFile] = useState(null);
  const [dnPath, setDnPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function upload(e) {
    e.preventDefault(); setBusy(true); setMsg(""); onErr("");
    try {
      const r = await api.uploadFile(host.id, remotePath.trim(), file);
      setMsg(`Uploaded ${r.filename} (${r.bytes} bytes) → ${r.remote_path}`);
      setFile(null); e.target.reset();
    } catch (e2) { onErr(e2.message); }
    finally { setBusy(false); }
  }
  function download(e) {
    e.preventDefault();
    window.location.href = api.downloadUrl(host.id, dnPath.trim());
  }

  const [browse, setBrowse] = useState(null); // "upload" | "download" target for the picker

  return (
    <Collapsible title={`File Transfer — ${host.label}`}>
      <form onSubmit={upload} className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input style={{ flex: 2, minWidth: 160 }} placeholder="Remote destination path"
               value={remotePath} onChange={(e) => setRemotePath(e.target.value)} />
        <button type="button" className="btn sm ghost" onClick={() => setBrowse("upload")}>Browse…</button>
        <input style={{ flex: 2, minWidth: 160 }} type="file"
               onChange={(e) => setFile(e.target.files[0] || null)} />
        <button className="btn sm" disabled={busy || !file || !remotePath.trim()}>Upload</button>
      </form>
      <form onSubmit={download} className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
        <input style={{ flex: 3, minWidth: 200 }} placeholder="Remote file path to fetch"
               value={dnPath} onChange={(e) => setDnPath(e.target.value)} />
        <button type="button" className="btn sm ghost" onClick={() => setBrowse("download")}>Browse…</button>
        <button className="btn sm" disabled={!dnPath.trim()}>Download</button>
      </form>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {browse && (
        <RemoteBrowse host={host} mode={browse} onErr={onErr}
          onClose={() => setBrowse(null)}
          onPick={(path) => { (browse === "upload" ? setRemotePath : setDnPath)(path); setBrowse(null); }} />
      )}
    </Collapsible>
  );
}

// Lightweight remote file/folder picker: lists a directory on the host (via a
// one-off `ls` exec) and lets you click into folders or pick an entry, so you
// don't have to remember and type the full remote path. In "upload" mode a
// folder is the selection (destination dir); in "download" mode a file is.
function RemoteBrowse({ host, mode, onPick, onClose, onErr }) {
  const [dir, setDir] = useState(".");
  const [entries, setEntries] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function list(d) {
    setBusy(true); setErr("");
    const cmd = `cd '${d.replace(/'/g, "'\\''")}' 2>/dev/null && pwd && ls -1Ap`;
    try {
      const r = await api.fleet("script", [host.id], cmd);
      const res = (r.results || [])[0] || {};
      const out = (res.stdout || "");
      const lines = out.split("\n").filter((l) => l !== "");
      if (lines.length === 0) { setErr(res.stderr || "Could not read that directory."); return; }
      const cwd = lines.shift();
      setDir(cwd);
      setEntries(lines);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }
  useEffect(() => { list("."); /* eslint-disable-next-line */ }, []);

  const join = (name) => (dir.endsWith("/") ? dir + name : dir + "/" + name);

  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ maxWidth: 560, textAlign: "left" }}>
        <h3 style={{ textAlign: "left", marginBottom: 4 }}>{mode === "upload" ? "Choose destination folder" : "Choose file to download"}</h3>
        <div className="faint mono" style={{ fontSize: 12, marginBottom: 8, wordBreak: "break-all" }}>{host.label}:{dir}</div>
        {err && <div className="error-box">{err}</div>}
        <div style={{ maxHeight: "46vh", overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
          {busy ? <div className="empty" style={{ padding: 20 }}><span className="spin" /></div>
            : entries.map((name) => {
                const isDir = name.endsWith("/");
                const clean = isDir ? name.slice(0, -1) : name;
                return (
                  <div key={name} className="host-row" style={{ cursor: "pointer", justifyContent: "space-between" }}
                       onClick={() => { if (isDir) list(name === "../" ? join("..") : join(clean)); else if (mode === "download") onPick(join(clean)); }}
                       onDoubleClick={() => { if (isDir && mode === "upload") onPick(join(clean)); }}>
                    <span>{isDir ? "📁 " : "📄 "}{clean}</span>
                    {isDir && mode === "upload" &&
                      <button type="button" className="btn ghost sm" onClick={(e) => { e.stopPropagation(); onPick(join(clean)); }}>Use this folder</button>}
                  </div>
                );
              })}
        </div>
        <div className="row" style={{ justifyContent: "space-between", marginTop: 12 }}>
          <span className="faint">{mode === "upload" ? "Click a folder to open it; use the button to pick it." : "Click a folder to open it; click a file to pick it."}</span>
          <div className="row" style={{ gap: 8 }}>
            {mode === "upload" && <button className="btn sm" onClick={() => onPick(dir)}>Use “{dir}”</button>}
            <button className="btn ghost sm" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
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
        <input style={{ flex: 1, minWidth: 120 }} placeholder="Host name" value={f.name} onChange={set("name")} />
        <input style={{ flex: 1, minWidth: 120 }} placeholder="IP address" value={f.ip} onChange={set("ip")} />
        <input style={{ flex: 1, minWidth: 120 }} placeholder="Username" value={f.username} onChange={set("username")} />
        <input style={{ flex: 1, minWidth: 120 }} type="password" placeholder="SSH password" value={f.password} onChange={set("password")} />
        <input style={{ flex: 1, minWidth: 100 }} placeholder="Environment" value={f.environment} onChange={set("environment")} />
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
