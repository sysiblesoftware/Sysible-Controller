import React, { useMemo, useState } from "react";

// Desktop-style host pane: hosts grouped by environment with checkboxes,
// plus Refresh / Select All / Deselect All / Collapse / Expand controls.
// `value` is an array of selected host ids; `onChange` gets the next array.
export default function HostTree({ hosts, value, onChange, onRefresh, footer }) {
  const groups = useMemo(() => {
    const m = {};
    for (const h of hosts) {
      const env = h.environment || "Ungrouped";
      (m[env] = m[env] || []).push(h);
    }
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [hosts]);

  const [collapsed, setCollapsed] = useState({});

  const toggleHost = (id) =>
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  const allIds = hosts.map((h) => h.id);
  const allSel = hosts.length > 0 && value.length === hosts.length;

  return (
    <div className="host-pane">
      <strong style={{ fontSize: 13 }}>Target Hosts</strong>
      <div className="ctl-row" style={{ marginTop: 8 }}>
        {onRefresh && <button className="btn ghost sm" onClick={onRefresh}>Refresh</button>}
        <button className="btn ghost sm" onClick={() => onChange(allIds)}>Select All</button>
        <button className="btn ghost sm" onClick={() => onChange([])}>Deselect All</button>
      </div>
      <div className="ctl-row">
        <button className="btn ghost sm" onClick={() =>
          setCollapsed(Object.fromEntries(groups.map(([e]) => [e, true])))}>Collapse All</button>
        <button className="btn ghost sm" onClick={() => setCollapsed({})}>Expand All</button>
      </div>

      <div className="host-tree">
        {hosts.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled.</div>}
        {groups.map(([env, list]) => {
          const isOpen = !collapsed[env];
          const groupIds = list.map((h) => h.id);
          const allInGroup = groupIds.every((id) => value.includes(id));
          return (
            <div className="env-group" key={env}>
              <div className="env-head" onClick={() =>
                setCollapsed((c) => ({ ...c, [env]: isOpen }))}>
                <input
                  type="checkbox"
                  className="env-check"
                  title={`Select all in ${env}`}
                  checked={allInGroup}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => {
                    e.stopPropagation();
                    onChange(allInGroup
                      ? value.filter((id) => !groupIds.includes(id))
                      : [...new Set([...value, ...groupIds])]);
                  }}
                />
                {isOpen ? "▾" : "▸"} {env}
              </div>
              {isOpen && list.map((h) => {
                // The dot stays reachability (online/offline); a critical posture
                // finding turns the host NAME red so the two signals don't collide.
                const crit = h.critical === true;
                const critTitle = crit ? "Critical: " + (h.critical_reasons || []).join(", ") : undefined;
                return (
                <label className="host-row" key={h.id}>
                  <input type="checkbox" checked={value.includes(h.id)} onChange={() => toggleHost(h.id)} />
                  <span className={"dot " + (h.online === true ? "ok" : h.online === false ? "bad" : "")}
                        title={h.online === false ? "Offline" : h.online === true ? "Online" : ""} />
                  <span className="host-name" style={crit ? { color: "#e06c6c", fontWeight: 600 } : undefined} title={critTitle}>{h.label}</span>
                  <span className="meta">{h.has_agent ? "Agent" : "SSH"}{h.online === false ? " · offline" : ""}</span>
                  {crit && <span className="meta crit" title={critTitle}>critical</span>}
                </label>
                );
              })}
            </div>
          );
        })}
      </div>
      {footer && <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>{footer}</div>}
    </div>
  );
}
