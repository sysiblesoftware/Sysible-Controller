import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";

// Fleet topology — a controller-centric map. The controller is the hub; managed
// hosts cluster around it, grouped either by ENVIRONMENT or by NETWORK segment
// (subnet / gateway). Two-level layout: controller → per-group hub → hosts, so
// it scales to a large fleet. Clusters collapse to a summary node, the canvas
// pans/zooms, edges show the connection type, and a host with an active critical
// finding gets a red ring. Pure inline SVG, read-only endpoints, 10s refresh.

const COLOR = { OK: "#4ec07a", WARNING: "#e0a83a", CRITICAL: "#e06c6c", OFFLINE: "#7a7a7a", SUPPRESSED: "#6c7fa8", UNKNOWN: "#5a6270" };
const ACCENT = "#3d7dd8";
const RANK = { CRITICAL: 0, OFFLINE: 1, WARNING: 2, SUPPRESSED: 3, OK: 4, UNKNOWN: 5 };
// Posture flags that count as CRITICAL (mirrors the dashboard's critical set).
const CRIT_FLAGS = ["ssh_root_login", "firewall_disabled", "eol_os", "risky_accounts"];

function statusOf(n) {
  if (n.online === false) return "OFFLINE";
  if (n.online == null && !n.verdict) return "UNKNOWN";
  return (n.verdict || "OK").toUpperCase();
}
const nodeColor = (n) => COLOR[statusOf(n)] || COLOR.OK;
const extractIp = (s) => { const m = /\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b/.exec(s || ""); return m ? m[1] : null; };
const subnetOf = (ip) => (ip ? ip.split(".").slice(0, 3).join(".") + ".0/24" : null);
function worst(hosts) {
  let r = 5;
  for (const h of hosts) r = Math.min(r, RANK[statusOf(h)] ?? 5);
  return Object.keys(RANK).find((k) => RANK[k] === r) || "OK";
}

