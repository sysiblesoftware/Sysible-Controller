import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostPicker from "../components/HostPicker.jsx";
import Results from "../components/Results.jsx";

// One tool = a list of actions (from the server catalog) sharing the
// same target host selection. The left rail is the action list + its
// param form; the right is the host picker and results. Every tool uses
// this same component - tool-specific behavior lives entirely in the
// server-side action catalog, so adding tools needs no new UI code.
export default function ToolPage({ tool, onBack }) {
  const [catalog, setCatalog] = useState(null);
  const [active, setActive] = useState(null); // action name
  const [form, setForm] = useState({});
  const [targets, setTargets] = useState([]);
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.tools().then((d) => {
      const entry = d.tools.find((t) => t.tool === tool);
      setCatalog(entry || { tool, actions: [] });
      if (entry && entry.actions.length) selectAction(entry.actions[0]);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tool]);

  const actions = catalog ? catalog.actions : [];
  const activeAction = useMemo(
    () => actions.find((a) => a.name === active),
    [actions, active]
  );

  function selectAction(a) {
    setActive(a.name);
    const init = {};
    for (const p of a.params) init[p.name] = p.default ?? "";
    setForm(init);
    setRun(null);
  }

  const setField = (name, value) => setForm((f) => ({ ...f, [name]: value }));

  const submit = async (e) => {
    e.preventDefault();
    if (!targets.length) {
      setRun({ error: "Select at least one target host." });
      return;
    }
    setBusy(true);
    setRun({ pending: true });
    try {
      const res = await api.runTool(active, targets, form);
      setRun(res);
    } catch (err) {
      setRun({ error: err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="toolpage">
      <div className="toolpage-bar">
        <button className="btn-ghost" onClick={onBack}>← Tools</button>
        <h2 className="page-title inline">{tool}</h2>
      </div>

      <div className="toolpage-grid">
        <div className="tool-left">
          <div className="action-tabs">
            {actions.map((a) => (
              <button
                key={a.name}
                className={`action-tab ${a.name === active ? "active" : ""} ${a.danger ? "danger" : ""}`}
                onClick={() => selectAction(a)}
              >
                {a.label}
              </button>
            ))}
          </div>

          {activeAction && (
            <form className="card action-form" onSubmit={submit}>
              {activeAction.description && (
                <p className="muted small">{activeAction.description}</p>
              )}
              {activeAction.params.map((p) => (
                <label key={p.name} className="field">
                  <span>
                    {p.label}
                    {!p.required && <em className="muted"> (optional)</em>}
                  </span>
                  {p.type === "checkbox" ? (
                    <input
                      type="checkbox"
                      checked={!!form[p.name]}
                      onChange={(e) => setField(p.name, e.target.checked)}
                    />
                  ) : p.type === "select" ? (
                    <select
                      value={form[p.name] ?? ""}
                      onChange={(e) => setField(p.name, e.target.value)}
                    >
                      {p.options.map((o) => (
                        <option key={o} value={o}>{o}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type={p.type === "password" ? "password" : p.type === "number" ? "number" : "text"}
                      value={form[p.name] ?? ""}
                      placeholder={p.help}
                      onChange={(e) => setField(p.name, e.target.value)}
                    />
                  )}
                  {p.help && p.type !== "password" && (
                    <small className="muted">{p.help}</small>
                  )}
                </label>
              ))}
              <button
                className={`btn ${activeAction.danger ? "btn-danger" : ""}`}
                disabled={busy}
              >
                {busy ? "Running…" : activeAction.danger ? `${activeAction.label} (confirm)` : activeAction.label}
              </button>
            </form>
          )}

          <Results run={run} />
        </div>

        <HostPicker
          selected={targets}
          onChange={setTargets}
          agentOnlyHint={tool !== "Run Command"}
        />
      </div>
    </div>
  );
}
