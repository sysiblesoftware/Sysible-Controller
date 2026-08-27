import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { hypervisorBadge } from "../hypervisor.js";
import { findSupp } from "../suppress.js";

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

// Recompute a host's health verdict the way the dashboard's per-host analysis
// does — from the raw fleet-health signals, honoring suppressions of its two
// suppressible health findings (disk_critical, failed_units) — so the map's node
// color matches the dashboard instead of the server's raw verdict (which ignores
// suppressions). `hh` is the fleet-health reading; isSupp(key)->bool. Returns
// OFFLINE / CRITICAL / WARNING / SUPPRESSED / OK, or null when status is unknown.
function healthVerdict(hh, isSupp) {
  if (hh.online === false) return "OFFLINE";
  if (hh.online == null && !hh.verdict) return null;   // unknown (e.g. SSH host, no probe)
  let sev = 9, active = 0, suppressed = 0;
  const add = (s, key) => {
    if (key && isSupp(key)) { suppressed++; return; }
    active++; sev = Math.min(sev, s);
  };
  if (hh.ok === false) add(2, null);
  if ((hh.disk ?? 0) >= 90) add(1, "disk_critical");
  else if ((hh.disk ?? 0) >= 80) add(2, null);
  if ((hh.mem ?? 0) >= 90) add(1, null);
  if ((hh.failed ?? 0) > 0) add(hh.failed >= 3 ? 1 : 2, "failed_units");
  if ((hh.oom ?? 0) > 0) add(1, null);
  if (active === 0) return suppressed ? "SUPPRESSED" : "OK";
  return sev === 1 ? "CRITICAL" : "WARNING";
}

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

// Last successful fetch, kept at module scope so leaving Topology and coming
// back paints the previous map INSTANTLY (then refreshes in the background)
// instead of flashing an empty canvas while every endpoint round-trips again.
const _snap = { hosts: [], health: [], agents: [], posture: [], supps: [] };