export default function Topology({ onOpen }) {
  const [hosts, setHosts] = useState([]);
  const [health, setHealth] = useState([]);
  const [agents, setAgents] = useState([]);
  const [posture, setPosture] = useState([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [lens, setLens] = useState("env");          // "env" | "network"
  const [collapsed, setCollapsed] = useState({});   // group key -> true
  const [hover, setHover] = useState(null);
  const [view, setView] = useState({ s: 1, tx: 0, ty: 0 });
  const inFlight = useRef(false);
  const drag = useRef(null);
  const svgRef = useRef(null);

  const load = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true); setErr("");
    Promise.allSettled([api.hosts(), api.fleetHealth(), api.agents(), api.fleetPosture(false)])
      .then(([h, fh, ag, po]) => {
        if (h.status === "fulfilled") setHosts(h.value.hosts || []); else setErr(h.reason?.message || "Couldn't load hosts");
        if (fh.status === "fulfilled") setHealth(fh.value.hosts || []);
        if (ag.status === "fulfilled") setAgents(ag.value.agents || []);
        if (po.status === "fulfilled") setPosture(po.value.hosts || []);
      })
      .finally(() => { inFlight.current = false; setLoading(false); });
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto) return undefined;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [auto, load]);

  const W = 1000, H = 660, cx = W / 2, cy = H / 2;

  // Merge every source into one rich per-host record.
  const all = useMemo(() => {
    const byId = {};
    for (const h of health) byId[h.id] = h;
    const agByName = {}, agById = {};
    for (const a of agents) { if (a.hostname) agByName[a.hostname] = a; if (a.host_id) agById[a.host_id] = a; }
    const postById = {};
    for (const p of posture) postById[p.id] = p;
    return hosts.map((h) => {
      const hh = byId[h.id] || {};
      const ag = agByName[h.label] || agById[h.id] || {};
      const pr = postById[h.id] || {};
      const flags = pr.flags || {};
      const ip = ag.ip || extractIp(h.address);
      const gw = (pr.posture && pr.posture.net && pr.posture.net.gateway) || null;
      const hasCrit = (hh.disk >= 90) || CRIT_FLAGS.some((k) => flags[k] === true);
      return {
        id: h.id, label: h.label, env: h.environment || "Unassigned",
        kind: h.type_text || "", address: h.address, isController: !!h.is_controller,
        online: hh.online, verdict: hh.verdict, disk: hh.disk, mem: hh.mem,
        agentVersion: ag.agent_version, ip, gateway: gw,
        subnet: subnetOf(ip), revoked: !!ag.revoked, quarantined: !!ag.integrity_quarantined,
        hasCrit,
      };
    });
  }, [hosts, health, agents, posture]);

  const center = useMemo(() => all.find((m) => m.isController) || null, [all]);

  // Group the (non-controller) hosts by the active lens.
  const groups = useMemo(() => {
    const others = all.filter((m) => !m.isController);
    const g = {};
    for (const m of others) {
      let key, label;
      if (lens === "network") {
        key = m.subnet || "unknown";
        label = m.subnet ? m.subnet + (m.gateway ? ` · gw ${m.gateway}` : "") : "no IP / unknown";
      } else {
        key = m.env; label = m.env;
      }
      (g[key] || (g[key] = { key, label, hosts: [] })).hosts.push(m);
    }
    const list = Object.values(g);
    list.forEach((grp) => { grp.hosts.sort((a, b) => a.label.localeCompare(b.label)); grp.worst = worst(grp.hosts); });
    list.sort((a, b) => b.hosts.length - a.hosts.length || a.label.localeCompare(b.label));
    return list;
  }, [all, lens]);

  // Lay out group hubs (radial) + host grids (outward from each hub).
  const layout = useMemo(() => {
    const G = groups.length || 1;
    const Rhub = 200;
    const hubs = [], nodes = [], edges = [];
    groups.forEach((grp, i) => {
      const th = -Math.PI / 2 + (2 * Math.PI) * (i + 0.5) / G;
      const rad = { x: Math.cos(th), y: Math.sin(th) }, tan = { x: -Math.sin(th), y: Math.cos(th) };
      const hx = cx + Rhub * rad.x, hy = cy + Rhub * rad.y;
      const isCollapsed = !!collapsed[grp.key];
      hubs.push({ ...grp, x: hx, y: hy, th, collapsed: isCollapsed });
      edges.push({ x1: cx, y1: cy, x2: hx, y2: hy, kind: "hub", worst: grp.worst });
      if (isCollapsed) return;
      const n = grp.hosts.length;
      const cols = Math.max(1, Math.min(6, Math.ceil(Math.sqrt(n))));
      const sp = 42;
      grp.hosts.forEach((hostn, k) => {
        const r = Math.floor(k / cols), c = k % cols;
        const colOff = (c - (cols - 1) / 2) * sp;
        const dist = Rhub + 60 + r * sp;                       // extend outward, row by row
        const x = cx + rad.x * dist + tan.x * colOff;
        const y = cy + rad.y * dist + tan.y * colOff;
        nodes.push({ ...hostn, x, y });
        edges.push({ x1: hx, y1: hy, x2: x, y2: y, kind: "host", host: hostn });
      });
    });
    return { hubs, nodes, edges };
  }, [groups, collapsed]);

  const nodeById = useMemo(() => {
    const m = {}; for (const n of layout.nodes) m[n.id] = n;
    if (center) m.__ctrl__ = { ...center, x: cx, y: cy };
    for (const h of layout.hubs) m["__hub__" + h.key] = h;
    return m;
  }, [layout, center]);
  const hoverObj = hover ? nodeById[hover] : null;

  const counts = useMemo(() => {
    let on = 0, off = 0, crit = 0;
    for (const n of all) { if (n.isController) continue; if (n.online === false) off++; else if (n.online) on++; if (n.hasCrit && n.online !== false) crit++; }
    return { on, off, crit };
  }, [all]);

  const toggleGroup = (key) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));
  const collapseAll = () => setCollapsed(Object.fromEntries(groups.map((g) => [g.key, true])));
  const expandAll = () => setCollapsed({});
  const zoom = (f) => setView((v) => {
    const s = Math.max(0.4, Math.min(3, v.s * f));
    return { s, tx: v.tx + (v.s - s) * cx, ty: v.ty + (v.s - s) * cy };
  });
  const resetView = () => setView({ s: 1, tx: 0, ty: 0 });
  const onWheel = (e) => { zoom(e.deltaY < 0 ? 1.12 : 0.89); };
  const onDown = (e) => { drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty }; };
  const onMove = (e) => {
    if (!drag.current || !svgRef.current) return;
    const k = W / svgRef.current.getBoundingClientRect().width;
    setView((v) => ({ ...v, tx: drag.current.tx + (e.clientX - drag.current.x) * k, ty: drag.current.ty + (e.clientY - drag.current.y) * k }));
  };
  const endDrag = () => { drag.current = null; };
  const trunc = (s, n = 15) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");

  const Btn = ({ active, ...p }) => <button className={"btn sm" + (active ? "" : " ghost")} {...p} />;

  return (
    <div>
      {err && <div className="error-box">{err}</div>}
      <div className="spread" style={{ marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          <span className="faint" style={{ fontSize: 12 }}>Group by:</span>
          <Btn active={lens === "env"} onClick={() => setLens("env")}>Environment</Btn>
          <Btn active={lens === "network"} onClick={() => setLens("network")}>Network</Btn>
          <span className="faint" style={{ fontSize: 12, marginLeft: 10 }}>
            {counts.on} online{counts.off > 0 && <> · <span style={{ color: COLOR.CRITICAL }}>{counts.off} offline</span></>}
            {counts.crit > 0 && <> · <span style={{ color: COLOR.CRITICAL }}>{counts.crit} critical</span></>}
          </span>
        </div>
        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn ghost sm" onClick={collapseAll}>Collapse all</button>
          <button className="btn ghost sm" onClick={expandAll}>Expand all</button>
          <div className="row" style={{ gap: 2 }}>
            <button className="btn ghost sm" onClick={() => zoom(1.2)} title="Zoom in">＋</button>
            <button className="btn ghost sm" onClick={() => zoom(0.83)} title="Zoom out">－</button>
            <button className="btn ghost sm" onClick={resetView} title="Reset view">⤢</button>
          </div>
          <label className="checkrow" style={{ margin: 0 }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            <span className="faint">Auto</span>
          </label>
          <button className="btn ghost sm" onClick={load} disabled={loading}>{loading ? <span className="spin" /> : "Refresh"}</button>
        </div>
      </div>

      {all.length === 0 ? (
        <div className="empty" style={{ padding: 40 }}>{loading ? "Loading the fleet…" : "No hosts enrolled yet."}</div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", maxHeight: "74vh", cursor: drag.current ? "grabbing" : "grab", touchAction: "none" }}
               onWheel={onWheel} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={endDrag} onMouseLeave={endDrag}>
            <g transform={`translate(${view.tx} ${view.ty}) scale(${view.s})`}>
              {/* Edges. */}
              {layout.edges.map((e, i) => {
                const isHub = e.kind === "hub";
                const h = e.host;
                const col = isHub ? (COLOR[e.worst] || COLOR.UNKNOWN)
                  : h.revoked ? COLOR.CRITICAL : h.quarantined ? COLOR.WARNING : nodeColor(h);
                const dash = isHub ? undefined : (h.revoked || h.quarantined) ? "3 3" : h.kind === "SSH" ? "5 4" : undefined;
                const op = isHub ? 0.5 : (h.online === false ? 0.22 : 0.5);
                return <line key={i} x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} stroke={col} strokeOpacity={op}
                             strokeWidth={isHub ? 2 : 1.4} strokeDasharray={dash} />;
              })}

              {/* Group hubs. */}
              {layout.hubs.map((h) => (
                <g key={"hub" + h.key} transform={`translate(${h.x} ${h.y})`} style={{ cursor: "pointer" }}
                   onMouseDown={(e) => e.stopPropagation()}
                   onMouseEnter={() => setHover("__hub__" + h.key)} onMouseLeave={() => setHover((x) => (x === "__hub__" + h.key ? null : x))}
                   onClick={() => toggleGroup(h.key)}>
                  <circle r={h.collapsed ? 18 : 8} fill={h.collapsed ? (COLOR[h.worst] || COLOR.UNKNOWN) : "var(--panel-2, #1a2130)"}
                          stroke={COLOR[h.worst] || COLOR.UNKNOWN} strokeWidth={2} />
                  {h.collapsed && <text textAnchor="middle" dominantBaseline="central" style={{ fontSize: 12, fontWeight: 700, fill: "#0d1117" }}>{h.hosts.length}</text>}
                  <text y={h.collapsed ? 32 : 22} textAnchor="middle" style={{ fontSize: 12.5, fontWeight: 600, fill: "var(--text-faint,#8b93a7)" }}>
                    {trunc(h.label, 22)} <tspan style={{ fontWeight: 400 }}>({h.hosts.length}{h.collapsed ? " ▸" : " ▾"})</tspan>
                  </text>
                </g>
              ))}

              {/* Host nodes. */}
              {layout.nodes.map((n) => {
                const hovered = hover === n.id;
                const ring = n.revoked ? COLOR.CRITICAL : n.quarantined ? COLOR.WARNING : (n.hasCrit && n.online !== false) ? COLOR.CRITICAL : null;
                return (
                  <g key={"n" + n.id} transform={`translate(${n.x} ${n.y})`} style={{ cursor: "pointer" }}
                     onMouseDown={(e) => e.stopPropagation()}
                     onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover((h) => (h === n.id ? null : h))}
                     onClick={() => n.id && onOpen && onOpen("host", { id: n.id, label: n.label })}>
                    {ring && <circle r={hovered ? 15 : 13.5} fill="none" stroke={ring} strokeWidth={2}
                                     strokeDasharray={n.revoked || n.quarantined ? "3 3" : undefined} />}
                    <circle r={hovered ? 11 : 9} fill={nodeColor(n)} stroke="var(--bg,#0d1117)" strokeWidth={2} />
                    {n.kind === "Agent + SSH" && <circle r={hovered ? 4.5 : 3.5} fill="var(--bg,#0d1117)" />}
                    <text y={24} textAnchor="middle" style={{ fontSize: 11.5, fill: "var(--text,#e6e6e6)", fontWeight: hovered ? 700 : 400 }}>{trunc(n.label)}</text>
                  </g>
                );
              })}

              {/* Controller hub. */}
              <g transform={`translate(${cx} ${cy})`} style={{ cursor: center && center.id ? "pointer" : "default" }}
                 onMouseDown={(e) => e.stopPropagation()}
                 onMouseEnter={() => center && setHover("__ctrl__")} onMouseLeave={() => setHover((h) => (h === "__ctrl__" ? null : h))}
                 onClick={() => center && center.id && onOpen && onOpen("host", { id: center.id, label: center.label })}>
                <circle r={26} fill={ACCENT} stroke="var(--bg,#0d1117)" strokeWidth={3} />
                <path d="M-8 -3 h16 M-8 3 h16 M-5 -6 v12 M5 -6 v12" stroke="#fff" strokeWidth={1.5} fill="none" opacity={0.9} />
                <text y={43} textAnchor="middle" style={{ fontSize: 13, fontWeight: 700, fill: "var(--text,#e6e6e6)" }}>{center ? trunc(center.label, 22) : "Sysible Controller"}</text>
                <text y={58} textAnchor="middle" style={{ fontSize: 11, fill: "var(--text-faint,#8b93a7)" }}>controller</text>
              </g>

              {hoverObj && hoverObj.id && !hoverObj.hosts && <NodeTooltip n={hoverObj} W={W} H={H} />}
            </g>
          </svg>

          <div className="row" style={{ gap: 14, flexWrap: "wrap", padding: "8px 12px", borderTop: "1px solid var(--border)", fontSize: 12 }}>
            {[["OK", "healthy"], ["WARNING", "warning"], ["CRITICAL", "critical"], ["OFFLINE", "offline"]].map(([k, l]) => (
              <span key={k} className="faint"><span className="dot" style={{ background: COLOR[k] }} /> {l}</span>
            ))}
            <span className="faint"><span style={{ display: "inline-block", width: 10, height: 10, borderRadius: "50%", border: `2px solid ${COLOR.CRITICAL}`, verticalAlign: "middle", marginRight: 4 }} /> critical finding / revoked</span>
            <span className="faint" style={{ marginLeft: "auto" }}>solid = agent · dashed = SSH · drag to pan · scroll to zoom · click a cluster to collapse</span>
          </div>
        </div>
      )}
    </div>
  );
}

