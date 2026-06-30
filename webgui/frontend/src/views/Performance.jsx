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

// All charted metrics. `kind` drives the y-axis + formatting: "pct" (0–100),
// "bytes" (throughput, auto KB/MB/GB), or "num" (plain). CPU prefers the agent's
// real cpu% and falls back to load/cores for agents that predate the richer
// sampling, so the CPU chart still shows something for un-updated hosts.
const METRICS = [
  { key: "cpu", label: "CPU", kind: "pct",
    valueOf: (s) => (s.cpu != null ? s.cpu : (s.load1 != null && s.cores ? (s.load1 / s.cores) * 100 : null)) },
  { key: "mem", label: "Memory", kind: "pct", valueOf: (s) => s.mem },
  { key: "swap", label: "Swap", kind: "pct", valueOf: (s) => s.swap },
  { key: "disk", label: "Disk usage", kind: "pct", valueOf: (s) => s.disk },
  { key: "net_rx", label: "Network in", kind: "bytes", valueOf: (s) => s.net_rx },
  { key: "net_tx", label: "Network out", kind: "bytes", valueOf: (s) => s.net_tx },
  { key: "io_r", label: "Disk read", kind: "bytes", valueOf: (s) => s.io_r },
  { key: "io_w", label: "Disk write", kind: "bytes", valueOf: (s) => s.io_w },
  { key: "load1", label: "Load (1m)", kind: "num", valueOf: (s) => s.load1 },
  { key: "procs", label: "Processes", kind: "num", valueOf: (s) => s.procs },
];

const WINDOWS = [
  { label: "1h", value: 3600 },
  { label: "6h", value: 21600 },
  { label: "24h", value: 86400 },
];

