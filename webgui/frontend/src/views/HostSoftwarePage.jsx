import React, { useMemo, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ResultsPane from "../components/ResultsPane.jsx";
import PkgResults from "../components/PkgResults.jsx";

// Bespoke Host Software Management page, mirroring the desktop: a package-name
// field, a live clickable installed-packages list (click to fill), action
// buttons, and Upload & Install a local package file onto the checked hosts.
export default function HostSoftwarePage({ hosts = [], onRefreshHosts }) {
  const [targets, setTargets] = useState([]);
  const [name, setName] = useState("");
  const [packages, setPackages] = useState([]);
  const [listHost, setListHost] = useState("");
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState([]);
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState(false);

  const filtered = useMemo(() => {
    const f = name.trim().toLowerCase();
    return (f ? packages.filter((p) => p.toLowerCase().includes(f)) : packages).slice(0, 500);
  }, [packages, name]);

  async function listInstalled() {
    const host = listHost || targets[0];
    if (!host) { setErr("Check a host first (the list is read from one host)."); return; }
    setBusy("list"); setErr("");
    try { const d = await api.packagesList(host); setPackages(d.packages || []); setListHost(host); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function run(action, params, label) {
    if (targets.length === 0) { setErr("Check one or more hosts first."); return; }
    setBusy(action); setErr("");
    try { const r = await api.runTool(action, targets, params); setResults((p) => [{ label, ...r, at: Date.now() }, ...p]); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function uploadInstall(e) {
    const file = e.target.files[0]; if (!file) return;
    if (targets.length === 0) { setErr("Check one or more hosts first."); e.target.value = ""; return; }
    setBusy("upload"); setErr("");
    try { const r = await api.installLocalPackage(file, targets); setResults((p) => [{ label: `Upload & install ${file.name}`, ...r, at: Date.now() }, ...p]); }
    catch (e2) { setErr(e2.message); }
    finally { setBusy(""); e.target.value = ""; }
  }

  return (
    <div className="tool-flex">
      {!expanded && <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check hosts to act on; the installed list is read from one host." />}

      {!expanded && (
      <div className="tool-actions-col"><div className="tool-actions-scroll">
        <label className="field" style={{ marginTop: 0 }}>
          <span>Package name(s) (space-separated; also filters the list)</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. nginx" />
        </label>

        <fieldset className="tool-group-box" style={{ marginTop: 12 }}><legend>Query</legend>
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
            "Check status" reports, per checked host, whether the named package(s) are installed and which version.
          </div>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy || !name.trim()}
                    onClick={() => run("pkg_status", { names: name }, `Status: ${name}`)}>Check status on checked hosts</button>
            <button className="btn sm" disabled={busy} onClick={() => run("pkg_detect_env", {}, "Detect package manager / OS")}>Detect Package Manager / OS</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => run("pkg_search", { term: name }, `Search ${name}`)}>Search Available</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => run("pkg_query", { name }, `Query ${name}`)}>Query Package Info</button>
            <button className="btn sm" disabled={busy || !name} onClick={() => run("pkg_verify", { name }, `Verify ${name}`)}>Verify Package Integrity</button>
          </div>
        </fieldset>

        <div className="section-title">Browse installed on one host — click a name to fill the field</div>
        <div className="row" style={{ gap: 8, marginBottom: 6, alignItems: "center" }}>
          <button className="btn sm ghost" disabled={busy === "list"} onClick={listInstalled}>
            {busy === "list" ? <span className="spin" /> : (listHost ? `Refresh list (${listHost})` : "List installed on a host")}</button>
          <span className="faint" style={{ fontSize: 12 }}>reads from one host, as a package-name picker</span>
        </div>
        <div className="card" style={{ maxHeight: 180, overflowY: "auto", padding: 6 }}>
          {filtered.length === 0 ? <div className="faint" style={{ padding: 8 }}>Click “List installed on a host” to populate this picker.</div>
            : filtered.map((p) => (
              <div key={p} className="host-row" style={{ cursor: "pointer", paddingLeft: 6 }} onClick={() => setName(p)}>{p}</div>
            ))}
        </div>

        <fieldset className="tool-group-box" style={{ marginTop: 14 }}><legend>Install / Update / Remove</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy || !name} onClick={() => run("pkg_install", { names: name }, `Install ${name}`)}>Install</button>
            <button className="btn sm" disabled={busy} onClick={() => run("pkg_update", { names: name }, name ? `Update ${name}` : "Update / upgrade all")}>Update / Upgrade</button>
            <button className="btn sm danger" disabled={busy || !name} onClick={() => run("pkg_remove", { names: name }, `Remove ${name}`)}>Remove</button>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <label className="btn sm" style={{ cursor: targets.length ? "pointer" : "not-allowed", opacity: targets.length ? 1 : 0.5 }}>
              {busy === "upload" ? <span className="spin" /> : "Upload & Install on Checked Hosts…"}
              <input type="file" style={{ display: "none" }} disabled={busy || targets.length === 0} onChange={uploadInstall} />
            </label>
            <span className="faint">Pushes a local .deb/.rpm to the {targets.length} checked host(s) and installs it.</span>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Maintenance</legend>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy} onClick={() => run("pkg_clean_cache", {}, "Clean package cache")}>Clean Package Cache</button>
          </div>
        </fieldset>
        {err && <div className="error-box">{err}</div>}
      </div></div>
      )}

      <ResultsPane results={results} setResults={setResults} expanded={expanded}
                   onToggleExpand={() => setExpanded((v) => !v)}
                   renderRun={(run) => <PkgResults run={run} />}
                   empty="Run an action — install/remove/status show a clear per-host result here." />
    </div>
  );
}
