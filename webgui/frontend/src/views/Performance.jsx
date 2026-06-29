import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Distinct hue per environment (overview) and per host (drill-down). Two
// separate palettes so the two levels read as visually different, and so a
// host line never gets confused with an environment line.
const ENV_COLORS = [
  "#5b9bd5", "#e0a83a", "#4ec07a", "#b07ad0",
  "#e06c6c", "#46c5c5", "#d98c5f", "#8a8af0",
];
const HOST_COLORS = [
  "#6fb1e0", "#f2c14e", "#67d39b", "#c98fe0",
  "#ef8a8a", "#5fd3d3", "#eaa06f", "#a0a0f7",
  "#bdd35a", "#e08fb8",
];

// CPU is load1 normalised to a percentage of the host's core count, so it
// shares a 0–100ish axis with memory and disk; it can exceed 100 (load > cores).
const METRICS = [
  { key: "cpu", label: "CPU load", suffix: "%", fixed100: false,
    valueOf: (s) => (s.load1 == null || !s.cores ? null : (s.load1 / s.cores) * 100) },
  { key: "mem", label: "Memory", suffix: "%", fixed100: true, valueOf: (s) => s.mem },
  { key: "disk", label: "Disk", suffix: "%", fixed100: true, valueOf: (s) => s.disk },
];

const WINDOWS = [
  { label: "1h", value: 3600 },
  { label: "6h", value: 21600 },
  { label: "24h", value: 86400 },
];

