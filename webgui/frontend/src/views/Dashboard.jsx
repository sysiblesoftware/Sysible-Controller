import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { searchTasks } from "../featureSearch.js";

// Fleet overview: at-a-glance metrics + recent activity. Navigation lives in
// the sidebar, so this is a real home screen rather than a duplicate tile grid.

function seenAgo(v) {
  if (v === null || v === undefined || v === "") return Infinity;
  let n = Number(v);
  if (!isFinite(n)) { const d = new Date(v); n = isNaN(d) ? NaN : d.getTime() / 1000; }
  if (!isFinite(n)) return Infinity;
  if (n > 1e12) n /= 1000; // ms → s
  return Date.now() / 1000 - n;
}
function fmtWhen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let n = Number(v); let d;
  if (isFinite(n)) { if (n < 1e12) n *= 1000; d = new Date(n); } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

const ONLINE_WINDOW_S = 150;

export default function Dashboard({ role, edition, onOpen }) {
  const [q, setQ] = useState("");
  const [agents, setAgents] = useState([]);
  const [activity, setActivity] = useState([]);
  const isSuper = role === "superuser";

  useEffect(() => {
    api.agents().then((d) => setAgents(d.agents || [])).catch(() => {});
    if (isSuper) api.activity(12).then((d) => setActivity(d.activity || [])).catch(() => {});
  }, [isSuper]);

  const results = useMemo(() => searchTasks(q), [q]);

  const m = useMemo(() => {
    const total = agents.length;
    const online = agents.filter((a) => seenAgo(a.last_seen) <= ONLINE_WINDOW_S).length;
    const envs = new Set(agents.map((a) => a.environment || "Unassigned")).size;
    return { total, online, offline: total - online, envs };
  }, [agents]);

  const recent = useMemo(
    () => [...activity].sort((a, b) => (b.id || 0) - (a.id || 0)).slice(0, 10),
    [activity]
  );

  if (q.trim()) {
    return (
      <div>
        <input className="search-bar" autoFocus
               placeholder='Search for a task, e.g. "create a user" or "add a repository"…'
               value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="card" style={{ padding: 6 }}>
          {results.length === 0 ? (
            <div className="empty" style={{ padding: 16 }}>No matching task. Try “create a user”, “firewall”, “restart service”…</div>
          ) : results.map((r, i) => (
            <button key={i} className="search-result"
                    onClick={() => onOpen(r.section, { tool: r.tool, tab: r.tab })}>
              <span style={{ fontWeight: 600 }}>{r.title}</span>
              <span className="faint" style={{ marginLeft: 8 }}>
                {r.section === "sysadmin" && r.tool ? `System Administration › ${r.tool}` : r.section}
              </span>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div>
      <input className="search-bar"
             placeholder='Search for a task, e.g. "create a user" or "add a repository"…'
             value={q} onChange={(e) => setQ(e.target.value)} />

      <div className="metric-row">
        <div className="metric">
          <div className="label">Hosts enrolled</div>
          <div className="value">{m.total}{edition?.host_limit ? <span className="faint" style={{ fontSize: 14, fontWeight: 400 }}>/ {edition.host_limit}</span> : null}</div>
        </div>
        <div className="metric">
          <div className="label">Online</div>
          <div className="value">{m.online}<span className="dot ok" /></div>
        </div>
        <div className="metric">
          <div className="label">Offline / stale</div>
          <div className="value">{m.offline}{m.offline > 0 && <span className="dot bad" />}</div>
        </div>
        <div className="metric">
          <div className="label">Environments</div>
          <div className="value">{m.envs}</div>
        </div>
      </div>

      <div className="overview-grid">
        {isSuper && (
          <div>
            <div className="section-title">Recent activity</div>
            <div className="overview-feed">
              {recent.length === 0 ? (
                <div className="empty" style={{ padding: 20 }}>No recent activity.</div>
              ) : recent.map((e) => (
                <div className="feed-row" key={e.id}>
                  <span style={{ flex: 1 }}>
                    <strong>{e.username || "—"}</strong> {e.description || "ran a command"}
                    {e.host ? <span className="faint"> · {e.host}</span> : null}
                  </span>
                  <span className="feed-when">{fmtWhen(e.timestamp)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {!isSuper && (
          <div className="card">
            <strong>Welcome back</strong>
            <p className="faint" style={{ marginTop: 8, marginBottom: 0 }}>
              Use the navigation on the left, or the search box above, to jump straight to a tool.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
