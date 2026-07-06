import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { searchTasks } from "../featureSearch.js";
import SuppressMenu from "../components/SuppressMenu.jsx";
import { findSupp, describeSupp } from "../suppress.js";

const VERDICT_COLOR = { OK: "#4ec07a", WARNING: "#e0a83a", CRITICAL: "#e06c6c", OFFLINE: "#7a7a7a", SUPPRESSED: "#6c7fa8" };
// Severity for the "Needs attention" list: 0 (offline) / 1 (critical) both red,
// 2 (warning) amber. Lower = more urgent (sorted first).
const SEV_COLOR = { 0: VERDICT_COLOR.CRITICAL, 1: VERDICT_COLOR.CRITICAL, 2: VERDICT_COLOR.WARNING };
// Operator-set host criticality: pulls a flagged host up the triage list and
// shows a badge, so a problem on a business-critical box stands out.
const CRIT_RANK = { critical: 0, high: 1, normal: 2, low: 3 };
const CRIT_COLOR = { critical: VERDICT_COLOR.CRITICAL, high: VERDICT_COLOR.WARNING };
// Badge marking the enrolled host that IS the controller itself.
const CTRL_BADGE = { marginLeft: 6, fontSize: 10, fontWeight: 700, verticalAlign: "middle",
  color: "#fff", background: "var(--accent, #3d7dd8)", borderRadius: 10, padding: "0 6px" };

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

// A failed systemd unit rendered as a one-click "restart it" chip. Right-click
// or left-click both offer to restart the unit on that host (a write, so an
// auditor can't; the command is built server-side from the unit name). Clicks
// stopPropagation so they don't also trigger the card's drill-down.
function UnitChip({ hostId, unit }) {
  const [state, setState] = useState("idle"); // idle | busy | ok | err
  const [msg, setMsg] = useState("");
  const canAct = !!(hostId && unit && unit !== "…");
  // Restart tries to bring the unit back; Clear (reset-failed) just wipes the
  // failed marker — the right move for a unit that can't run (e.g. a VMware
  // vmblock .mount), where restart only fails again.
  const act = (mode, verb) => (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (!canAct || state === "busy") return;
    if (!window.confirm(`${verb} ${unit} on this host?`)) return;
    setState("busy"); setMsg("");
    const call = mode === "reset-failed" ? api.resetUnit : api.restartUnit;
    call(hostId, unit)
      .then((r) => {
        const res = r.result || {};
        const ok = res.ok || res.code === 0;
        setState(ok ? "ok" : "err");
        setMsg(ok ? (mode === "reset-failed" ? "cleared" : "restarted")
                  : (res.error || res.stderr || `exit ${res.code}`));
      })
      .catch((err) => { setState("err"); setMsg(String(err.message || err)); });
  };
  const color = state === "ok" ? VERDICT_COLOR.OK : state === "err" ? VERDICT_COLOR.CRITICAL : VERDICT_COLOR.WARNING;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", border: `1px solid ${color}`,
                   borderRadius: 10, margin: "2px 4px 0 0", overflow: "hidden", whiteSpace: "nowrap" }}>
      <button onClick={act("restart", "Restart")} disabled={!canAct || state === "busy"}
        title={canAct ? `Restart ${unit}` : unit}
        style={{ font: "inherit", fontSize: 11, cursor: canAct ? "pointer" : "default",
                 border: "none", color, background: "transparent", padding: "0 7px", lineHeight: "17px" }}>
        {state === "busy" ? "working…" : unit}
        {state === "ok" && " ✓"}{state === "err" && " ✕"}
        {msg && state === "err" ? <span className="faint" style={{ marginLeft: 4 }}>{msg}</span> : null}
      </button>
      {state !== "ok" && (
        <button onClick={act("reset-failed", "Clear the failed state of")} disabled={!canAct || state === "busy"}
          title={`Clear the failed marker for ${unit} (doesn't start it)`}
          style={{ font: "inherit", fontSize: 11, cursor: canAct ? "pointer" : "default",
                   border: "none", borderLeft: `1px solid ${color}`, color, background: "transparent",
                   padding: "0 6px", lineHeight: "17px", opacity: 0.85 }}>clear</button>
      )}
    </span>
  );
}

