import React, { useMemo, useState, useEffect } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ParamField from "../components/ParamField.jsx";

// Three-pane tool page (desktop layout): host tree on the left, action tabs +
// form on the right, per-host results below. One page per catalog tool; each
// action is a tab.
export default function ToolPage({ tool, hosts, onRefreshHosts, onBack }) {
  const [activeIdx, setActiveIdx] = useState(0);
  const [targets, setTargets] = useState([]);
  const [params, setParams] = useState({});
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState(null);
  const [runErr, setRunErr] = useState("");

  const action = tool.actions[activeIdx];

  // Reset the form when switching actions.
  useEffect(() => {
    const init = {};
    for (const p of action.params) init[p.name] = p.type === "checkbox" ? Boolean(p.default) : (p.default ?? "");
    setParams(init);
    setResults(null);
    setRunErr("");
  }, [activeIdx, tool]);

  function setParam(name, val) { setParams((p) => ({ ...p, [name]: val })); }

  const missing = useMemo(() => {
    return action.params
      .filter((p) => p.required && p.type !== "checkbox")
      .filter((p) => String(params[p.name] ?? "").trim() === "")
      .map((p) => p.label);
  }, [action, params]);

  async function run() {
    setRunning(true); setRunErr(""); setResults(null);
    try {
      setResults(await api.runTool(action.name, targets, params));
    } catch (e) { setRunErr(e.message); }
    finally { setRunning(false); }
  }

  return (
    <div className="three-pane">
      <HostTree
        hosts={hosts}
        value={targets}
        onChange={setTargets}
        onRefresh={onRefreshHosts}
        footer="Check one or more hosts, pick an action, then Run."
      />

      <div style={{ overflowY: "auto" }}>
        <div className="tabs">
          {tool.actions.map((a, i) => (
            <button key={a.name} className={i === activeIdx ? "active" : ""}
                    onClick={() => setActiveIdx(i)}>
              {a.label}{a.danger ? " ⚠" : ""}
            </button>
          ))}
        </div>

        <div className="spread">
          <div>
            <h3 style={{ margin: "0 0 4px" }}>{action.label}</h3>
            <div className="muted">{action.description}</div>
          </div>
          {action.danger && <span className="badge amber">Destructive</span>}
        </div>

        {action.params.map((p) => (
          <ParamField key={p.name} p={p} value={params[p.name]} onChange={setParam} />
        ))}

        <div className="row" style={{ marginTop: 16 }}>
          <button className={"btn" + (action.danger ? " danger" : "")}
                  disabled={running || targets.length === 0 || missing.length > 0} onClick={run}>
            {running ? <span className="spin" /> : `Run on ${targets.length} host${targets.length === 1 ? "" : "s"}`}
          </button>
          {missing.length > 0 && <span className="faint">Required: {missing.join(", ")}</span>}
          {targets.length === 0 && <span className="faint">Select at least one host.</span>}
        </div>

        {runErr && <div className="error-box">{runErr}</div>}

        {results && (
          <div style={{ marginTop: 16 }}>
            <div className="muted">Command dispatched:</div>
            <div className="cmd-preview">{results.command}</div>
            {results.results.map((r, i) => (
              <div className="result" key={i}>
                <div className="rh">
                  <span className={"dot " + (r.ok ? "ok" : "bad")} />
                  <span style={{ fontWeight: 600 }}>{r.host}</span>
                  {r.code !== null && r.code !== undefined && <span className="faint">exit {r.code}</span>}
                  {r.error && <span className="badge amber">{r.error}</span>}
                </div>
                {(r.stdout || r.stderr) && (
                  <pre>{r.stdout}{r.stderr ? (r.stdout ? "\n" : "") + r.stderr : ""}</pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
