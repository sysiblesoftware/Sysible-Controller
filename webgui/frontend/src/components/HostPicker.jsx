import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Loads the enrolled fleet and lets the admin pick targets. Grouped by
// environment to mirror the desktop host panel. Selection is lifted to
// the parent (ToolPage) via onChange(selectedIds).
export default function HostPicker({ selected, onChange, agentOnlyHint }) {
  const [hosts, setHosts] = useState([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const reload = () => {
    setLoading(true);
    api
      .hosts()
      .then((d) => setHosts(d.hosts || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };
  useEffect(reload, []);

  const groups = useMemo(() => {
    const by = {};
    for (const h of hosts) {
      const env = h.environment || "Unassigned";
      (by[env] = by[env] || []).push(h);
    }
    return by;
  }, [hosts]);

  const toggle = (id) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    onChange([...next]);
  };
  const selectAll = () => onChange(hosts.map((h) => h.id));
  const clear = () => onChange([]);

  return (
    <aside className="hostpicker card">
      <div className="hostpicker-head">
        <strong>Target hosts</strong>
        <span className="muted small">{selected.length} selected</span>
      </div>
      <div className="hostpicker-actions">
        <button className="btn-ghost small" onClick={selectAll}>All</button>
        <button className="btn-ghost small" onClick={clear}>None</button>
        <button className="btn-ghost small" onClick={reload}>Reload</button>
      </div>
      {agentOnlyHint && (
        <div className="hint small">
          This tool needs root — only agent (or agent+SSH) hosts will succeed.
        </div>
      )}
      {loading && <div className="muted small">Loading hosts…</div>}
      {error && <div className="alert error small">{error}</div>}
      {!loading && hosts.length === 0 && !error && (
        <div className="muted small">No hosts enrolled.</div>
      )}
      <div className="hostpicker-list">
        {Object.entries(groups).map(([env, list]) => (
          <div key={env} className="host-env">
            <div className="host-env-name">{env}</div>
            {list.map((h) => (
              <label key={h.id} className="host-row">
                <input
                  type="checkbox"
                  checked={selected.includes(h.id)}
                  onChange={() => toggle(h.id)}
                />
                <span className="host-label">{h.label}</span>
                <span className={`host-tag tag-${h.kind}`}>{h.type_text}</span>
              </label>
            ))}
          </div>
        ))}
      </div>
    </aside>
  );
}