function NodeTooltip({ n, W, H }) {
  const lines = [
    n.isController ? "This host is the controller" : (n.kind || "host"),
    n.env,
    n.ip ? `${n.ip}${n.gateway ? `  · gw ${n.gateway}` : ""}` : (n.address || ""),
    `status: ${statusOf(n).toLowerCase()}`,
    (n.disk != null || n.mem != null) ? `disk ${n.disk ?? "—"}%  ·  mem ${n.mem ?? "—"}%` : "",
    n.hasCrit ? "⚠ active critical finding" : "",
    n.revoked ? "⦸ agent revoked" : n.quarantined ? "⚠ integrity quarantined" : "",
    n.agentVersion ? `agent ${n.agentVersion}` : "",
  ].filter(Boolean);
  const w = 220, h = 22 + lines.length * 16;
  const left = n.x > W / 2;
  const x = Math.max(4, Math.min(W - w - 4, left ? n.x - w - 18 : n.x + 18));
  const y = Math.max(4, Math.min(H - h - 4, n.y - h / 2));
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none">
      <rect width={w} height={h} rx={8} fill="#141a24" stroke="var(--border,#2a3242)" opacity={0.98} />
      <text x={12} y={19} style={{ fontSize: 13, fontWeight: 700, fill: "#e6e6e6" }}>{n.label}</text>
      {lines.map((l, i) => <text key={i} x={12} y={38 + i * 16} style={{ fontSize: 11.5, fill: "#aeb6c6" }}>{l}</text>)}
    </g>
  );
}
