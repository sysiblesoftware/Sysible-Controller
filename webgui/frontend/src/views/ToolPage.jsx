import React, { useMemo, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ParamField from "../components/ParamField.jsx";

// Faithful desktop tool-page layout: host tree (left); the tool's actions
// organized into TABS, each holding titled GROUP sections, each section a
// bordered box of action rows (inline fields + a button); a shared results
// area at the bottom with "Clear All Results". Mirrors client/*_page.py.
export default function ToolPage({ tool, hosts, onRefreshHosts }) {
  const [targets, setTargets] = useState([]);
  const [params, setParams] = useState({});      // { [actionName]: { [paramName]: value } }
  const [results, setResults] = useState([]);    // newest first
  const [runningAction, setRunningAction] = useState("");
  const [err, setErr] = useState("");

  // Build tab -> group -> [actions] preserving registration order.
  const tabs = useMemo(() => {
    const order = [];
    const map = new Map();
    for (const a of tool.actions) {
      const tab = a.tab || tool.tool;
      const group = a.group || "";
      if (!map.has(tab)) { map.set(tab, new Map()); order.push(tab); }
      const groups = map.get(tab);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(a);
    }
    return { order, map };
  }, [tool]);

  const [activeTab, setActiveTab] = useState(tabs.order[0]);
  const currentTab = tabs.order.includes(activeTab) ? activeTab : tabs.order[0];

  function pval(action, p) {
    const a = params[action.name] || {};
    if (a[p.name] !== undefined) return a[p.name];
    return p.type === "checkbox" ? Boolean(p.default) : (p.default ?? "");
  }
  function setParam(actionName, name, val) {
    setParams((s) => ({ ...s, [actionName]: { ...(s[actionName] || {}), [name]: val } }));
  }

  async function run(action) {
    if (targets.length === 0) { setErr("Check one or more hosts on the left first."); return; }
    if (action.danger && !window.confirm(`${action.label} on ${targets.length} host(s)?`)) return;
    setErr(""); setRunningAction(action.name);
    const built = {};
    for (const p of action.params) built[p.name] = pval(action, p);
    try {
      const r = await api.runTool(action.name, targets, built);
      setResults((prev) => [{ label: action.label, ...r, at: Date.now() }, ...prev]);
    } catch (e) { setErr(e.message); }
    finally { setRunningAction(""); }
  }

  const groups = tabs.map.get(currentTab) || new Map();

  return (
    <div className="three-pane">
      <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check one or more hosts, then run an action." />

      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        {tabs.order.length > 1 && (
          <div className="tabs">
            {tabs.order.map((t) => (
              <button key={t} className={t === currentTab ? "active" : ""} onClick={() => setActiveTab(t)}>{t}</button>
            ))}
          </div>
        )}

        <div style={{ flex: 1, overflowY: "auto", paddingRight: 4 }}>
          {[...groups.entries()].map(([groupTitle, acts]) => (
            <fieldset key={groupTitle || "_"} className="tool-group-box">
              {groupTitle && <legend>{groupTitle}</legend>}
              {acts.map((a) => (
                <ActionRow key={a.name} action={a} running={runningAction === a.name}
                           pval={pval} setParam={setParam} onRun={() => run(a)} />
              ))}
            </fieldset>
          ))}
          {err && <div className="error-box">{err}</div>}
        </div>

        <div className="results-bar">
          <span className="faint">Run an action above; results appear below (newest first).</span>
          <button className="btn ghost sm" onClick={() => setResults([])} disabled={!results.length}>Clear All Results</button>
        </div>
        <div style={{ maxHeight: "34vh", overflowY: "auto" }}>
          {results.map((res, i) => (
            <div className="result" key={res.at + "-" + i}>
              <div className="rh"><strong>{res.label}</strong>
                <span className="faint mono" style={{ fontSize: 11 }}>{new Date(res.at).toLocaleTimeString()}</span></div>
              {res.results.map((r, j) => (
                <div key={j} style={{ borderTop: "1px solid var(--border)" }}>
                  <div className="rh"><span className={"dot " + (r.ok ? "ok" : "bad")} /><span>{r.host}</span>
                    {r.code != null && <span className="faint">exit {r.code}</span>}
                    {r.error && <span className="badge amber">{r.error}</span>}</div>
                  {(r.stdout || r.stderr) && <pre>{r.stdout}{r.stderr}</pre>}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// One action: inline param fields followed by its run button. Param-less
// actions render as a lone button (like the desktop's Status/Reload rows).
function ActionRow({ action, running, pval, setParam, onRun }) {
  const hasParams = action.params.length > 0;
  return (
    <div className="action-row">
      {hasParams && (
        <div className="action-fields">
          {action.params.map((p) => (
            <div key={p.name} className="action-field">
              <ParamField p={p} value={pval(action, p)} onChange={(n, v) => setParam(action.name, n, v)} />
            </div>
          ))}
        </div>
      )}
      <button className={"btn sm" + (action.danger ? " danger" : "")} disabled={running}
              onClick={onRun} title={action.description}>
        {running ? <span className="spin" /> : action.label}
      </button>
    </div>
  );
}