function fmtClock(tSec) {
  return new Date(tSec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtBytes(v) {
  if (v == null || !isFinite(v)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let n = v;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

function fmtMetric(kind, v, withRate = false) {
  if (v == null || !isFinite(v)) return "—";
  if (kind === "pct") return `${Math.round(v)}%`;
  if (kind === "bytes") return fmtBytes(v) + (withRate ? "/s" : "");
  return Math.round(v * 10) / 10;
}

// Round up to a "nice" axis maximum (1/2/5 × 10ⁿ).
function niceCeil(v) {
  if (!v || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return m * p;
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
function LineChart({ series, t0, t1, kind }) {
  const W = 1000, H = 190, padL = 52, padR = 14, padT = 10, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const yMax = useMemo(() => {
    let mx = 0;
    for (const s of series) for (const p of s.points) if (p.v > mx) mx = p.v;
    if (kind === "pct") return Math.max(100, Math.ceil(mx / 25) * 25);
    return niceCeil(mx || 1);
  }, [series, kind]);

  const x = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * plotW;
  const y = (v) => padT + (1 - Math.max(0, Math.min(v, yMax)) / yMax) * plotH;

  const gridFracs = [0, 0.25, 0.5, 0.75, 1];
  const xticks = [t0, t0 + (t1 - t0) / 2, t1];
  const hasData = series.some((s) => s.points.length > 0);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", height: "auto" }}>
      {gridFracs.map((f, i) => {
        const yv = yMax * f, yy = y(yv);
        return (
          <g key={i}>
            <line x1={padL} y1={yy} x2={W - padR} y2={yy}
                  stroke="var(--border)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
            <text x={padL - 6} y={yy + 3} textAnchor="end"
                  style={{ fontSize: 10, fill: "var(--text-faint)" }}>{fmtMetric(kind, yv)}</text>
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

// --- per-host snapshot (current detail) -----------------------------------

function Bar({ pct, color }) {
  const v = Math.max(0, Math.min(100, pct || 0));
  const c = color || (v >= 90 ? "#e06c6c" : v >= 75 ? "#e0a83a" : "#4ec07a");
  return (
    <div style={{ height: 7, borderRadius: 4, background: "var(--border)", overflow: "hidden" }}>
      <div style={{ width: v + "%", height: "100%", background: c }} />
    </div>
  );
}

function StatCard({ label, value }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "8px 12px", minWidth: 96 }}>
      <div className="faint" style={{ fontSize: 11 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

function HostSnapshot({ hostId }) {
  const [snap, setSnap] = useState(undefined); // undefined=loading, null=none
  const [ts, setTs] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setErr("");
    api.hostSnapshot(hostId)
      .then((d) => { setSnap(d?.snapshot || null); setTs(d?.ts || null); })
      .catch((e) => { setErr(e.message); setSnap(null); });
  }, [hostId]);
  useEffect(() => { setSnap(undefined); load(); }, [load]);

  if (snap === undefined) return <div className="empty" style={{ padding: 16 }}><span className="spin" /></div>;
  if (err) return <div className="error-box">{err}</div>;
  if (!snap) {
    return (
      <div className="empty" style={{ padding: 16 }}>
        No detail snapshot yet for this host. It appears within a minute or two of an
        up-to-date agent checking in (older agents report history but not this detail).
      </div>
    );
  }

  const mem = snap.mem || {};
  const Section = ({ title, children }) => (
    <div className="card" style={{ padding: "12px 14px" }}>
      <div className="section-title" style={{ marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  );

  return (
    <div>
      <div className="spread" style={{ margin: "4px 0 10px" }}>
        <span className="faint" style={{ fontSize: 12 }}>
          {snap.procs != null && <>{snap.procs} processes</>}
          {snap.threads != null && <> · {snap.threads} threads</>}
        </span>
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          {ts && <span className="faint" style={{ fontSize: 12 }}>as of {new Date(ts * 1000).toLocaleTimeString()}</span>}
          <button className="btn ghost sm" onClick={load}>Refresh detail</button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 12, alignItems: "start" }}>
        {snap.percpu && snap.percpu.length > 0 && (
          <Section title={`Per-core CPU (${snap.percpu.length})`}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))", gap: "6px 12px" }}>
              {snap.percpu.map((c, i) => (
                <div key={i} className="row" style={{ gap: 6, alignItems: "center" }}>
                  <span className="faint" style={{ fontSize: 11, width: 34 }}>cpu{i}</span>
                  <div style={{ flex: 1 }}><Bar pct={c} /></div>
                  <span style={{ fontSize: 11, width: 32, textAlign: "right" }}>{Math.round(c)}%</span>
                </div>
              ))}
            </div>
          </Section>
        )}

        <Section title="Memory">
          {[
            ["Total", mem.total_mb], ["Available", mem.available_mb], ["Free", mem.free_mb],
            ["Cached", mem.cached_mb], ["Buffers", mem.buffers_mb],
            ["Swap total", mem.swap_total_mb], ["Swap used", mem.swap_used_mb],
          ].map(([k, v]) => (
            <div key={k} className="spread" style={{ padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
              <span className="faint" style={{ fontSize: 13 }}>{k}</span>
              <span style={{ fontSize: 13 }}>{v == null ? "—" : (v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v} MB`)}</span>
            </div>
          ))}
        </Section>

        {snap.mounts && snap.mounts.length > 0 && (
          <Section title="Filesystems">
            {snap.mounts.map((m) => (
              <div key={m.mount} style={{ padding: "5px 0", borderBottom: "1px solid var(--border)" }}>
                <div className="spread" style={{ fontSize: 13 }}>
                  <span style={{ wordBreak: "break-all" }}>{m.mount}</span>
                  <span className="faint">{m.used_gb} / {m.total_gb} GB · {m.pct}%</span>
                </div>
                <div style={{ marginTop: 4 }}><Bar pct={m.pct} /></div>
              </div>
            ))}
          </Section>
        )}

        {snap.net && snap.net.length > 0 && (
          <Section title="Network interfaces">
            {snap.net.map((n) => (
              <div key={n.name} className="spread" style={{ padding: "5px 0", borderBottom: "1px solid var(--border)", fontSize: 13 }}>
                <span>{n.name}</span>
                <span className="faint">
                  ↓ {fmtBytes(n.rx_bps)}/s · ↑ {fmtBytes(n.tx_bps)}/s
                  {(n.rx_err || n.tx_err || n.rx_drop || n.tx_drop) ?
                    ` · err ${n.rx_err + n.tx_err}, drop ${n.rx_drop + n.tx_drop}` : ""}
                </span>
              </div>
            ))}
          </Section>
        )}

        {snap.top_cpu && snap.top_cpu.length > 0 && (
          <Section title="Top processes — CPU">
            <ProcTable rows={snap.top_cpu} metric="cpu" />
          </Section>
        )}
        {snap.top_mem && snap.top_mem.length > 0 && (
          <Section title="Top processes — memory">
            <ProcTable rows={snap.top_mem} metric="mem" />
          </Section>
        )}
      </div>
    </div>
  );
}

function ProcTable({ rows, metric }) {
  return (
    <div>
      {rows.map((p) => (
        <div key={p.pid} className="spread" style={{ padding: "4px 0", borderBottom: "1px solid var(--border)", fontSize: 13 }}>
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            <span className="faint" style={{ fontSize: 11, marginRight: 6 }}>{p.pid}</span>{p.name}
          </span>
          <span className="faint" style={{ whiteSpace: "nowrap", marginLeft: 8 }}>
            {metric === "cpu" ? `${p.cpu}% · ${p.mem_mb} MB` : `${p.mem_mb} MB${p.mem_pct != null ? ` · ${p.mem_pct}%` : ""}`}
          </span>
        </div>
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
  const [selectedHostId, setSelectedHostId] = useState(null);

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

  const drillHosts = selectedEnv ? (envGroups[selectedEnv] || []) : [];
  const hostColor = useMemo(() => {
    const sorted = [...drillHosts].sort((a, b) => a.hostname.localeCompare(b.hostname));
    const m = {};
    sorted.forEach((h, i) => { m[h.host_id] = HOST_COLORS[i % HOST_COLORS.length]; });
    return m;
  }, [drillHosts]);

  const selectedHost = useMemo(
    () => data.hosts.find((h) => h.host_id === selectedHostId) || null,
    [data.hosts, selectedHostId]);

  // Series builder for the overview / env drill (env-average or per-host lines).
  const seriesFor = useCallback((metric) => {
    if (!selectedEnv) {
      return envNames.map((env) => ({
        key: env, label: env, color: envColor[env],
        points: bucketAverage(envGroups[env].flatMap((h) => h.samples), metric.valueOf, t0, t1),
      }));
    }
    return [...drillHosts]
      .sort((a, b) => a.hostname.localeCompare(b.hostname))
      .map((h) => ({
        key: h.host_id, label: h.hostname, color: hostColor[h.host_id],
        points: h.samples.map((s) => ({ t: s.t, v: metric.valueOf(s) })).filter((p) => p.v != null && isFinite(p.v)),
      }));
  }, [selectedEnv, envNames, envColor, envGroups, drillHosts, hostColor, t0, t1]);

  // Single-host series for the per-host drill-down.
  const hostSeriesFor = useCallback((metric) => {
    if (!selectedHost) return [];
    return [{
      key: selectedHost.host_id, label: selectedHost.hostname, color: HOST_COLORS[0],
      points: selectedHost.samples.map((s) => ({ t: s.t, v: metric.valueOf(s) })).filter((p) => p.v != null && isFinite(p.v)),
    }];
  }, [selectedHost]);

  const latest = selectedHost && selectedHost.samples.length ? selectedHost.samples[selectedHost.samples.length - 1] : null;

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
          <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <strong>Fleet performance</strong>
            {(selectedEnv || selectedHost) && (
              <button className="btn ghost sm" onClick={() => { setSelectedEnv(null); setSelectedHostId(null); }}>
                ← All environments
              </button>
            )}
            {selectedHost && (
              <button className="btn ghost sm" onClick={() => setSelectedHostId(null)}>
                ← {selectedEnv || "hosts"}
              </button>
            )}
            {selectedHost ? (
              <span className="faint"><span className="dot" style={{ background: HOST_COLORS[0] }} /> {selectedHost.hostname}</span>
            ) : selectedEnv ? (
              <span className="faint"><span className="dot" style={{ background: envColor[selectedEnv] }} /> {selectedEnv}</span>
            ) : null}
          </div>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="row" style={{ gap: 4 }}>
              {WINDOWS.map((w) => (
                <button key={w.value} className={"btn sm " + (window === w.value ? "" : "ghost")}
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
              <>No performance samples yet. Agents report CPU, memory, swap, disk,
              network, and I/O on heartbeat — data appears within a minute or two of an
              up-to-date agent checking in. SSH-only hosts aren’t sampled.</>
            )}
          </div>
        ) : selectedHost ? (
          <>
            {/* Current-value stat strip from the latest sample. */}
            {latest && (
              <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
                <StatCard label="CPU" value={fmtMetric("pct", METRICS[0].valueOf(latest))} />
                <StatCard label="Memory" value={fmtMetric("pct", latest.mem)} />
                <StatCard label="Swap" value={fmtMetric("pct", latest.swap)} />
                <StatCard label="Disk" value={fmtMetric("pct", latest.disk)} />
                <StatCard label="Load 1m" value={fmtMetric("num", latest.load1)} />
                <StatCard label="Net in" value={fmtMetric("bytes", latest.net_rx, true)} />
                <StatCard label="Net out" value={fmtMetric("bytes", latest.net_tx, true)} />
                <StatCard label="Processes" value={fmtMetric("num", latest.procs)} />
              </div>
            )}

            <div className="section-title" style={{ marginBottom: 6 }}>History</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(440px, 1fr))", gap: 16 }}>
              {METRICS.map((metric) => (
                <div key={metric.key}>
                  <div className="faint" style={{ fontSize: 12, marginBottom: 2 }}>{metric.label}</div>
                  <LineChart series={hostSeriesFor(metric)} t0={t0} t1={t1} kind={metric.kind} />
                </div>
              ))}
            </div>

            <div className="section-title" style={{ margin: "18px 0 6px" }}>Current detail</div>
            <HostSnapshot hostId={selectedHost.host_id} />
          </>
        ) : (
          <>
            <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>
              {selectedEnv
                ? "Each line is a host in this environment. Click a host to see all its metrics."
                : "Each line is an environment (averaged across its hosts). Click an environment to drill into its hosts."}
            </div>
            <Legend items={selectedEnv ? hostLegend : envLegend}
                    onClick={selectedEnv ? (id) => setSelectedHostId(id) : (env) => setSelectedEnv(env)}
                    selectedKey={null} />

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(440px, 1fr))", gap: 16, marginTop: 8 }}>
              {METRICS.map((metric) => (
                <div key={metric.key}>
                  <div className="faint" style={{ fontSize: 12, marginBottom: 2 }}>{metric.label}</div>
                  <LineChart series={seriesFor(metric)} t0={t0} t1={t1} kind={metric.kind} />
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
