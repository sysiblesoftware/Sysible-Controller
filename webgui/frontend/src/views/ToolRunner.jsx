import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostPicker from "./HostPicker.jsx";

export default function ToolRunner() {
  const [catalog, setCatalog] = useState(null);
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState("");
  const [openTool, setOpenTool] = useState(null);
  const [selected, setSelected] = useState(null); // action object
  const [params, setParams] = useState({});
  const [targets, setTargets] = useState([]);
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState(null);
  const [runErr, setRunErr] = useState("");

  useEffect(() => {
    Promise.all([api.tools(), api.hosts()])
      .then(([t, h]) => {
        setCatalog(t.tools || []);
        setHosts(h.hosts || []);
      })
      .catch((x) => setErr(x.message));
  }, []);

  function pickAction(tool, action) {
    setSelected(action);
    setResults(null);
    setRunErr("");
    const init = {};
    for (const p of action.params) {
      init[p.name] = p.type === "checkbox"
        ? Boolean(p.default)
        : (p.default ?? "");
    }
    setParams(init);
  }

  function setParam(name, val) {
    setParams((p) => ({ ...p, [name]: val }));
  }

  const missing = useMemo(() => {
    if (!selected) return [];
    return selected.params
      .filter((p) => p.required && p.type !== "checkbox")
      .filter((p) => String(params[p.name] ?? "").trim() === "")
      .map((p) => p.label);
  }, [selected, params]);

  async function run() {
    setRunning(true);
    setRunErr("");
    setResults(null);
    try {
      const r = await api.runTool(selected.name, targets, params);
      setResults(r);
    } catch (e) {
      setRunErr(e.message);
    } finally {
      setRunning(false);
    }
  }

  if (err) return <div className="error-box">{err}</div>;
  if (catalog === null) return <div className="empty"><span className="spin" /></div>;

  return (
    <div className="runner">
      <div className="tool-list">
        {catalog.map((group) => (
          <div className="tool-group" key={group.tool}>
            <button
              className="tg-head"
              onClick={() => setOpenTool(openTool === group.tool ? null : group.tool)}
            >
              <span>{group.tool}</span>
              <span className="faint">{openTool === group.tool ? "▾" : "▸"}</span>
            </button>
            {openTool === group.tool && (
              <div className="tool-actions">
                {group.actions.map((a) => (
                  <button
                    key={a.name}
                    className={
                      (selected && selected.name === a.name ? "active " : "") +
                      (a.danger ? "danger-act" : "")
                    }
                    onClick={() => pickAction(group, a)}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="runner-form">
        {!selected ? (
          <div className="empty">Pick a tool on the left to get started.</div>
        ) : (
          <>
            <div className="run-head">
              <div>
                <h3 style={{ margin: "0 0 4px" }}>{selected.label}</h3>
                <div className="muted">{selected.description}</div>
              </div>
              {selected.danger && <span className="badge amber">Destructive</span>}
            </div>

            {selected.params.length > 0 && (
              <div style={{ marginTop: 8 }}>
                {selected.params.map((p) => (
                  <ParamField key={p.name} p={p} value={params[p.name]} onChange={setParam} />
                ))}
              </div>
            )}

            <HostPicker hosts={hosts} value={targets} onChange={setTargets} />

            <div className="row">
              <button
                className={"btn" + (selected.danger ? " danger" : "")}
                disabled={running || targets.length === 0 || missing.length > 0}
                onClick={run}
              >
                {running ? <span className="spin" /> : `Run on ${targets.length || "0"} host${targets.length === 1 ? "" : "s"}`}
              </button>
              {missing.length > 0 && (
                <span className="faint">Required: {missing.join(", ")}</span>
              )}
              {targets.length === 0 && (
                <span className="faint">Select at least one target.</span>
              )}
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
                      <span className="host">{r.host}</span>
                      {r.code !== null && r.code !== undefined && (
                        <span className="faint">exit {r.code}</span>
                      )}
                      {r.error && <span className="badge amber">{r.error}</span>}
                    </div>
                    {(r.stdout || r.stderr) && (
                      <pre>{r.stdout}{r.stderr ? (r.stdout ? "\n" : "") + r.stderr : ""}</pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ParamField({ p, value, onChange }) {
  if (p.type === "checkbox") {
    return (
      <div className="checkrow">
        <input
          id={"p_" + p.name}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(p.name, e.target.checked)}
        />
        <label htmlFor={"p_" + p.name}>{p.label}</label>
      </div>
    );
  }
  return (
    <label className="field">
      <span>
        {p.label}{p.required ? " *" : ""}
        {p.help ? <span className="faint"> — {p.help}</span> : null}
      </span>
      {p.type === "select" ? (
        <select value={value ?? ""} onChange={(e) => onChange(p.name, e.target.value)}>
          {(p.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <input
          type={p.type === "password" ? "password" : p.type === "number" ? "number" : "text"}
          value={value ?? ""}
          onChange={(e) => onChange(p.name, e.target.value)}
        />
      )}
    </label>
  );
}