export default function Topology({ onOpen }) {
  const [hosts, setHosts] = useState(_snap.hosts);
  const [health, setHealth] = useState(_snap.health);
  const [agents, setAgents] = useState(_snap.agents);
  const [posture, setPosture] = useState(_snap.posture);
  const [supps, setSupps] = useState(_snap.supps);   // active finding suppressions
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [lens, setLens] = useState("env");          // "env" | "network"
  const [collapsed, setCollapsed] = useState({});   // group key -> true
  const [hover, setHover] = useState(null);
  const [view, setView] = useState({ s: 1, tx: 0, ty: 0 });
  const [positions, setPositions] = useState({});   // id / "__hub__"+key / "__ctrl__" -> {x,y} manual overrides
  const inFlight = useRef(false);
  const postureInFlight = useRef(false);   // guards the heavy posture overlay separately
  const drag = useRef(null);        // background pan
  const nodeDrag = useRef(null);    // dragging a single node / hub / controller
  const svgRef = useRef(null);
  const userAdjusted = useRef(false);   // once the user pans/zooms, stop auto-fitting
  const lastFitSig = useRef("");        // structural signature we last fit to

  const load = useCallback((force = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true); setErr("");
    // The map's structure and status colors come from hosts + health + agents,
    // which are cheap (health is heartbeat-served). Render as soon as those land
    // rather than waiting on the whole batch. Each applies independently.
    const fast = [
      api.hosts().then((v) => { _snap.hosts = v.hosts || []; setHosts(_snap.hosts); },
                       (e) => setErr(e?.message || "Couldn't load hosts")),
      api.fleetHealth(force).then((v) => { _snap.health = v.hosts || []; setHealth(_snap.health); }, () => {}),
      api.agents().then((v) => { _snap.agents = v.agents || []; setAgents(_snap.agents); }, () => {}),
      // Suppressions (cheap) so a node's critical ring clears when its finding is
      // suppressed — the map must match the dashboard, not re-flag silenced ones.
      api.suppressions().then((v) => { _snap.supps = v.suppressions || []; setSupps(_snap.supps); }, () => {}),
    ];
    // Posture is only an OVERLAY here — the critical red ring and the network
    // lens's gateway grouping. It's also the heavy call (a full fleet posture
    // sweep on a cold cache), so it must NOT gate the map: fire it separately and
    // let the rings/grouping fill in when it resolves. Without it the map still
    // renders fully, just with disk-only criticality and subnet-based grouping.
    // Its own in-flight guard stops the 10s poll from stacking overlapping
    // sweeps when a cold-cache sweep runs longer than the poll interval.
    if (!postureInFlight.current) {
      postureInFlight.current = true;
      api.fleetPosture(false)
        .then((v) => { _snap.posture = v.hosts || []; setPosture(_snap.posture); }, () => {})
        .finally(() => { postureInFlight.current = false; });
    }
    Promise.allSettled(fast).finally(() => { inFlight.current = false; setLoading(false); });
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
      // Honor suppressions the way the dashboard does, so the map agrees with it.
      // A finding counts only if firing AND not silenced by an active suppression
      // (host/env scope; boot_epoch drives the "until next reboot" check). The
      // node COLOR uses a suppression-aware health verdict (a suppressed disk /
      // failed-unit finding no longer keeps it amber); the red RING uses the
      // suppression-filtered sev-1 posture flags.
      const env = h.environment || "Unassigned";
      const boot = (pr.posture && pr.posture.os && pr.posture.os.boot_epoch) || null;
      const isSupp = (key) => !!findSupp(supps, { host: h.label, env, key, bootEpoch: boot });
      const verdict = healthVerdict(hh, isSupp);
      const hasCrit = (hh.disk >= 90 && !isSupp("disk_critical")) ||
        CRIT_FLAGS.some((k) => flags[k] === true && !isSupp(k));
      return {
        id: h.id, label: h.label, env,
        // The reliable is_controller flag is computed on the AGENT record (GET /agents,
        // _is_controller_host by name+IP); the merged-hosts source may not carry it, which
        // would draw the controller's own self-managed host as an ordinary host under an
        // environment (a duplicate "controller"). Trust either source.
        kind: h.type_text || "", address: h.address, isController: !!(ag.is_controller || h.is_controller),
        online: hh.online, verdict, disk: hh.disk, mem: hh.mem,
        agentVersion: ag.agent_version, ip, gateway: gw,
        subnet: subnetOf(ip), revoked: !!ag.revoked, quarantined: !!ag.integrity_quarantined,
        hasCrit, hypervisor: hh.hyp, vms: hh.vms, vmNames: hh.vm_names || [],
      };
    });
  }, [hosts, health, agents, posture, supps]);

  const center = useMemo(() => all.find((m) => m.isController) || null, [all]);

  // VM → hypervisor parentage, computed GLOBALLY (across every environment /
  // network group): a host whose label is in some hypervisor's reported vm_names
  // is a guest of that hypervisor — no matter what environment tag either carries.
  // This is what lets a VM hang off the machine it actually runs on even when the
  // hypervisor sits in a different group (e.g. hypervisor tagged "Sysible Labs"
  // but some of its guests tagged "Dev" — both still read as sourced from it).
  const parentOf = useMemo(() => {
    const byLabel = new Map(all.map((m) => [m.label, m]));
    const p = new Map();   // vm label -> hypervisor label
    for (const h of all) {
      if (h.hypervisor && Array.isArray(h.vmNames)) {
        for (const nm of h.vmNames) if (nm !== h.label && byLabel.has(nm)) p.set(nm, h.label);
      }
    }
    return p;
  }, [all]);

  // Group the (non-controller) hosts by the active lens. Guests are excluded here
  // — they don't form their own group entry; they hang under their hypervisor
  // (below), so an environment made up entirely of one hypervisor's VMs no longer
  // draws a separate hub whose members duplicate what's under the hypervisor.
  const groups = useMemo(() => {
    const others = all.filter((m) => !m.isController && !parentOf.has(m.label));
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
  }, [all, lens, parentOf]);

  // Base radial layout: group hubs around the controller + host grids outward
  // from each hub. Edges are rebuilt in `laid` from the FINAL positions so
  // manually-dragged nodes keep their connectors attached.
  const layout = useMemo(() => {
    const G = groups.length || 1;
    const Rhub = 200;
    const hubs = [], nodes = [];
    // Phase 1 — place each group's hub and its TOP hosts (hypervisors + non-VM
    // hosts; guests are excluded from groups). Record each top host's placement
    // and its group's radial basis so we can fan guests outward from it later,
    // even for guests that live in another group.
    const placedTop = new Map();   // label -> {rad, tan, dist, colOff, hubKey}
    groups.forEach((grp, i) => {
      const th = -Math.PI / 2 + (2 * Math.PI) * (i + 0.5) / G;
      const rad = { x: Math.cos(th), y: Math.sin(th) }, tan = { x: -Math.sin(th), y: Math.cos(th) };
      const hx = cx + Rhub * rad.x, hy = cy + Rhub * rad.y;
      const isCollapsed = !!collapsed[grp.key];
      hubs.push({ ...grp, x: hx, y: hy, th, collapsed: isCollapsed });
      if (isCollapsed) return;
      const topHosts = grp.hosts;   // guests already excluded from group formation
      const cols = Math.max(1, Math.min(6, Math.ceil(Math.sqrt(topHosts.length))));
      const sp = 42;
      topHosts.forEach((hostn, k) => {
        const r = Math.floor(k / cols), c = k % cols;
        const colOff = (c - (cols - 1) / 2) * sp;
        const dist = Rhub + 60 + r * sp;                       // extend outward, row by row
        nodes.push({ ...hostn, x: cx + rad.x * dist + tan.x * colOff, y: cy + rad.y * dist + tan.y * colOff, hub: grp.key });
        placedTop.set(hostn.label, { rad, tan, dist, colOff, hubKey: grp.key });
      });
    });
    // Phase 2 — hang every hypervisor's guests as a fan BEYOND it, using that
    // hypervisor's own placement/direction. Guests from ANY environment attach to
    // the machine they run on; a guest whose hypervisor wasn't placed (unknown, or
    // in a collapsed group) is hidden along with it.
    const childrenByHyp = new Map();       // hypervisor label -> [guest hosts]
    all.forEach((m) => {
      if (m.isController || !parentOf.has(m.label)) return;
      const hyp = parentOf.get(m.label);
      if (!childrenByHyp.has(hyp)) childrenByHyp.set(hyp, []);
      childrenByHyp.get(hyp).push(m);
    });
    childrenByHyp.forEach((vms, hypLabel) => {
      const p = placedTop.get(hypLabel);
      if (!p) return;
      vms.sort((a, b) => a.label.localeCompare(b.label));
      const vcols = Math.max(1, Math.min(5, Math.ceil(Math.sqrt(vms.length))));
      const vsp = 40;
      vms.forEach((vm, k) => {
        const r = Math.floor(k / vcols), c = k % vcols;
        const colOff = p.colOff + (c - (vcols - 1) / 2) * vsp;
        const dist = p.dist + 74 + r * vsp;
        nodes.push({ ...vm, x: cx + p.rad.x * dist + p.tan.x * colOff, y: cy + p.rad.y * dist + p.tan.y * colOff, hub: p.hubKey, vmParentLabel: hypLabel });
      });
    });
    return { hubs, nodes };
  }, [groups, collapsed, all, parentOf]);

  // Apply any manual position overrides, then rebuild edges from the final
  // positions so a dragged node's connectors follow it.
  const laid = useMemo(() => {
    const pos = positions;
    const ctrl = pos.__ctrl__ || { x: cx, y: cy };
    const hubs = layout.hubs.map((h) => {
      const o = pos["__hub__" + h.key];
      return o ? { ...h, x: o.x, y: o.y } : h;
    });
    const hubByKey = {}; hubs.forEach((h) => { hubByKey[h.key] = h; });
    const nodes = layout.nodes.map((n) => {
      const o = pos[n.id];
      return o ? { ...n, x: o.x, y: o.y } : n;
    });
    const edges = [];
    hubs.forEach((h) => edges.push({ x1: ctrl.x, y1: ctrl.y, x2: h.x, y2: h.y, kind: "hub", worst: h.worst }));
    // Each node connects to its PARENT: a nested VM to its hypervisor (so it
    // reads as a subtree hanging off the host), every other host to its group hub.
    const byLabel = {}; nodes.forEach((n) => { byLabel[n.label] = n; });
    nodes.forEach((n) => {
      const parent = n.vmParentLabel ? byLabel[n.vmParentLabel] : hubByKey[n.hub];
      if (parent) edges.push({ x1: parent.x, y1: parent.y, x2: n.x, y2: n.y, kind: "host", host: n });
    });
    return { hubs, nodes, edges, ctrl };
  }, [layout, positions]);

  const nodeById = useMemo(() => {
    const m = {}; for (const n of laid.nodes) m[n.id] = n;
    if (center) m.__ctrl__ = { ...center, x: laid.ctrl.x, y: laid.ctrl.y };
    for (const h of laid.hubs) m["__hub__" + h.key] = h;
    return m;
  }, [laid, center]);
  const hoverObj = hover ? nodeById[hover] : null;

  // Fit-to-view: center + scale the whole graph (controller + hubs + hosts) into
  // the viewport. The radial layout pins the controller at the viewBox centre, so
  // with few groups the content sits off to one side and hosts run past an edge —
  // this reframes it so everything is visible. Padding leaves room for the labels
  // that sit below/around each node.
  const fitView = useCallback(() => {
    const pts = [[laid.ctrl.x, laid.ctrl.y]];
    laid.hubs.forEach((h) => pts.push([h.x, h.y]));
    laid.nodes.forEach((n) => pts.push([n.x, n.y]));
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [x, y] of pts) {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    if (!Number.isFinite(minX)) return;
    const padX = 80, padTop = 46, padBottom = 66;   // labels hang below the nodes
    minX -= padX; maxX += padX; minY -= padTop; maxY += padBottom;
    const cw = Math.max(1, maxX - minX), ch = Math.max(1, maxY - minY);
    const s = Math.max(0.3, Math.min(3, Math.min(W / cw, H / ch)));
    const bcx = (minX + maxX) / 2, bcy = (minY + maxY) / 2;
    setView({ s, tx: W / 2 - s * bcx, ty: H / 2 - s * bcy });
  }, [laid]);

  // Re-fit only when the STRUCTURE changes (group set / lens / collapse / which
  // hosts exist) — not on every 10 s value refresh, and never after the user has
  // panned or zoomed themselves.
  const fitSig = useMemo(() =>
    lens + "|" + laid.hubs.map((h) => h.key + (h.collapsed ? "c" : "")).join(",")
         + "|" + laid.nodes.map((n) => n.id).join(","),
    [lens, laid]);
  useEffect(() => {
    if (laid.hubs.length === 0) return;           // nothing to frame yet
    if (userAdjusted.current) return;             // respect a manual pan/zoom
    if (fitSig === lastFitSig.current) return;    // structure unchanged
    lastFitSig.current = fitSig;
    fitView();
  }, [fitSig, fitView, laid.hubs.length]);

  const counts = useMemo(() => {
    let on = 0, off = 0, crit = 0;
    for (const n of all) { if (n.isController) continue; if (n.online === false) off++; else if (n.online) on++; if (n.hasCrit && n.online !== false) crit++; }
    return { on, off, crit };
  }, [all]);

  const toggleGroup = (key) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));
  const collapseAll = () => setCollapsed(Object.fromEntries(groups.map((g) => [g.key, true])));
  const expandAll = () => setCollapsed({});
  const zoom = (f) => {
    userAdjusted.current = true;
    setView((v) => {
      const s = Math.max(0.4, Math.min(3, v.s * f));
      return { s, tx: v.tx + (v.s - s) * cx, ty: v.ty + (v.s - s) * cy };
    });
  };
  // Reset any manual node positions and re-fit the whole graph to the viewport
  // (clearing the "user adjusted" flag so the fit effect reframes it).
  const resetView = () => { userAdjusted.current = false; lastFitSig.current = ""; setPositions({}); };
  const onWheel = (e) => { zoom(e.deltaY < 0 ? 1.12 : 0.89); };
  const onDown = (e) => { drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty }; };

  const svgUnitsPerPx = () => (svgRef.current ? W / svgRef.current.getBoundingClientRect().width : 1);

  // Start dragging a single node/hub/controller. `action` is what a click (a
  // press that doesn't move) should do — open the host, or toggle the cluster.
  const startNodeDrag = (e, key, origX, origY, action) => {
    e.stopPropagation();   // don't let the background pan kick in
    nodeDrag.current = { key, cx0: e.clientX, cy0: e.clientY, origX, origY, action, moved: false };
  };

  const onMove = (e) => {
    if (nodeDrag.current) {
      const nd = nodeDrag.current;
      const k = svgUnitsPerPx() / view.s;   // px delta -> inner-group (data) units
      if (!nd.moved && Math.hypot(e.clientX - nd.cx0, e.clientY - nd.cy0) > 3) nd.moved = true;
      if (nd.moved) {
        const nx = nd.origX + (e.clientX - nd.cx0) * k, ny = nd.origY + (e.clientY - nd.cy0) * k;
        setPositions((p) => ({ ...p, [nd.key]: { x: nx, y: ny } }));
      }
      return;
    }
    if (!drag.current || !svgRef.current) return;
    userAdjusted.current = true;   // a manual pan; stop auto-fitting
    const k = svgUnitsPerPx();
    setView((v) => ({ ...v, tx: drag.current.tx + (e.clientX - drag.current.x) * k, ty: drag.current.ty + (e.clientY - drag.current.y) * k }));
  };

  const onUp = () => {
    if (nodeDrag.current) {
      const nd = nodeDrag.current; nodeDrag.current = null;
      if (!nd.moved && nd.action) nd.action();   // a press that didn't move is a click
      return;
    }
    drag.current = null;
  };
  const onLeave = () => { nodeDrag.current = null; drag.current = null; };
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
            <button className="btn ghost sm" onClick={resetView} title="Fit everything to view & reset node positions">⤢</button>
          </div>
          <label className="checkrow" style={{ margin: 0 }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            <span className="faint">Auto</span>
          </label>
          <button className="btn ghost sm" onClick={() => load(true)} disabled={loading}>{loading ? <span className="spin" /> : "Refresh"}</button>
        </div>
      </div>

      {all.length === 0 ? (
        <div className="empty" style={{ padding: 40 }}>{loading ? "Loading the fleet…" : "No hosts enrolled yet."}</div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", maxHeight: "74vh", cursor: drag.current ? "grabbing" : "grab", touchAction: "none" }}
               onWheel={onWheel} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onLeave}>
            <g transform={`translate(${view.tx} ${view.ty}) scale(${view.s})`}>
              {/* Edges. */}
              {laid.edges.map((e, i) => {
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
              {laid.hubs.map((h) => (
                <g key={"hub" + h.key} transform={`translate(${h.x} ${h.y})`} style={{ cursor: "grab" }}
                   onMouseDown={(e) => startNodeDrag(e, "__hub__" + h.key, h.x, h.y, () => toggleGroup(h.key))}
                   onMouseEnter={() => setHover("__hub__" + h.key)} onMouseLeave={() => setHover((x) => (x === "__hub__" + h.key ? null : x))}>
                  <circle r={h.collapsed ? 18 : 8} fill={h.collapsed ? (COLOR[h.worst] || COLOR.UNKNOWN) : "var(--panel-2, #1a2130)"}
                          stroke={COLOR[h.worst] || COLOR.UNKNOWN} strokeWidth={2} />
                  {h.collapsed && <text textAnchor="middle" dominantBaseline="central" style={{ fontSize: 12, fontWeight: 700, fill: "#0d1117" }}>{h.hosts.length}</text>}
                  <text y={h.collapsed ? 32 : 22} textAnchor="middle" style={{ fontSize: 12.5, fontWeight: 600, fill: "var(--text-faint,#8b93a7)" }}>
                    {trunc(h.label, 22)} <tspan style={{ fontWeight: 400 }}>({h.hosts.length}{h.collapsed ? " ▸" : " ▾"})</tspan>
                  </text>
                </g>
              ))}

              {/* Host nodes. */}
              {laid.nodes.map((n) => {
                const hovered = hover === n.id;
                const ring = n.revoked ? COLOR.CRITICAL : n.quarantined ? COLOR.WARNING : (n.hasCrit && n.online !== false) ? COLOR.CRITICAL : null;
                return (
                  <g key={"n" + n.id} transform={`translate(${n.x} ${n.y})`} style={{ cursor: "grab" }}
                     onMouseDown={(e) => startNodeDrag(e, n.id, n.x, n.y, () => n.id && onOpen && onOpen("host", { id: n.id, label: n.label }))}
                     onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover((h) => (h === n.id ? null : h))}>
                    {ring && <circle r={hovered ? 15 : 13.5} fill="none" stroke={ring} strokeWidth={2}
                                     strokeDasharray={n.revoked || n.quarantined ? "3 3" : undefined} />}
                    <circle r={hovered ? 11 : 9} fill={nodeColor(n)} stroke="var(--bg,#0d1117)" strokeWidth={2} />
                    {n.kind === "Agent + SSH" && <circle r={hovered ? 4.5 : 3.5} fill="var(--bg,#0d1117)" />}
                    {n.hypervisor && (
                      // Yellow server glyph marking a VM host (hypervisor). Drawn as
                      // SVG (not the 🖥 emoji, which can't be recoloured) so it's a
                      // clear, consistent yellow above the node.
                      <g transform="translate(0 -16)" stroke="#eab308" fill="none" strokeWidth={1.5} strokeLinecap="round">
                        <rect x={-6} y={-5} width={12} height={4.5} rx={1} />
                        <rect x={-6} y={0.5} width={12} height={4.5} rx={1} />
                        <line x1={-3.5} y1={-2.75} x2={-3.4} y2={-2.75} />
                        <line x1={-3.5} y1={2.75} x2={-3.4} y2={2.75} />
                      </g>
                    )}
                    <text y={24} textAnchor="middle" style={{ fontSize: 11.5, fill: "var(--text,#e6e6e6)", fontWeight: hovered ? 700 : 400 }}>{trunc(n.label)}</text>
                  </g>
                );
              })}

              {/* Controller hub. */}
              <g transform={`translate(${laid.ctrl.x} ${laid.ctrl.y})`} style={{ cursor: "grab" }}
                 onMouseDown={(e) => startNodeDrag(e, "__ctrl__", laid.ctrl.x, laid.ctrl.y,
                   () => center && center.id && onOpen && onOpen("host", { id: center.id, label: center.label }))}
                 onMouseEnter={() => center && setHover("__ctrl__")} onMouseLeave={() => setHover((h) => (h === "__ctrl__" ? null : h))}>
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
            <span className="faint" style={{ marginLeft: "auto" }}>solid = agent · dashed = SSH · drag a node to move it · drag the background to pan · scroll to zoom · click a cluster to collapse · ⤢ fits everything to view</span>
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
    n.hypervisor ? `🖥 ${hypervisorBadge(n)}` : "",
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