function FleetHostCard({ h, onOpenHost }) {
  const v = (h.verdict || "OK").toUpperCase();
  const noData = v === "OFFLINE" || h.disk == null;
  const clickable = !!(onOpenHost && h.id);
  return (
    <div onClick={clickable ? () => onOpenHost(h) : undefined}
         title={clickable ? "View posture / compliance detail" : undefined}
         style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px",
                  cursor: clickable ? "pointer" : "default" }}>
      <div className="spread" style={{ marginBottom: noData ? 0 : 6 }}>
        <span><span className="dot" style={{ background: VERDICT_COLOR[v] || VERDICT_COLOR.OFFLINE }} />{" "}
          <strong>{h.host}</strong>
          {h.is_controller && <span style={CTRL_BADGE} title="This host is the Sysible controller">controller</span>}
          <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>{h.environment}</span></span>
        <span className="faint" style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 6 }}>
          {h.issues > 0 && <span style={{ color: VERDICT_COLOR.WARNING, fontWeight: 700 }}>⚠ {h.issues}</span>}
          {h.limited && <span style={{ color: VERDICT_COLOR.OFFLINE }} title="Posture gathered without root — some checks incomplete">limited</span>}
          {h.postureError && <span style={{ color: VERDICT_COLOR.OFFLINE }}>posture n/a</span>}
          {v === "OFFLINE"
            ? <span style={{ color: VERDICT_COLOR.CRITICAL, fontWeight: 700 }}
                    title="Not reporting — unreachable or powered off">⚠ OFFLINE</span>
            : v}
        </span>
      </div>
      {noData ? (
        <div className="faint" style={{ fontSize: 12 }}>{h.error || "no data"}</div>
      ) : (
        <>
          <Meter label="disk" pct={h.disk} />
          <Meter label="mem" pct={h.mem} />
          <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
            load {h.load1 ?? "—"} / {h.cores} core{h.cores === 1 ? "" : "s"}
          </div>
          {(h.failed > 0 || (h.units && h.units.length) || h.oom > 0 || (h.sysd && h.sysd !== "running")) && (
            <div style={{ fontSize: 12, marginTop: 4, color: VERDICT_COLOR.WARNING }}>
              {h.failed > 0 && (
                <div>
                  <div>{h.failed} crashed service{h.failed > 1 ? "s" : ""}
                    {h.units && h.units.length ? "" : "."}
                    {h.units && h.units.length ? <span className="faint" style={{ marginLeft: 4 }}>— click a unit to restart it</span> : null}</div>
                  {h.units && h.units.length ? (
                    <div style={{ display: "flex", flexWrap: "wrap", marginTop: 2 }}>
                      {h.units.map((u) => <UnitChip key={u} hostId={h.id} unit={u} />)}
                      {h.failed > h.units.length && <span className="faint" style={{ fontSize: 11, marginTop: 2 }}>+{h.failed - h.units.length} more</span>}
                    </div>
                  ) : null}
                </div>
              )}
              {h.oom > 0 && <div>{h.oom} OOM kill{h.oom > 1 ? "s" : ""} (out-of-memory)</div>}
              {h.sysd && h.sysd !== "running" && h.sysd !== "unknown" && <div>systemd: {h.sysd}</div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// One card per environment, combining BOTH lenses: health (worst verdict, peak
// disk/mem, aggregated problem signals) and compliance ("N need attention" from
// the posture sweep). Expands to the per-host cards, which show health meters
// plus a posture issue badge and drill into the full per-host page. Defaults
// open when the environment has any health trouble or posture findings.
function EnvFleetCard({ group, postureLoaded, onOpenHost }) {
  const v = group.verdict;
  const [open, setOpen] = useState(v !== "OK" || group.problematic > 0);
  const attn = group.hosts.filter((h) => h.needsAttention);
  // Click "N needs attention": if it's one host, jump straight to it; if several,
  // expand the environment so each flagged host card is visible.
  const goAttention = (e) => {
    e.stopPropagation();
    if (attn.length === 1) onOpenHost(attn[0]);
    else setOpen(true);
  };
  return (
    <div style={{ border: "1px solid " + (v === "CRITICAL" ? VERDICT_COLOR.CRITICAL : "var(--border)"),
                  borderRadius: 8, overflow: "hidden" }}>
      <button onClick={() => setOpen((o) => !o)}
        style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between",
                 gap: 8, padding: "8px 10px", background: "none", border: "none", cursor: "pointer",
                 color: "var(--text)", textAlign: "left", font: "inherit" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s",
                         fontSize: 10, opacity: 0.6 }}>▶</span>
          <span className="dot" style={{ background: VERDICT_COLOR[v] || VERDICT_COLOR.OFFLINE }} />
          <strong style={{ whiteSpace: "nowrap" }}>{group.env}</strong>
          <span className="faint" style={{ fontSize: 11 }}>
            {group.hosts.length} host{group.hosts.length === 1 ? "" : "s"}
          </span>
        </span>
        <span style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 8 }}>
          {group.counts.OFFLINE > 0 && (
            <span style={{ color: VERDICT_COLOR.CRITICAL, fontWeight: 700 }}
                  title="Host(s) not reporting — unreachable or powered off">
              ⚠ {group.counts.OFFLINE} offline
            </span>
          )}
          {postureLoaded && (group.problematic > 0
            ? <span onClick={goAttention} role="button"
                    title={attn.length ? `${attn.map((h) => h.host).join(", ")} — click to ${attn.length === 1 ? "open" : "view"}` : ""}
                    style={{ color: v === "CRITICAL" ? VERDICT_COLOR.CRITICAL : VERDICT_COLOR.WARNING,
                             cursor: "pointer", textDecoration: "underline dotted" }}>
                {group.problematic} need{group.problematic === 1 ? "s" : ""} attention</span>
            : group.counts.OFFLINE === 0 && <span style={{ color: VERDICT_COLOR.OK }}>all clear</span>)}
          {group.limited > 0 && (
            <span className="faint">· {group.limited} limited</span>
          )}
        </span>
      </button>
      <div style={{ padding: "0 10px 8px" }}>
        <Meter label="disk" pct={group.disk} />
        <Meter label="mem" pct={group.mem} />
        {(group.failed > 0 || group.oom > 0 || group.degraded > 0) && (
          <div style={{ fontSize: 12, marginTop: 4, color: VERDICT_COLOR.WARNING }}>
            {[
              group.failed > 0 && `${group.failed} crashed service${group.failed > 1 ? "s" : ""}`,
              group.oom > 0 && `${group.oom} OOM kill${group.oom > 1 ? "s" : ""}`,
              group.degraded > 0 && `${group.degraded} degraded systemd`,
            ].filter(Boolean).join(" · ")}
          </div>
        )}
      </div>
      {open && (
        <div style={{ padding: "0 10px 10px", display: "grid", gap: 8,
                      borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          {group.hosts.map((h) => <FleetHostCard key={h.host} h={h} onOpenHost={onOpenHost} />)}
        </div>
      )}
    </div>
  );
}

// Curated high-ticket compliance signals for the dashboard strip. The first
// group is derived from the read-only posture sweep (/api/fleet-posture); the
// disk/service ones reuse the fleet-health snapshot already on the dashboard.
// Each counts the hosts where the signal is a problem; green at zero.
const POSTURE_SIGNALS = [
  { key: "reboot_required", label: "Reboot required" },
  { key: "ssh_root_login", label: "SSH root login enabled" },
  { key: "firewall_disabled", label: "Firewall disabled" },
  { key: "mac_not_enforcing", label: "SELinux/AppArmor not enforcing" },
  { key: "eol_os", label: "EOL / unsupported OS" },
  { key: "risky_accounts", label: "UID-0 / empty-password accounts" },
  { key: "cert_expiring", label: "TLS cert expiring < 30 days" },
  { key: "time_unsynced", label: "Time not synchronized" },
];
const POSTURE_SIGNAL_KEYS = POSTURE_SIGNALS.map((s) => s.key);
// Human labels + default severity (1 = critical/red, 2 = warning/amber) for the
// suppressible dashboard findings — posture signals plus the two health signals.
const SIGNAL_LABEL = {
  ...Object.fromEntries(POSTURE_SIGNALS.map((s) => [s.key, s.label])),
  disk_critical: "Disk usage critical (≥ 90%)",
  failed_units: "Failed systemd units",
};
// Severity per signal: 1 = critical (red), 2 = warning (amber). The fleet
// dashboard's compliance strip shows only the CRITICAL ones (the at-a-glance
// "what's on fire" view); warnings still surface on each host's detail page.
// Critical = active compromise vector or imminent outage.
const SIGNAL_SEV = {
  disk_critical: 1,      // ≥90% — imminent outage
  risky_accounts: 1,     // UID-0 dupes / empty-password accounts
  ssh_root_login: 1,     // root login exposed over SSH
  firewall_disabled: 1,  // host has no active firewall
  eol_os: 1,             // unsupported OS = unpatched CVEs
};   // everything else defaults to 2 (warning)
const isCriticalSignal = (key) => SIGNAL_SEV[key] === 1;

// One compliance signal: label + affected-host count, expands to the hosts.
// Each affected host opens the drill-down and carries a "Suppress ▾" so the
// finding can be silenced (per host or per environment). A small muted count of
// hosts where it's already suppressed hangs off the header.
function SignalChip({ signal, canAct, onOpenHost, onDone }) {
  const [open, setOpen] = useState(false);
  const { key, label, hosts, suppressed } = signal;
  const n = hosts.length;
  const nsupp = suppressed.length;
  const color = n > 0 ? VERDICT_COLOR.WARNING : VERDICT_COLOR.OK;
  const expandable = n > 0 || nsupp > 0;
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
      <button onClick={() => expandable && setOpen((o) => !o)}
        style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between",
                 gap: 8, padding: "8px 10px", background: "none", border: "none",
                 cursor: expandable ? "pointer" : "default", color: "var(--text)", textAlign: "left", font: "inherit" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span className="dot" style={{ background: color }} />
          <span style={{ fontSize: 13 }}>{label}</span>
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {nsupp > 0 && <span title={`${nsupp} suppressed`} style={{ fontSize: 11, color: VERDICT_COLOR.SUPPRESSED }}>⊘{nsupp}</span>}
          <span style={{ fontWeight: 700, color }}>{n}</span>
        </span>
      </button>
      {open && expandable && (
        <div style={{ padding: "8px 10px", display: "grid", gap: 6, borderTop: "1px solid var(--border)" }}>
          {hosts.map((h) => (
            <div key={h.id ?? h.host} style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between" }}>
              <button className="btn ghost sm" style={{ flex: 1, textAlign: "left" }}
                      onClick={() => h.id && onOpenHost && onOpenHost(h)}
                      title={h.id ? "View posture detail" : ""}>{h.host}</button>
              {canAct && <SuppressMenu ctx={{ key, host: h.host, env: h.env, bootEpoch: h.boot }} onDone={onDone} />}
            </div>
          ))}
          {nsupp > 0 && (
            <div className="faint" style={{ fontSize: 11 }}>
              Suppressed on: {suppressed.map((h) => h.host).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Collapsible list of every currently-suppressed finding, with who/why and a
// one-click un-suppress. Nothing is hidden — suppressed items live here.
function SuppressedPanel({ items, canAct, onOpenHost, onRemove }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card" style={{ marginTop: 12, padding: 0, overflow: "hidden", borderColor: VERDICT_COLOR.SUPPRESSED }}>
      <button onClick={() => setOpen((o) => !o)}
        style={{ width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "8px 12px",
                 background: "none", border: "none", color: "var(--text)", cursor: "pointer", font: "inherit" }}>
        <span className="dot" style={{ background: VERDICT_COLOR.SUPPRESSED }} />
        <strong style={{ fontSize: 13 }}>Suppressed</strong>
        <span className="faint" style={{ fontSize: 12 }}>{items.length} finding{items.length === 1 ? "" : "s"}</span>
        <span className="faint" style={{ marginLeft: "auto", fontSize: 11 }}>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div style={{ borderTop: "1px solid var(--border)", padding: 6, display: "grid", gap: 3 }}>
          {items.map((it) => (
            <div key={it.supp.id} className="row" style={{ gap: 8, alignItems: "center", fontSize: 12, padding: "3px 6px" }}>
              {it.id
                ? <button className="btn ghost sm" onClick={() => onOpenHost && onOpenHost(it)} title="View host">{it.host}</button>
                : <span className="btn ghost sm" style={{ cursor: "default" }}>{it.host}</span>}
              <span style={{ minWidth: 0, flex: 1 }}>{it.label}
                <span className="faint" style={{ marginLeft: 6 }}>· {describeSupp(it.supp)}{it.supp.reason ? ` — ${it.supp.reason}` : ""}</span>
              </span>
              {canAct && <button className="btn ghost sm" onClick={() => onRemove(it.supp.id)} title="Remove suppression">Un-suppress</button>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// A top-strip metric that, when it has an associated host list, becomes
// clickable and drops down links to those specific hosts (e.g. click
// "Offline / stale 1" to see which host, "Online 3" for the three, etc.).
// Each host link opens that host's detail page.
function MetricCard({ label, value, extra, hosts, onOpenHost, accent }) {
  const [open, setOpen] = useState(false);
  const ref = React.useRef(null);
  const clickable = Array.isArray(hosts) && hosts.length > 0;
  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div className="metric" ref={ref}
         style={{ position: "relative", cursor: clickable ? "pointer" : "default" }}
         onClick={() => clickable && setOpen((o) => !o)}
         title={clickable ? "Click to see which host(s)" : undefined}>
      <div className="label">{label}</div>
      <div className="value" style={accent ? { color: accent } : undefined}>{value}{extra}
        {clickable && <span className="faint" style={{ fontSize: 11, marginLeft: 6 }}>{open ? "▲" : "▾"}</span>}
      </div>
      {open && clickable && (
        <div onClick={(e) => e.stopPropagation()}
             style={{ position: "absolute", top: "100%", left: 0, zIndex: 30, marginTop: 4,
                      minWidth: 200, maxHeight: 280, overflowY: "auto", background: "var(--panel-2)",
                      border: "1px solid var(--border)", borderRadius: 8, padding: 6,
                      boxShadow: "0 8px 24px rgba(0,0,0,0.35)" }}>
          {hosts.map((h) => (
            <button key={h.id ?? h.host} className="btn ghost sm"
                    style={{ display: "flex", width: "100%", justifyContent: "space-between",
                             gap: 8, textAlign: "left", marginBottom: 4 }}
                    disabled={!h.id}
                    onClick={() => { if (h.id && onOpenHost) onOpenHost(h); setOpen(false); }}
                    title={h.id ? "View host detail" : ""}>
              <span>{h.host}{h.ctrl && <span style={CTRL_BADGE} title="This host is the Sysible controller">controller</span>}</span>
              {h.env ? <span className="faint" style={{ fontSize: 11 }}>{h.env}</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Fleet overview: at-a-glance metrics + recent activity. Navigation lives in
// the sidebar, so this is a real home screen rather than a duplicate tile grid.

function fmtWhen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let n = Number(v); let d;
  if (isFinite(n)) { if (n < 1e12) n *= 1000; d = new Date(n); } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

export default function Dashboard({ role, edition, onOpen }) {
  const [q, setQ] = useState("");
  const [activity, setActivity] = useState([]);
  const isSuper = role === "superuser";
  const isAuditor = role === "auditor";

  useEffect(() => {
    if (isSuper) api.activity(12).then((d) => setActivity(d.activity || [])).catch(() => {});
  }, [isSuper]);

  const results = useMemo(() => searchTasks(q), [q]);

  // NOTE: the top-strip counts (m) and hostLists are computed AFTER the
  // fleet-health state below, so online/offline track the SAME server-side
  // reachability the fleet donut uses (and refresh with it) instead of a stale,
  // never-refreshed last_seen window that left "Online" frozen at page-load.

  const recent = useMemo(
    () => [...activity].sort((a, b) => (b.id || 0) - (a.id || 0)).slice(0, 10),
    [activity]
  );

  // Fleet health snapshot (on-demand sweep; optional auto-refresh).
  const [fleet, setFleet] = useState([]);
  const [fleetAt, setFleetAt] = useState(0);
  const [fleetLoading, setFleetLoading] = useState(false);
  const [fleetErr, setFleetErr] = useState("");
  const [fleetAuto, setFleetAuto] = useState(true);   // on by default; refreshes every 10s
  const [updates, setUpdates] = useState([]);         // cached patch status for the summary tile
  useEffect(() => { api.fleetUpdates(0, 0).then((d) => setUpdates(d.hosts || [])).catch(() => {}); }, []);
  const [hostMetaMap, setHostMetaMap] = useState({}); // name -> {criticality, owner, tags, ...}
  useEffect(() => { api.hostMeta().then((d) => setHostMetaMap(d.meta || {})).catch(() => {}); }, []);
  const [tagFilter, setTagFilter] = useState(null);  // service-group (tag) focus for the fleet view

  // All tags in use = the fleet's "service groups". Filtering the fleet-health
  // views (donut/env cards/triage) to a tag lets you focus on one service.
  const allTags = useMemo(() => {
    const s = new Set();
    for (const m of Object.values(hostMetaMap)) for (const t of (m.tags || [])) s.add(t);
    return [...s].sort();
  }, [hostMetaMap]);
  const filteredFleet = useMemo(() => {
    if (!tagFilter) return fleet;
    return fleet.filter((h) => ((hostMetaMap[h.host] || {}).tags || []).includes(tagFilter));
  }, [fleet, tagFilter, hostMetaMap]);

  const fleetInFlight = React.useRef(false);
  const loadFleet = useCallback(() => {
    // Don't stack sweeps: if one is still running (e.g. slow controller), let it
    // finish rather than piling on another request every 10s and starving the
    // BFF's worker pool — which is what made everything feel frozen.
    if (fleetInFlight.current) return;
    fleetInFlight.current = true;
    setFleetLoading(true); setFleetErr("");
    api.fleetHealth()
      .then((d) => { setFleet(d.hosts || []); setFleetAt(Date.now()); })
      .catch((e) => setFleetErr(e.message))
      .finally(() => { fleetInFlight.current = false; setFleetLoading(false); });
  }, []);
  useEffect(() => { loadFleet(); }, [loadFleet]);
  useEffect(() => {
    if (!fleetAuto) return undefined;
    const t = setInterval(loadFleet, 10000);
    return () => clearInterval(t);
  }, [fleetAuto, loadFleet]);

  // Have we completed (or failed) a health sweep yet? Gates the "nothing is
  // checking in" alarm so it can't flash before the first sweep runs.
  const healthAttempted = fleetAt > 0 || !!fleetErr;

  // Top-strip counts come from the SAME deduped host set as the fleet donut and
  // the licensed "Hosts enrolled" count (list_merged_hosts: agents + SSH, with
  // duplicates merged). Deriving Online/Offline from this deduped set — rather
  // than the raw agent inventory — guarantees Online can never exceed Enrolled
  // and that the tiles agree with the donut. SSH-only hosts report online===null
  // (they can't be passively judged): they count toward the total but as neither
  // online nor offline.
  const m = useMemo(() => {
    const hosts = fleet.filter(Boolean);
    const total = hosts.length;
    const online = hosts.filter((h) => h.online === true).length;
    const offline = hosts.filter((h) => h.online === false).length;
    const envs = new Set(hosts.map((h) => h.environment || "Unassigned")).size;
    // Only agent hosts that actually answered (online true/false) count toward
    // the "nothing is checking in" alarm — an all-SSH fleet (online unknown)
    // must not trip it.
    return { total, online, offline, envs, judgeable: online + offline };
  }, [fleet]);

  // Emergency state for the dashboard: either the controller isn't answering the
  // health sweep at all, or a completed sweep shows nothing is checking in. Only
  // raised AFTER a sweep has been attempted, so the first paint doesn't flash red
  // while health is still gathering.
  const healthDown = !!fleetErr;
  const noCheckins = m.judgeable > 0 && m.online === 0;
  const emergency = healthAttempted && (healthDown || noCheckins);

  // Host lists behind the top-strip counts, so each count can drop down the
  // specific hosts (id → drill-down, hostname + environment shown).
  const hostLists = useMemo(() => {
    // Drill-downs open by agent host_id when present (merged/agent hosts), else
    // the entry id (SSH-only). Same deduped set as the counts above.
    const mk = (h) => ({ id: h.agent_id || h.id, host: h.host, env: h.environment || "Unassigned", ctrl: !!h.is_controller });
    const hosts = fleet.filter(Boolean);
    const online = [], offline = [];
    for (const h of hosts) { if (h.online === true) online.push(mk(h)); else if (h.online === false) offline.push(mk(h)); }
    return { all: hosts.map(mk), online, offline };
  }, [fleet]);

  const openHost = useCallback((h) => onOpen("host", { id: h.id, label: h.host }), [onOpen]);

  // Compliance / posture sweep (read-only, on-demand; cached by the BFF).
  const [posture, setPosture] = useState([]);
  const [postureAt, setPostureAt] = useState(0);
  const [postureLoading, setPostureLoading] = useState(false);
  const [postureErr, setPostureErr] = useState("");

  const loadPosture = useCallback((refresh = false) => {
    setPostureLoading(true); setPostureErr("");
    api.fleetPosture(refresh)
      .then((d) => { setPosture(d.hosts || []); setPostureAt((d.ts ? d.ts * 1000 : Date.now())); })
      .catch((e) => setPostureErr(e.message))
      .finally(() => setPostureLoading(false));
  }, []);
  useEffect(() => { loadPosture(false); }, [loadPosture]);

  // Finding suppressions (silence a signal on a host/env, with a reason/expiry).
  const [supps, setSupps] = useState([]);
  const loadSupps = useCallback(() => {
    api.suppressions().then((d) => setSupps(d.suppressions || [])).catch(() => {});
  }, []);
  useEffect(() => { loadSupps(); }, [loadSupps]);

  // Per-host context needed to evaluate a suppression: its name (host scope), its
  // environment (env scope), and its current boot time (baseline for the
  // "remind after next reboot" type). Env comes from the fleet health record;
  // boot time from the posture sweep.
  const hostCtx = useMemo(() => {
    const bootById = {};
    for (const p of posture) {
      const b = p.posture && p.posture.os ? p.posture.os.boot_epoch : undefined;
      if (b != null) bootById[p.id] = Number(b);
    }
    const m2 = {};
    for (const h of fleet) m2[h.id] = { host: h.host, env: h.environment || "Unassigned", boot: bootById[h.id] ?? null };
    for (const p of posture) if (!m2[p.id]) m2[p.id] = { host: p.host, env: p.environment || "Unassigned", boot: bootById[p.id] ?? null };
    return m2;
  }, [fleet, posture]);

  // Active suppression for a (hostId, findingKey), or null.
  const suppFor = useCallback((hostId, key) => {
    const c = hostCtx[hostId];
    if (!c) return null;
    return findSupp(supps, { host: c.host, env: c.env, key, bootEpoch: c.boot });
  }, [supps, hostCtx]);

  // ONE per-host analysis that every grader reads, so the donut, env dots, host
  // cards, compliance strip, and triage list all agree — AND all honour
  // suppressions. For each host: the ACTIVE findings (worst-first severity), the
  // SUPPRESSED findings (with their suppression), and the resulting verdict
  // (OFFLINE / CRITICAL / WARNING / SUPPRESSED / OK). SUPPRESSED = a host whose
  // only findings are suppressed (would otherwise flag).
  const analysis = useMemo(() => {
    const flagsById = {};
    for (const p of posture) flagsById[p.id] = p.flags || {};
    const per = {};
    for (const h of filteredFleet) {
      const active = [], suppressed = [];
      let sev = 9;
      // Add a finding. If it has a suppressible key and an active suppression,
      // it goes to `suppressed` instead of counting.
      const add = (text, s, key) => {
        if (key) {
          const sup = suppFor(h.id, key);
          if (sup) { suppressed.push({ key, label: SIGNAL_LABEL[key] || text, text, supp: sup }); return; }
        }
        active.push({ text, sev: s, key: key || null });
        sev = Math.min(sev, s);
      };
      let verdict;
      if (h.online === false) {
        verdict = "OFFLINE";
      } else {
        if ((h.disk ?? 0) >= 90) add(`disk ${h.disk}%`, 1, "disk_critical");
        else if ((h.disk ?? 0) >= 80) add(`disk ${h.disk}%`, 2);            // warn, not a named signal
        if ((h.mem ?? 0) >= 90) add(`mem ${h.mem}%`, 1);
        if ((h.failed ?? 0) > 0) {
          const names = (h.units || []).filter(Boolean);
          const text = names.length
            ? (names.length === 1 ? `${names[0]} failed` : `${names[0]} +${h.failed - 1} more failed`)
            : `${h.failed} failed unit${h.failed > 1 ? "s" : ""}`;
          add(text, h.failed >= 3 ? 1 : 2, "failed_units");
        }
        if ((h.oom ?? 0) > 0) add(`${h.oom} OOM`, 1);
        const flags = flagsById[h.id] || {};
        for (const key of POSTURE_SIGNAL_KEYS) {
          if (flags[key] === true) add(SIGNAL_LABEL[key], SIGNAL_SEV[key] || 2, key);
        }
        verdict = active.length === 0
          ? (suppressed.length ? "SUPPRESSED" : "OK")
          : (sev === 1 ? "CRITICAL" : "WARNING");
      }
      per[h.id] = { active, suppressed, verdict, worstSev: sev };
    }
    return per;
  }, [filteredFleet, posture, suppFor]);

  const order = { CRITICAL: 0, WARNING: 1, SUPPRESSED: 2.5, OFFLINE: 2, OK: 3 };

  // Build the dashboard compliance strip. Posture signals come straight from the
  // POSTURE sweep (so they show even if the health sweep is empty/slow — the two
  // are independent); the two health signals come from the fleet-health sweep.
  // Each finding is split active vs SUPPRESSED via suppFor, and every host entry
  // carries the context (env + boot time) the Suppress menu needs.
  const complianceSignals = useMemo(() => {
    const keys = [...POSTURE_SIGNAL_KEYS, "disk_critical", "failed_units"];
    const acc = Object.fromEntries(keys.map((k) => [k, { active: [], suppressed: [] }]));
    const place = (key, id, host) => {
      const c = hostCtx[id] || {};
      const entry = { id, host, env: c.env || "Unassigned", boot: c.boot ?? null };
      const s = suppFor(id, key);
      if (s) acc[key].suppressed.push({ ...entry, supp: s });
      else acc[key].active.push(entry);
    };
    // Only count signals for hosts in the current (tag-filtered) view that
    // aren't offline. Previously the posture loop ran over the WHOLE posture
    // sweep, so the chips ignored the service-group filter and kept counting an
    // offline host's stale cached flags — contradicting the donut/env cards,
    // which run over filteredFleet and grade offline hosts as OFFLINE (not a
    // live exposure). This aligns the chips with the per-host `analysis`.
    const eligible = new Set(filteredFleet.filter((h) => h.online !== false).map((h) => h.id));
    for (const p of posture) {
      if (!p.flags || !eligible.has(p.id)) continue;
      for (const key of POSTURE_SIGNAL_KEYS) if (p.flags[key] === true) place(key, p.id, p.host);
    }
    for (const h of filteredFleet) {
      if (h.online === false) continue;
      if ((h.disk ?? 0) >= 90) place("disk_critical", h.id, h.host);
      if ((h.failed ?? 0) > 0) place("failed_units", h.id, h.host);
    }
    return keys.map((k) => ({ key: k, label: SIGNAL_LABEL[k], hosts: acc[k].active, suppressed: acc[k].suppressed }))
      .sort((a, b) => b.hosts.length - a.hosts.length);
  }, [posture, filteredFleet, suppFor, hostCtx]);

  // The strip shows CRITICAL signals only — the login view is "what's on fire",
  // not every hardening nit. (Warnings still show on each host's detail page.)
  const criticalSignals = useMemo(
    () => complianceSignals.filter((s) => isCriticalSignal(s.key)),
    [complianceSignals]);
  const issuesTotal = criticalSignals.reduce((n, s) => n + (s.hosts.length > 0 ? 1 : 0), 0);

  // Every active suppression that applies to a host/env currently in the fleet —
  // the source for both the donut's "suppressed" metric and the Suppressed panel.
  // Built from the raw suppression list (not just the compliance strip) so it
  // also counts findings that aren't dashboard signals (e.g. password
  // complexity suppressed from a host's detail page). Snoozes past their expiry
  // are dropped. Each carries a friendly finding label + a host id when the
  // scope is a single host (so the panel row can open it).
  const suppressedList = useMemo(() => {
    const now = Date.now();
    const hostNames = new Set(filteredFleet.map((h) => h.host));
    const idByName = {};
    for (const h of filteredFleet) idByName[h.host] = h.id;
    const envs = new Set(filteredFleet.map((h) => h.environment || "Unassigned"));
    return supps
      .filter((s) => (s.type !== "snooze" || !s.until || s.until * 1000 > now)
                     && (s.scope === "host" ? hostNames.has(s.target) : envs.has(s.target)))
      .map((s) => ({
        supp: s, scope: s.scope, target: s.target, key: s.key,
        id: s.scope === "host" ? idByName[s.target] : null,
        host: s.scope === "host" ? s.target : `env: ${s.target}`,
        label: SIGNAL_LABEL[s.key] || s.key.replace(/[._]/g, " "),
      }));
  }, [supps, filteredFleet]);

  async function unsuppress(id) {
    try { await api.removeSuppression(id); loadSupps(); } catch { /* ignore */ }
  }

  // Posture findings per host id (issue count + scan error/limited), shared by
  // the donut, the environment rollup, and the triage list so all three grade a
  // host the same way.
  const postById = useMemo(() => {
    const m = {};
    for (const p of posture) {
      m[p.id] = {
        issues: p.flags ? Object.values(p.flags).filter((v) => v === true).length : 0,
        postureError: p.error || null,
        limited: !!p.limited,
      };
    }
    return m;
  }, [posture]);

  const fleetSummary = useMemo(() => {
    const counts = { OK: 0, WARNING: 0, CRITICAL: 0, SUPPRESSED: 0, OFFLINE: 0 };
    for (const h of filteredFleet) {
      const v = (analysis[h.id] || {}).verdict || "OK";
      counts[v] = (counts[v] || 0) + 1;
    }
    return { counts };
  }, [filteredFleet, analysis]);

  const patch = useMemo(() => {
    let withUpd = 0, sec = 0;
    for (const h of updates) { if ((h.total || 0) > 0) withUpd++; sec += h.security || 0; }
    return { withUpd, sec, loaded: updates.length > 0 };
  }, [updates]);

  // Ranked triage list: every host with something wrong, worst first, each with
  // its specific reasons. Built from the health + posture data already loaded —
  // the "what do I look at first" view a per-environment grid doesn't give.
  const attention = useMemo(() => {
    const rows = [];
    for (const h of filteredFleet) {
      const a = analysis[h.id];
      if (!a) continue;
      const pe = postById[h.id] || {};
      // Only ACTIVE (non-suppressed) findings drive the triage list. A failed
      // posture scan is included too, so this list doesn't silently omit hosts
      // the per-environment "needs attention" rollup counts (which also flags
      // postureError).
      let reasons, sev;
      if (a.verdict === "OFFLINE") { reasons = ["offline"]; sev = 0; }
      else if (a.active.length > 0) {
        reasons = a.active.map((f) => f.text);
        sev = a.active.reduce((mn, f) => Math.min(mn, f.sev), 3);
      }
      else if (pe.postureError) { reasons = ["posture scan incomplete"]; sev = 2; }
      else continue;   // clean, or only suppressed
      const crit = (hostMetaMap[h.host] || {}).criticality || "normal";
      rows.push({ id: h.id, host: h.host, env: h.environment || "Unassigned", sev, reasons, crit, ctrl: !!h.is_controller });
    }
    // Worst first; within a severity, business-critical hosts rank above others.
    rows.sort((a, b) => a.sev - b.sev
      || (CRIT_RANK[a.crit] ?? 2) - (CRIT_RANK[b.crit] ?? 2)
      || b.reasons.length - a.reasons.length);
    return rows;
  }, [filteredFleet, analysis, hostMetaMap, postById]);

  // Single environment-grouped rollup joining the two lenses by host id: each
  // host carries its live health (verdict, disk/mem, problem signals) AND its
  // posture issue count; each environment carries worst verdict, peak disk/mem,
  // summed health signals, and how many hosts need attention. Health is the
  // base set (always present); posture issues are attached when scanned.
  const fleetEnvs = useMemo(() => {
    const g = {};
    let total = 0, clear = 0;
    for (const h of filteredFleet) {
      const env = h.environment || "Unassigned";
      const pe = postById[h.id] || {};
      const a = analysis[h.id] || { active: [], suppressed: [], verdict: "OK" };
      // Verdict from the unified analysis, so the env dot + host card match the
      // needs-attention list and the compliance strip AND honour suppressions.
      const ev = a.verdict;
      const activeIssues = a.active.filter((f) => f.key).length;   // suppressible findings still active
      // Matches the env card's "N needs attention" number (`problematic` below):
      // offline is surfaced by the card's own separate "N offline" badge, so it
      // isn't folded in here — otherwise the count and the host list it drills
      // into (this same flag) disagreed whenever an environment had offline hosts.
      const needsAttention = a.active.length > 0 || !!pe.postureError;
      const host = { ...h, issues: activeIssues, postureError: pe.postureError || null,
                     limited: !!pe.limited, verdict: ev, needsAttention };
      const e = g[env] || (g[env] = {
        env, hosts: [], counts: { OK: 0, WARNING: 0, CRITICAL: 0, SUPPRESSED: 0, OFFLINE: 0 },
        disk: null, mem: null, failed: 0, oom: 0, degraded: 0, problematic: 0, suppressed: 0, limited: 0,
      });
      e.hosts.push(host);
      e.counts[ev] = (e.counts[ev] || 0) + 1;
      if (h.disk != null) e.disk = Math.max(e.disk ?? 0, h.disk);
      if (h.mem != null) e.mem = Math.max(e.mem ?? 0, h.mem);
      e.failed += h.failed || 0;
      e.oom += h.oom || 0;
      if (h.sysd && h.sysd !== "running" && h.sysd !== "unknown") e.degraded += 1;
      // "Needs attention" = has an ACTIVE finding (suppressed ones don't count).
      const trouble = a.active.length > 0 || host.postureError;
      if (trouble) e.problematic += 1;
      if (a.suppressed.length > 0) e.suppressed += 1;
      if (host.limited) e.limited += 1;
      total += 1;
      // A "limited" host (posture gathered without root) isn't counted clean -
      // its root-only checks couldn't be trusted.
      if ((ev === "OK" || ev === "SUPPRESSED") && !trouble && !host.limited) clear += 1;
    }
    const envs = Object.values(g).map((e) => {
      // A down host makes the whole environment critical (red), not grey/amber —
      // an unreachable host is the most urgent thing to see at a glance.
      e.verdict = (e.counts.CRITICAL > 0 || e.counts.OFFLINE > 0) ? "CRITICAL"
        : e.counts.WARNING > 0 ? "WARNING"
        : e.counts.OK > 0 ? "OK"
        : e.counts.SUPPRESSED > 0 ? "SUPPRESSED" : "OFFLINE";
      e.hosts.sort((a, b) =>
        (order[(a.verdict || "OK").toUpperCase()] ?? 9) - (order[(b.verdict || "OK").toUpperCase()] ?? 9)
        || (b.issues || 0) - (a.issues || 0) || (b.disk || 0) - (a.disk || 0));
      return e;
    });
    envs.sort((a, b) => (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9)
      || b.problematic - a.problematic || a.env.localeCompare(b.env));
    return { envs, total, clear };
  }, [filteredFleet, postById, analysis]);

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
      {/* Emergency strip: the controller isn't answering, or nothing is checking
          in. Deliberately loud — a calm green "Online" while the fleet is dark is
          exactly what we don't want. */}
      {emergency && (
        <div className="alarm-strip" style={{ marginBottom: 14, borderRadius: 8, padding: "12px 16px",
                      background: "rgba(224,108,108,0.14)", border: `2px solid ${VERDICT_COLOR.CRITICAL}`,
                      display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 22, lineHeight: 1 }}>🚨</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 700, color: VERDICT_COLOR.CRITICAL, fontSize: 15 }}>
              {healthDown ? "Fleet health unavailable — controller not responding"
                          : `No hosts are checking in — 0 of ${m.total} online`}
            </div>
            <div className="faint" style={{ fontSize: 13, marginTop: 2 }}>
              {healthDown
                ? `The controller isn't answering the health sweep${fleetErr ? ` (${fleetErr})` : ""}. Queries and actions may be failing or delayed.`
                : "Agents aren't reporting in. They may be down, or the controller can't reach them — commands you issue may not run."}
            </div>
          </div>
          <button className="btn sm" onClick={() => { loadFleet(); loadPosture(false); }} disabled={fleetLoading}>
            {fleetLoading ? <span className="spin" /> : "Re-check"}
          </button>
        </div>
      )}

      {/* The task search routes into the tool pages, which auditors can't use,
          so it's hidden for the read-only role. */}
      {!isAuditor && (
        <input className="search-bar"
               placeholder='Search for a task, e.g. "create a user" or "add a repository"…'
               value={q} onChange={(e) => setQ(e.target.value)} />
      )}

      <div className="metric-row">
        {/* Use the license-authoritative deduped host_count (same number the
            "Community · N/limit hosts" badge shows) so the two never disagree.
            A host enrolled twice (re-enroll dupe, or agent+SSH) counts once. */}
        <MetricCard label="Hosts enrolled" value={edition?.host_count ?? m.total}
          extra={edition?.host_limit ? <span className="faint" style={{ fontSize: 14, fontWeight: 400 }}> / {edition.host_limit}</span> : null}
          hosts={hostLists.all} onOpenHost={openHost} />
        <MetricCard label="Online" value={m.online}
          accent={m.total > 0 && m.online === 0 ? VERDICT_COLOR.CRITICAL : undefined}
          extra={<span className={`dot ${m.total > 0 && m.online === 0 ? "bad" : "ok"}`} />}
          hosts={hostLists.online} onOpenHost={openHost} />
        <MetricCard label="Offline / stale" value={m.offline}
          extra={m.offline > 0 ? <span className="dot bad" /> : null}
          hosts={hostLists.offline} onOpenHost={openHost} />
        {patch.loaded && (
          <div className="metric" style={{ cursor: "pointer" }} onClick={() => onOpen("updates")}
               title="Open Update Hosts">
            <div className="label">Needs patching</div>
            <div className="value">{patch.withUpd}
              {patch.sec > 0 && <span style={{ fontSize: 14, fontWeight: 400, color: VERDICT_COLOR.CRITICAL }}> · {patch.sec} sec</span>}
            </div>
          </div>
        )}
        <div className="metric">
          <div className="label">Environments</div>
          <div className="value">{m.envs}</div>
        </div>
      </div>

      {attention.length > 0 && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <strong>Needs attention
              <span className="faint" style={{ fontSize: 12, marginLeft: 6 }}>
                {attention.length} host{attention.length === 1 ? "" : "s"} — worst first
              </span>
            </strong>
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            {attention.slice(0, 8).map((r) => (
              <div key={r.id} onClick={() => openHost(r)} title="View host detail"
                   style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 9px",
                            border: "1px solid var(--border)", borderRadius: 6, cursor: "pointer" }}>
                <span className="dot" style={{ background: SEV_COLOR[r.sev] || VERDICT_COLOR.WARNING }} />
                <strong style={{ whiteSpace: "nowrap" }}>{r.host}</strong>
                {r.ctrl && <span style={CTRL_BADGE} title="This host is the Sysible controller">controller</span>}
                <span className="faint" style={{ fontSize: 11 }}>{r.env}</span>
                {CRIT_COLOR[r.crit] && (
                  <span style={{ fontSize: 10, fontWeight: 700, color: CRIT_COLOR[r.crit],
                                 border: `1px solid ${CRIT_COLOR[r.crit]}`, borderRadius: 10, padding: "0 6px" }}
                        title="Operator-set criticality">{r.crit}</span>
                )}
                <span style={{ marginLeft: "auto", display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                  {r.reasons.map((x, i) => (
                    <span key={i} style={{ fontSize: 11, color: SEV_COLOR[r.sev] || VERDICT_COLOR.WARNING,
                                           border: `1px solid ${SEV_COLOR[r.sev] || VERDICT_COLOR.WARNING}`,
                                           borderRadius: 10, padding: "0 8px" }}>{x}</span>
                  ))}
                </span>
              </div>
            ))}
            {attention.length > 8 && <div className="faint" style={{ fontSize: 11 }}>+{attention.length - 8} more</div>}
          </div>
        </div>
      )}

      <div className="card" style={{ marginTop: 14 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <strong>Fleet
            {posture.length > 0 && (
              <span className="faint" style={{ fontSize: 12, marginLeft: 8 }}>
                {issuesTotal === 0 ? "no critical findings" : `${issuesTotal} critical signal${issuesTotal === 1 ? "" : "s"} with findings`}
              </span>
            )}
          </strong>
          <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            {fleetAt > 0 && <span className="faint" style={{ fontSize: 12 }}>health {new Date(fleetAt).toLocaleTimeString()}</span>}
            {postureAt > 0 && <span className="faint" style={{ fontSize: 12 }}>· scan {new Date(postureAt).toLocaleTimeString()}</span>}
            <label className="checkrow" style={{ margin: 0 }}>
              <input type="checkbox" checked={fleetAuto} onChange={(e) => setFleetAuto(e.target.checked)} />
              <span className="faint">Auto (10s)</span>
            </label>
            <button className="btn ghost sm" onClick={() => loadPosture(true)} disabled={postureLoading}>
              {postureLoading ? <span className="spin" /> : "Run posture scan"}
            </button>
            <button className="btn ghost sm" onClick={() => { loadFleet(); loadPosture(false); }} disabled={fleetLoading}>
              {fleetLoading ? <span className="spin" /> : "Refresh"}
            </button>
          </div>
        </div>
        {allTags.length > 0 && (
          <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 8, alignItems: "center" }}>
            <span className="faint" style={{ fontSize: 11 }}>Service group:</span>
            <button className="btn ghost sm" style={{ borderColor: !tagFilter ? VERDICT_COLOR.OK : "var(--border)" }}
                    onClick={() => setTagFilter(null)}>All</button>
            {allTags.map((t) => (
              <button key={t} className="btn ghost sm"
                      style={{ borderColor: tagFilter === t ? VERDICT_COLOR.OK : "var(--border)" }}
                      onClick={() => setTagFilter(tagFilter === t ? null : t)}>{t}</button>
            ))}
            {tagFilter && <span className="faint" style={{ fontSize: 11 }}>· showing hosts tagged “{tagFilter}”</span>}
          </div>
        )}
        {(fleetErr || postureErr) && <div className="error-box">{fleetErr || postureErr}</div>}
        {fleetEnvs.total === 0 && posture.length === 0 ? (
          <div className="empty" style={{ padding: 16 }}>
            {fleetLoading ? "Gathering fleet health…" : "No host data yet — click Refresh."}
          </div>
        ) : (
          <>
            {/* Two summaries side by side: fleet-health verdict donut + the
                high-ticket compliance signal chips. Distinct lenses, not a repeat
                of the environment list below. */}
            <div className="row" style={{ gap: 24, alignItems: "flex-start", flexWrap: "wrap", marginBottom: 14 }}>
              <div className="row" style={{ gap: 14, alignItems: "center" }}>
                <Donut segments={[
                  { value: fleetSummary.counts.OK, color: VERDICT_COLOR.OK },
                  { value: fleetSummary.counts.WARNING, color: VERDICT_COLOR.WARNING },
                  { value: fleetSummary.counts.CRITICAL, color: VERDICT_COLOR.CRITICAL },
                  { value: fleetSummary.counts.SUPPRESSED, color: VERDICT_COLOR.SUPPRESSED },
                  { value: fleetSummary.counts.OFFLINE, color: VERDICT_COLOR.OFFLINE },
                ]} />
                <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>
                  <span><span className="dot" style={{ background: VERDICT_COLOR.OK }} /> {fleetSummary.counts.OK} OK</span>
                  <span><span className="dot" style={{ background: VERDICT_COLOR.WARNING }} /> {fleetSummary.counts.WARNING} warning</span>
                  <span><span className="dot" style={{ background: VERDICT_COLOR.CRITICAL }} /> {fleetSummary.counts.CRITICAL} critical</span>
                  {fleetSummary.counts.SUPPRESSED > 0 && (
                    <span title={`${fleetSummary.counts.SUPPRESSED} host${fleetSummary.counts.SUPPRESSED === 1 ? "" : "s"} whose only findings are suppressed (this grey slice). All ${suppressedList.length} suppression${suppressedList.length === 1 ? "" : "s"} are listed in the Suppressed panel below.`}>
                      <span className="dot" style={{ background: VERDICT_COLOR.SUPPRESSED }} /> {fleetSummary.counts.SUPPRESSED} suppressed</span>
                  )}
                  <span><span className="dot" style={{ background: VERDICT_COLOR.OFFLINE }} /> {fleetSummary.counts.OFFLINE} offline</span>
                </div>
              </div>
              {posture.length > 0 ? (
                <div style={{ flex: 1, minWidth: 300, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 8 }}>
                  {criticalSignals.map((s) => (
                    <SignalChip key={s.key} signal={s} canAct={!isAuditor} onOpenHost={openHost} onDone={loadSupps} />
                  ))}
                </div>
              ) : (
                <div className="faint" style={{ flex: 1, minWidth: 300, fontSize: 12, alignSelf: "center" }}>
                  {postureLoading ? "Scanning fleet posture…" : "Run a posture scan to add compliance signals."}
                </div>
              )}
            </div>

            <div className="section-title" style={{ margin: "4px 0 6px" }}>
              Environments <span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>
                — {fleetEnvs.clear}/{fleetEnvs.total} hosts clear
              </span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 10 }}>
              {fleetEnvs.envs.map((e) => (
                <EnvFleetCard key={e.env} group={e} postureLoaded={posture.length > 0} onOpenHost={openHost} />
              ))}
            </div>

            {suppressedList.length > 0 && (
              <SuppressedPanel items={suppressedList} canAct={!isAuditor} onOpenHost={openHost} onRemove={unsuppress} />
            )}

            <div className="faint" style={{ fontSize: 11, marginTop: 10 }}>
              Health is live; compliance is the last read-only posture scan. Open an environment for its hosts, then a host for the full per-category drill-down.
              {suppressedList.length > 0 && " Suppressed findings don't count toward the donut or “needs attention”."}
            </div>
          </>
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
