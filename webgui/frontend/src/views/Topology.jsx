import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";

// Fleet topology — a controller-centric hub-and-spoke map. The controller sits
// at the middle; every managed host radiates out, grouped into angular sectors
// by environment. Nodes are coloured by live health (online/offline + verdict),
// hover shows details, click opens the host's drill-down. Pure inline SVG (no
// chart lib), auto-refreshed with the same 10s cadence as the dashboard.

const COLOR = { OK: "#4ec07a", WARNING: "#e0a83a", CRITICAL: "#e06c6c", OFFLINE: "#7a7a7a", SUPPRESSED: "#6c7fa8", UNKNOWN: "#5a6270" };
const ACCENT = "#3d7dd8";

function nodeColor(n) {
  if (n.online === false) return COLOR.OFFLINE;
  if (n.online == null && !n.verdict) return COLOR.UNKNOWN;
  return COLOR[(n.verdict || "OK").toUpperCase()] || COLOR.OK;
}
function statusText(n) {
  if (n.online === false) return "offline";
  if (n.online == null && !n.verdict) return "unknown";
  return (n.verdict || "OK").toLowerCase();
}

export default function Topology({ onOpen }) {
  const [hosts, setHosts] = useState([]);
  const [health, setHealth] = useState([]);
  const [agents, setAgents] = useState([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [hover, setHover] = useState(null);   // hovered node id
  const inFlight = useRef(false);

  const load = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true); setErr("");
    Promise.allSettled([api.hosts(), api.fleetHealth(), api.agents()])
      .then(([h, fh, ag]) => {
        if (h.status === "fulfilled") setHosts(h.value.hosts || []);
        else setErr(h.reason?.message || "Couldn't load hosts");
        if (fh.status === "fulfilled") setHealth(fh.value.hosts || []);
        if (ag.status === "fulfilled") setAgents(ag.value.agents || []);
      })
      .finally(() => { inFlight.current = false; setLoading(false); });
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto) return undefined;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [auto, load]);

  const W = 960, H = 620, cx = W / 2, cy = H / 2;

  const { center, nodes, envLabels } = useMemo(() => {
    const healthById = {};
    for (const h of health) healthById[h.id] = h;
    const agByName = {};
    for (const a of agents) agByName[a.hostname || a.host_id] = a;
    const merged = hosts.map((h) => {
      const hh = healthById[h.id] || {};
      const ag = agByName[h.label] || {};
      return {
        id: h.id, label: h.label, env: h.environment || "Unassigned",
        kind: h.type_text || "", address: h.address, isController: !!h.is_controller,
        online: hh.online, verdict: hh.verdict, disk: hh.disk, mem: hh.mem,
        agentVersion: ag.agent_version, ip: ag.ip,
      };
    });
    const ctrl = merged.find((m) => m.isController) || null;
    const others = merged.filter((m) => !m.isController);
    others.sort((a, b) => a.env.localeCompare(b.env) || a.label.localeCompare(b.label));
    const T = others.length;
    const R = Math.min(W, H) / 2 - 118;
    const placed = others.map((m, k) => {
      const ang = -Math.PI / 2 + (2 * Math.PI) * (k + 0.5) / Math.max(1, T);
      return { ...m, x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), ang };
    });
    const byEnv = {};
    for (const p of placed) (byEnv[p.env] || (byEnv[p.env] = [])).push(p);
    const envLabels = Object.entries(byEnv).map(([env, list]) => {
      const mid = list.reduce((s, p) => s + p.ang, 0) / list.length;
      const lr = R + 70;
      return { env, count: list.length, x: cx + lr * Math.cos(mid), y: cy + lr * Math.sin(mid),
               anchor: Math.cos(mid) > 0.25 ? "start" : Math.cos(mid) < -0.25 ? "end" : "middle" };
    });
    return { center: ctrl, nodes: placed, envLabels };
  }, [hosts, health, agents]);

  const hoverNode = hover === "__ctrl__" ? center : nodes.find((n) => n.id === hover);
  const counts = useMemo(() => {
    const c = { online: 0, offline: 0 };
    for (const n of nodes) (n.online === false ? c.offline++ : n.online ? c.online++ : 0);
    return c;
  }, [nodes]);

  const trunc = (s, n = 16) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");

  return (
    <div>
      {err && <div className="error-box">{err}</div>}
      <div className="spread" style={{ marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
        <span className="faint" style={{ fontSize: 13 }}>
          {hosts.length} host{hosts.length === 1 ? "" : "s"}
          {nodes.length > 0 && <> · <span style={{ color: COLOR.OK }}>{counts.online} online</span>
            {counts.offline > 0 && <> · <span style={{ color: COLOR.CRITICAL }}>{counts.offline} offline</span></>}</>}
        </span>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <label className="checkrow" style={{ margin: 0 }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            <span className="faint">Auto (10s)</span>
          </label>
          <button className="btn ghost sm" onClick={load} disabled={loading}>
            {loading ? <span className="spin" /> : "Refresh"}
          </button>
        </div>
      </div>

      {hosts.length === 0 ? (
        <div className="empty" style={{ padding: 40 }}>
          {loading ? "Loading the fleet…" : "No hosts enrolled yet — enroll a host to see the topology."}
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", maxHeight: "72vh" }}>
            {/* Spokes: controller -> host. Dashed for SSH-only, faded when offline. */}
            {nodes.map((n) => (
              <line key={"e" + n.id} x1={cx} y1={cy} x2={n.x} y2={n.y}
                    stroke={nodeColor(n)} strokeOpacity={n.online === false ? 0.25 : 0.45}
                    strokeWidth={1.4} strokeDasharray={n.kind === "SSH" ? "4 4" : undefined} />
            ))}

            {/* Environment labels at the outer edge of each sector. */}
            {envLabels.map((e) => (
              <text key={"env" + e.env} x={e.x} y={e.y} textAnchor={e.anchor} dominantBaseline="middle"
                    style={{ fontSize: 13, fontWeight: 600, fill: "var(--text-faint, #8b93a7)" }}>
                {e.env} <tspan style={{ fontWeight: 400 }}>({e.count})</tspan>
              </text>
            ))}

            {/* Host nodes. */}
            {nodes.map((n) => {
              const c = nodeColor(n);
              const hovered = hover === n.id;
              return (
                <g key={"n" + n.id} transform={`translate(${n.x} ${n.y})`} style={{ cursor: "pointer" }}
                   onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover((h) => (h === n.id ? null : h))}
                   onClick={() => n.id && onOpen && onOpen("host", { id: n.id, label: n.label })}>
                  <circle r={hovered ? 12 : 10} fill={c} stroke="var(--bg, #0d1117)" strokeWidth={2} />
                  {n.kind === "Agent + SSH" && <circle r={hovered ? 5 : 4} fill="var(--bg, #0d1117)" />}
                  <text y={26} textAnchor="middle" style={{ fontSize: 12, fill: "var(--text, #e6e6e6)",
                        fontWeight: hovered ? 700 : 400 }}>{trunc(n.label)}</text>
                </g>
              );
            })}

            {/* Controller hub. */}
            <g transform={`translate(${cx} ${cy})`} style={{ cursor: center && center.id ? "pointer" : "default" }}
               onMouseEnter={() => center && setHover("__ctrl__")} onMouseLeave={() => setHover((h) => (h === "__ctrl__" ? null : h))}
               onClick={() => center && center.id && onOpen && onOpen("host", { id: center.id, label: center.label })}>
              <circle r={28} fill={ACCENT} stroke="var(--bg, #0d1117)" strokeWidth={3} />
              <path d="M-9 -3 h18 M-9 3 h18 M-6 -6 v12 M6 -6 v12" stroke="#fff" strokeWidth={1.6} fill="none" opacity={0.9} />
              <text y={46} textAnchor="middle" style={{ fontSize: 13, fontWeight: 700, fill: "var(--text, #e6e6e6)" }}>
                {center ? trunc(center.label, 20) : "Sysible Controller"}
              </text>
              <text y={62} textAnchor="middle" style={{ fontSize: 11, fill: "var(--text-faint, #8b93a7)" }}>controller</text>
            </g>

            {/* Hover tooltip (drawn last so it's on top). */}
            {hoverNode && <NodeTooltip n={hoverNode} W={W} H={H} />}
          </svg>

          {/* Legend. */}
          <div className="row" style={{ gap: 16, flexWrap: "wrap", padding: "8px 12px", borderTop: "1px solid var(--border)", fontSize: 12 }}>
            {[["OK", "healthy"], ["WARNING", "warning"], ["CRITICAL", "critical"], ["SUPPRESSED", "suppressed"], ["OFFLINE", "offline"]].map(([k, l]) => (
              <span key={k} className="faint"><span className="dot" style={{ background: COLOR[k] }} /> {l}</span>
            ))}
            <span className="faint" style={{ marginLeft: "auto" }}>solid = agent · dashed = SSH-only</span>
          </div>
        </div>
      )}
    </div>
  );
}

// SVG tooltip near the hovered node, flipped to stay inside the canvas.
function NodeTooltip({ n, W, H }) {
  const lines = [
    n.env,
    n.isController ? "This host is the controller" : (n.kind || "host"),
    n.ip || n.address || "",
    `status: ${statusText(n)}`,
    (n.disk != null || n.mem != null) ? `disk ${n.disk ?? "—"}%  ·  mem ${n.mem ?? "—"}%` : "",
    n.agentVersion ? `agent ${n.agentVersion}` : "",
  ].filter(Boolean);
  const w = 210, h = 22 + lines.length * 16;
  const px = n.isController ? W / 2 : n.x;
  const py = n.isController ? H / 2 : n.y;
  const left = px > W / 2;
  const x = left ? px - w - 18 : px + 18;
  const y = Math.max(6, Math.min(H - h - 6, py - h / 2));
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none">
      <rect width={w} height={h} rx={8} fill="#141a24" stroke="var(--border, #2a3242)" opacity={0.98} />
      <text x={12} y={19} style={{ fontSize: 13, fontWeight: 700, fill: "#e6e6e6" }}>{n.label}</text>
      {lines.map((l, i) => (
        <text key={i} x={12} y={38 + i * 16} style={{ fontSize: 11.5, fill: "#aeb6c6" }}>{l}</text>
      ))}
    </g>
  );
}