function fmtClock(tSec) {
  const d = new Date(tSec * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Bucket-average a set of samples (across many hosts) into evenly spaced points
// over [t0, t1] — used to draw one line per environment in the overview.
function bucketAverage(samples, valueOf, t0, t1, buckets = 80) {
  const span = Math.max(1, t1 - t0);
  const w = span / buckets;
  const sum = new Array(buckets).fill(0);
  const cnt = new Array(buckets).fill(0);
  for (const s of samples) {
    const v = valueOf(s);
    if (v == null || !isFinite(v)) continue;
    let i = Math.floor((s.t - t0) / w);
    if (i < 0) i = 0; if (i >= buckets) i = buckets - 1;
    sum[i] += v; cnt[i] += 1;
  }
  const pts = [];
  for (let i = 0; i < buckets; i++) {
    if (cnt[i] > 0) pts.push({ t: t0 + (i + 0.5) * w, v: sum[i] / cnt[i] });
  }
  return pts;
}

// A single inline-SVG multi-line chart (no chart-library dependency).
function LineChart({ series, t0, t1, suffix, fixed100 }) {
  const W = 1000, H = 200, padL = 40, padR = 14, padT = 10, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const yMax = useMemo(() => {
    if (fixed100) return 100;
    let mx = 0;
    for (const s of series) for (const p of s.points) if (p.v > mx) mx = p.v;
    return Math.max(100, Math.ceil(mx / 25) * 25);
  }, [series, fixed100]);

  const x = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * plotW;
  const y = (v) => padT + (1 - Math.max(0, Math.min(v, yMax)) / yMax) * plotH;

  const gridFracs = [0, 0.25, 0.5, 0.75, 1];
  const xticks = [t0, t0 + (t1 - t0) / 2, t1];
  const hasData = series.some((s) => s.points.length > 0);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%"
         style={{ display: "block", height: "auto" }}>
      {gridFracs.map((f, i) => {
        const yv = yMax * f, yy = y(yv);
        return (
          <g key={i}>
            <line x1={padL} y1={yy} x2={W - padR} y2={yy}
                  stroke="var(--border)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
            <text x={padL - 6} y={yy + 3} textAnchor="end"
                  style={{ fontSize: 10, fill: "var(--text-faint)" }}>
              {Math.round(yv)}{suffix}
            </text>
          </g>
        );
      })}
      {xticks.map((t, i) => (
        <text key={i} x={x(t)} y={H - 6}
              textAnchor={i === 0 ? "start" : i === xticks.length - 1 ? "end" : "middle"}
              style={{ fontSize: 10, fill: "var(--text-faint)" }}>{fmtClock(t)}</text>
      ))}
      {series.map((s) => (
        s.points.length === 0 ? null : (
          <polyline key={s.key} fill="none" stroke={s.color} strokeWidth="1.8"
                    strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke"
                    points={s.points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")} />
        )
      ))}
      {!hasData && (
        <text x={W / 2} y={H / 2} textAnchor="middle"
              style={{ fontSize: 12, fill: "var(--text-faint)" }}>no samples in this window</text>
      )}
    </svg>
  );
}

function Legend({ items, onClick, selectedKey }) {
  return (
    <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
      {items.map((it) => (
        <button key={it.key} onClick={onClick ? () => onClick(it.key) : undefined}
                className="btn ghost sm"
                style={{
                  display: "flex", alignItems: "center", gap: 6,
                  cursor: onClick ? "pointer" : "default",
                  borderColor: selectedKey === it.key ? it.color : "var(--border)",
                  opacity: selectedKey && selectedKey !== it.key ? 0.55 : 1,
                }}>
          <span style={{ width: 10, height: 10, borderRadius: 3, background: it.color }} />
          <span>{it.label}</span>
          {it.sub != null && <span className="faint" style={{ fontSize: 11 }}>{it.sub}</span>}
        </button>
      ))}
    </div>
  );
}

export default function Performance() {
  const [window, setWindow] = useState(3600);
  const [data, setData] = useState({ hosts: [], now: 0 });
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [auto, setAuto] = useState(false);
  const [at, setAt] = useState(0);
  const [selectedEnv, setSelectedEnv] = useState(null);

  const load = useCallback(() => {
    setLoading(true); setErr("");
    api.fleetMetrics(window)
      .then((d) => { setData(d || { hosts: [], now: 0 }); setAt(Date.now()); })
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [window]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto) return undefined;
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [auto, load]);

  const now = data.now || Date.now() / 1000;
  const t0 = now - window, t1 = now;

  // Stable env → color, sorted by name so colors don't reshuffle on refresh.
  const envColor = useMemo(() => {
    const names = [...new Set(data.hosts.map((h) => h.environment || "Unassigned"))].sort();
    const m = {};
    names.forEach((n, i) => { m[n] = ENV_COLORS[i % ENV_COLORS.length]; });
    return m;
  }, [data.hosts]);

  const envGroups = useMemo(() => {
    const g = {};
    for (const h of data.hosts) {
      const env = h.environment || "Unassigned";
      (g[env] = g[env] || []).push(h);
    }
    return g;
  }, [data.hosts]);

  const envNames = useMemo(() => Object.keys(envGroups).sort(), [envGroups]);

  // Drill-down: stable host → color within the selected environment.
  const drillHosts = selectedEnv ? (envGroups[selectedEnv] || []) : [];
  const hostColor = useMemo(() => {
    const sorted = [...drillHosts].sort((a, b) => a.hostname.localeCompare(b.hostname));
    const m = {};
    sorted.forEach((h, i) => { m[h.host_id] = HOST_COLORS[i % HOST_COLORS.length]; });
    return m;
  }, [drillHosts]);

  // One series-set per metric, built from either env-averages or host lines.
  const seriesFor = useCallback((metric) => {
    if (!selectedEnv) {
      return envNames.map((env) => ({
        key: env, label: env, color: envColor[env],
        points: bucketAverage(
          envGroups[env].flatMap((h) => h.samples), metric.valueOf, t0, t1),
      }));
    }
    return [...drillHosts]
      .sort((a, b) => a.hostname.localeCompare(b.hostname))
      .map((h) => ({
        key: h.host_id, label: h.hostname, color: hostColor[h.host_id],
        points: h.samples
          .map((s) => ({ t: s.t, v: metric.valueOf(s) }))
          .filter((p) => p.v != null && isFinite(p.v)),
      }));
  }, [selectedEnv, envNames, envColor, envGroups, drillHosts, hostColor, t0, t1]);

  const envLegend = envNames.map((env) => ({
    key: env, color: envColor[env], label: env,
    sub: `${envGroups[env].length} host${envGroups[env].length === 1 ? "" : "s"}`,
  }));
  const hostLegend = [...drillHosts]
    .sort((a, b) => a.hostname.localeCompare(b.hostname))
    .map((h) => ({ key: h.host_id, color: hostColor[h.host_id], label: h.hostname }));

  const empty = data.hosts.length === 0;

  return (
    <div>
      <div className="card">
        <div className="spread" style={{ marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
          <div className="row" style={{ gap: 10, alignItems: "center" }}>
            <strong>Fleet performance</strong>
            {selectedEnv && (
              <button className="btn ghost sm" onClick={() => setSelectedEnv(null)}>
                ← All environments
              </button>
            )}
            {selectedEnv && (
              <span className="faint">
                <span className="dot" style={{ background: envColor[selectedEnv] }} /> {selectedEnv}
              </span>
            )}
          </div>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="row" style={{ gap: 4 }}>
              {WINDOWS.map((w) => (
                <button key={w.value}
                        className={"btn sm " + (window === w.value ? "" : "ghost")}
                        onClick={() => setWindow(w.value)}>{w.label}</button>
              ))}
            </div>
            {at > 0 && <span className="faint" style={{ fontSize: 12 }}>updated {new Date(at).toLocaleTimeString()}</span>}
            <label className="checkrow" style={{ margin: 0 }}>
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              <span className="faint">Auto (30s)</span>
            </label>
            <button className="btn ghost sm" onClick={load} disabled={loading}>
              {loading ? <span className="spin" /> : "Refresh"}
            </button>
          </div>
        </div>

        {err && <div className="error-box">{err}</div>}

        {empty ? (
          <div className="empty" style={{ padding: 20 }}>
            {loading ? "Loading metrics…" : (
              <>No performance samples yet. Agents report load, memory, and disk on
              heartbeat — data appears within a minute or two of an up-to-date agent
              checking in. SSH-only hosts aren’t sampled.</>
            )}
          </div>
        ) : (
          <>
            <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>
              {selectedEnv
                ? "Each line is a host in this environment. Click a host to highlight it."
                : "Each line is an environment (averaged across its hosts). Click an environment to drill into its hosts."}
            </div>
            <Legend items={selectedEnv ? hostLegend : envLegend}
                    onClick={selectedEnv ? undefined : (env) => setSelectedEnv(env)} />

            <div style={{ display: "grid", gap: 16, marginTop: 8 }}>
              {METRICS.map((metric) => (
                <div key={metric.key}>
                  <div className="section-title" style={{ marginBottom: 2 }}>
                    {metric.label} <span className="faint" style={{ fontWeight: 400 }}>({metric.suffix})</span>
                  </div>
                  <LineChart series={seriesFor(metric)} t0={t0} t1={t1}
                             suffix={metric.suffix} fixed100={metric.fixed100} />
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
