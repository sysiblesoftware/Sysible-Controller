import React, { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import TerminalDock from "../components/TerminalDock.jsx";

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

  async function runCheckin() {
    setBusy("checkin"); setErr("");
    try { setCheckin((await api.checkin()).results); }
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
          <button className="btn ghost sm" disabled={busy === "checkin"} onClick={runCheckin}>
            {busy === "checkin" ? <span className="spin" /> : "Check In / Ping"}
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
                      {ci && <span className={"dot " + (ci.reachable ? "ok" : "bad")} />}
                      <span style={{ cursor: "pointer" }}
                            onClick={() => setSel(h)} onDoubleClick={() => openTerm(h)}
                            title="Click to select · double-click to open a terminal">{h.label}</span>
                      <span className="meta">{h.has_agent ? "Agent+SSH" : "SSH"} {h.address}</span>
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

      {/* RIGHT: stacked sections */}
      <div style={{ overflowY: "auto", paddingRight: 4 }}>
        {err && <div className="error-box">{err}</div>}

        <Section title="Selected Host">
          {sel ? (
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <strong>{sel.label}</strong>{" "}
                <span className="badge">{sel.has_agent ? "Agent + SSH" : "SSH"}</span>{" "}
                <span className="faint">{sel.address} · {sel.environment || "Ungrouped"}</span>
              </div>
              <button className="btn sm" onClick={() => openTerm(sel)}>Open Terminal</button>
            </div>
          ) : <span className="faint">No host selected. Click a host; double-click to open its terminal.</span>}
        </Section>

        <Section title="Terminals">
          <TerminalDock ref={dock} />
        </Section>

        <FleetActions hosts={hosts} checked={checked} onErr={setErr} />

        {sel && <FileTransfer host={sel} onErr={setErr} />}

        <SshEnroll onDone={loadHosts} onErr={setErr} />

        <Section title="RDP to a Windows Host">
          <div className="faint">
            RDP opens a desktop client on <em>your</em> machine, so it runs from the
            Sysible desktop app — a browser can't launch it directly. Use the desktop
            client for RDP, or ask about adding a browser RDP gateway.
          </div>
        </Section>
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

  // Act on checked hosts; if none are checked, fall back to the whole fleet.
  const targets = checked.length ? checked : [];
  const scopeN = checked.length || hosts.length;
  const scopeLabel = checked.length ? `${checked.length} checked` : `all ${hosts.length}`;

  async function act(action, confirmMsg, command) {
    if (confirmMsg && !window.confirm(`${confirmMsg} (${scopeLabel} host${scopeN === 1 ? "" : "s"})`)) return;
    setRunning(action); setResults(null); onErr("");
    try { setResults(await api.fleet(action, targets, command)); }
    catch (e) { onErr(e.message); }
    finally { setRunning(""); }
  }

  return (
    <Section title={`Fleet Actions (${scopeLabel} host${scopeN === 1 ? "" : "s"})`}>
      <label className="field" style={{ marginTop: 0 }}>
        <span>Run a script on all hosts</span>
        <textarea rows={2} value={script} onChange={(e) => setScript(e.target.value)}
                  placeholder="e.g. uname -a && uptime" />
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
        <div style={{ marginTop: 10 }}>
          {results.results.map((r, i) => (
            <div className="result" key={i}>
              <div className="rh">
                <span className={"dot " + (r.ok ? "ok" : "bad")} />
                <strong>{r.host}</strong>
                {r.code !== null && r.code !== undefined && <span className="faint">exit {r.code}</span>}
                {r.error && <span className="badge amber">{r.error}</span>}
              </div>
              {(r.stdout || r.stderr) && <pre>{r.stdout}{r.stderr}</pre>}
            </div>
          ))}
        </div>
      )}
    </Section>
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

  return (
    <Section title={`File Transfer — ${host.label}`}>
      <form onSubmit={upload} className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input style={{ flex: 2, minWidth: 160 }} placeholder="Remote destination path"
               value={remotePath} onChange={(e) => setRemotePath(e.target.value)} />
        <input style={{ flex: 2, minWidth: 160 }} type="file"
               onChange={(e) => setFile(e.target.files[0] || null)} />
        <button className="btn sm" disabled={busy || !file || !remotePath.trim()}>Upload</button>
      </form>
      <form onSubmit={download} className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
        <input style={{ flex: 3, minWidth: 200 }} placeholder="Remote file path to fetch"
               value={dnPath} onChange={(e) => setDnPath(e.target.value)} />
        <button className="btn sm ghost" disabled={!dnPath.trim()}>Download</button>
      </form>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
    </Section>
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
    <Section title="SSH to a New Host (Not Yet Joined)">
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
    </Section>
  );
}
