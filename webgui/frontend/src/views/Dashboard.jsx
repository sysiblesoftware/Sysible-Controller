import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { searchTasks } from "../featureSearch.js";

const VERDICT_COLOR = { OK: "#4ec07a", WARNING: "#e0a83a", CRITICAL: "#e06c6c", OFFLINE: "#7a7a7a" };

// Inline SVG donut (no chart-library dependency): one ring segment per value.
function Donut({ segments, size = 88, stroke = 13 }) {
  const r = (size - stroke) / 2, C = 2 * Math.PI * r;
  const total = segments.reduce((s, x) => s + x.value, 0);
  let off = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--border)" strokeWidth={stroke} />
        {total > 0 && segments.filter((s) => s.value > 0).map((s, i) => {
          const len = (s.value / total) * C;
          const el = <circle key={i} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={s.color}
            strokeWidth={stroke} strokeDasharray={`${len} ${C - len}`} strokeDashoffset={-off} />;
          off += len; return el;
        })}
      </g>
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central"
            style={{ fontSize: 20, fontWeight: 700, fill: "var(--text)" }}>{total}</text>
    </svg>
  );
}

// Horizontal usage meter (disk/mem), colored by threshold.
function Meter({ label, pct }) {
  const v = pct == null ? null : Math.max(0, Math.min(100, pct));
  const color = v == null ? "var(--border)" : v >= 90 ? VERDICT_COLOR.CRITICAL : v >= 75 ? VERDICT_COLOR.WARNING : VERDICT_COLOR.OK;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "3px 0" }}>
      <span className="faint" style={{ width: 34, fontSize: 12 }}>{label}</span>
      <div style={{ flex: 1, height: 8, borderRadius: 4, background: "var(--border)", overflow: "hidden" }}>
        <div style={{ width: (v || 0) + "%", height: "100%", background: color }} />
      </div>
      <span className="faint" style={{ width: 36, textAlign: "right", fontSize: 12 }}>{v == null ? "—" : v + "%"}</span>
    </div>
  );
}

function FleetHostCard({ h }) {
  const v = (h.verdict || "OK").toUpperCase();
  const noData = v === "OFFLINE" || h.disk == null;
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px" }}>
      <div className="spread" style={{ marginBottom: noData ? 0 : 6 }}>
        <span><span className="dot" style={{ background: VERDICT_COLOR[v] || VERDICT_COLOR.OFFLINE }} />{" "}
          <strong>{h.host}</strong>
          <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>{h.environment}</span></span>
        <span className="faint" style={{ fontSize: 11 }}>{v}</span>
      </div>
      {noData ? (
        <div className="faint" style={{ fontSize: 12 }}>{h.error || "no data"}</div>
      ) : (
        <>
          <Meter label="disk" pct={h.disk} />
          <Meter label="mem" pct={h.mem} />
          <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
            load {h.load1 ?? "—"} / {h.cores} core{h.cores === 1 ? "" : "s"}
            {h.failed ? ` · ${h.failed} failed unit${h.failed > 1 ? "s" : ""}` : ""}
          </div>
        </>
      )}
    </div>
  );
}

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

  // Fleet health snapshot (on-demand sweep; optional auto-refresh).
  const [fleet, setFleet] = useState([]);
  const [fleetAt, setFleetAt] = useState(0);
  const [fleetLoading, setFleetLoading] = useState(false);
  const [fleetErr, setFleetErr] = useState("");
  const [fleetAuto, setFleetAuto] = useState(false);

  const loadFleet = useCallback(() => {
    setFleetLoading(true); setFleetErr("");
    api.fleetHealth()
      .then((d) => { setFleet(d.hosts || []); setFleetAt(Date.now()); })
      .catch((e) => setFleetErr(e.message))
      .finally(() => setFleetLoading(false));
  }, []);
  useEffect(() => { loadFleet(); }, [loadFleet]);
  useEffect(() => {
    if (!fleetAuto) return undefined;
    const t = setInterval(loadFleet, 30000);
    return () => clearInterval(t);
  }, [fleetAuto, loadFleet]);

  const fleetSummary = useMemo(() => {
    const counts = { OK: 0, WARNING: 0, CRITICAL: 0, OFFLINE: 0 };
    for (const h of fleet) {
      const v = (h.verdict || "OK").toUpperCase();
      counts[v] = (counts[v] || 0) + 1;
    }
    const order = { CRITICAL: 0, WARNING: 1, OFFLINE: 2, OK: 3 };
    const sorted = [...fleet].sort((a, b) =>
      (order[(a.verdict || "OK").toUpperCase()] ?? 9) - (order[(b.verdict || "OK").toUpperCase()] ?? 9)
      || (b.disk || 0) - (a.disk || 0));
    return { counts, sorted };
  }, [fleet]);

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

      <div className="card" style={{ marginTop: 14 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <strong>Fleet health</strong>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            {fleetAt > 0 && <span className="faint" style={{ fontSize: 12 }}>updated {new Date(fleetAt).toLocaleTimeString()}</span>}
            <label className="checkrow" style={{ margin: 0 }}>
              <input type="checkbox" checked={fleetAuto} onChange={(e) => setFleetAuto(e.target.checked)} />
              <span className="faint">Auto (30s)</span>
            </label>
            <button className="btn ghost sm" onClick={loadFleet} disabled={fleetLoading}>
              {fleetLoading ? <span className="spin" /> : "Refresh"}
            </button>
          </div>
        </div>
        {fleetErr && <div className="error-box">{fleetErr}</div>}
        {fleet.length === 0 ? (
          <div className="empty" style={{ padding: 16 }}>
            {fleetLoading ? "Gathering fleet health…" : "No host metrics yet — click Refresh."}
          </div>
        ) : (
          <div className="row" style={{ gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div className="row" style={{ gap: 14, alignItems: "center" }}>
              <Donut segments={[
                { value: fleetSummary.counts.OK, color: VERDICT_COLOR.OK },
                { value: fleetSummary.counts.WARNING, color: VERDICT_COLOR.WARNING },
                { value: fleetSummary.counts.CRITICAL, color: VERDICT_COLOR.CRITICAL },
                { value: fleetSummary.counts.OFFLINE, color: VERDICT_COLOR.OFFLINE },
              ]} />
              <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>
                <span><span className="dot" style={{ background: VERDICT_COLOR.OK }} /> {fleetSummary.counts.OK} OK</span>
                <span><span className="dot" style={{ background: VERDICT_COLOR.WARNING }} /> {fleetSummary.counts.WARNING} warning</span>
                <span><span className="dot" style={{ background: VERDICT_COLOR.CRITICAL }} /> {fleetSummary.counts.CRITICAL} critical</span>
                <span><span className="dot" style={{ background: VERDICT_COLOR.OFFLINE }} /> {fleetSummary.counts.OFFLINE} offline</span>
              </div>
            </div>
            <div style={{ flex: 1, minWidth: 300, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(270px, 1fr))", gap: 10 }}>
              {fleetSummary.sorted.map((h) => <FleetHostCard key={h.host} h={h} />)}
            </div>
          </div>
        )}
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
