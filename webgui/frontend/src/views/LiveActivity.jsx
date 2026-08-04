import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Live Activity & Logs: attributed activity feed across the fleet, plus the
// controller's own log. Auto-refreshes while open.
function fmtTime(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

// Collapse same-action-different-host entries (same user/description/command
// within 30s) into one group with a combined host list — mirrors the desktop.
const GROUP_WINDOW_S = 30;
function groupActivity(entries) {
  const groups = [];
  const openByKey = {};
  const sorted = [...entries].sort((a, b) => (a.id || 0) - (b.id || 0)); // oldest first
  for (const e of sorted) {
    const key = `${e.username}|${e.description}|${e.command}`;
    const ts = Number(e.timestamp || 0);
    const g = openByKey[key];
    if (g && Math.abs(ts - g._lastTs) <= GROUP_WINDOW_S) {
      if (e.host && !g.hosts.includes(e.host)) g.hosts.push(e.host);
      g._lastTs = ts; g.timestamp = Math.max(g.timestamp, ts); g.id = Math.max(g.id, e.id || 0);
    } else {
      const ng = { id: e.id || 0, timestamp: ts, _lastTs: ts, username: e.username,
        description: e.description, command: e.command, hosts: e.host ? [e.host] : [] };
      groups.push(ng); openByKey[key] = ng;
    }
  }
  return groups.sort((a, b) => b.id - a.id); // newest first
}

// Summarize a group's host list using the fleet inventory so a combined entry
// reads "all servers (all environments)" / "all dev servers" instead of a long
// hostname list. `inv` is /api/hosts ({label, environment}).
function summarizeHosts(hostnames, inv) {
  const sel = [...new Set((hostnames || []).filter(Boolean))];
  if (sel.length === 0) return "";
  if (sel.length === 1) return sel[0];
  if (!inv || inv.length === 0) return `${sel.length} hosts: ${sel.join(", ")}`;

  const envOf = {};                 // hostname -> environment
  const envAll = {};                // environment -> Set of all its hostnames
  for (const h of inv) {
    const env = h.environment || "Unassigned";
    envOf[h.label] = env;
    (envAll[env] ||= new Set()).add(h.label);
  }

  const known = sel.filter((h) => h in envOf);
  // Every host in the fleet → "all servers (all environments)".
  if (known.length === inv.length && known.length === sel.length) {
    return "all servers (all environments)";
  }

  const byEnv = {};                 // environment -> selected hostnames
  const unknown = [];
  for (const h of sel) (h in envOf ? (byEnv[envOf[h]] ||= []) : unknown).push(h);

  const parts = Object.keys(byEnv).map((env) =>
    byEnv[env].length === envAll[env].size
      ? `all ${env} servers`        // whole environment covered
      : byEnv[env].join(", "));     // partial — list the hosts
  if (unknown.length) parts.push(unknown.join(", "));
  return parts.join(", ");
}

// Heuristic: does this actor look like non-human automation — an API key, a
// service/bot identity — rather than a person? Lets operators hide machine
// noise (e.g. an integration polling the fleet on a schedule) from the feed
// VIEW without touching the underlying audit records, which stay intact. The
// explicit actor dropdown is the precise control; this just powers the default
// "Hide automation" toggle.
const AUTOMATION_RX = /(?:^|[\s_-])(?:api[\s_-]?key|apikey|token|bot|robot|svc|service[\s_-]?account|automation|integration|daemon|scheduler|system)(?:[\s_-]|$)/i;
export function isAutomationActor(name) {
  return AUTOMATION_RX.test(String(name || ""));
}

export default function LiveActivity({ role }) {
  // The read-only 'auditor' role may read the activity feed but NOT the
  // controller service log (which stays superuser-only and 403s for them).
  const isAuditor = role === "auditor";
  const [tab, setTab] = useState("activity");
  const [activity, setActivity] = useState([]);
  const [log, setLog] = useState("");
  const [err, setErr] = useState("");
  const [auto, setAuto] = useState(true);
  const [detail, setDetail] = useState(null);
  const [copied, setCopied] = useState(false);
  const [hostInv, setHostInv] = useState([]);  // fleet inventory for env-aware host labels
  const [q, setQ] = useState("");              // free-text filter (user / host / action)
  const [filterUser, setFilterUser] = useState("");   // "" = all users
  const [hideAuto, setHideAuto] = useState(true);     // hide API-key/automation noise by default
  const timer = useRef(null);

  useEffect(() => { api.hosts().then((d) => setHostInv(d.hosts || [])).catch(() => {}); }, []);

  async function load() {
    try {
      if (tab === "activity") {
        const d = await api.activity(200);
        setActivity(d.activity || []);
      } else {
        const d = await api.controllerLog(500);
        setLog(typeof d === "string" ? d : (d.log || d.text || JSON.stringify(d)));
      }
    } catch (e) { setErr(e.message); }
  }

  useEffect(() => {
    load();
    if (auto) { timer.current = setInterval(load, 4000); }
    return () => clearInterval(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, auto]);

  // Group first, then apply the view filters. Filtering is purely presentational
  // — the audit records behind these rows are untouched (and remain in the
  // tamper-evident chain); this only changes what THIS operator is looking at.
  const grouped = groupActivity(activity);
  const actors = [...new Set(grouped.map((g) => g.username).filter(Boolean))].sort(
    (a, b) => a.localeCompare(b));
  const ql = q.trim().toLowerCase();
  const shown = grouped.filter((g) => {
    if (filterUser && g.username !== filterUser) return false;
    // Selecting a specific actor overrides "hide automation" so you can still
    // inspect exactly what an API key has been doing.
    if (hideAuto && !filterUser && isAutomationActor(g.username)) return false;
    if (ql) {
      const hay = `${g.username || ""} ${(g.hosts || []).join(" ")} ${g.description || ""}`.toLowerCase();
      if (!hay.includes(ql)) return false;
    }
    return true;
  });
  const hiddenAuto = (hideAuto && !filterUser)
    ? grouped.reduce((n, g) => n + (isAutomationActor(g.username) ? 1 : 0), 0) : 0;

  return (
    <div>
      <div className="tabs" style={{ marginBottom: 14 }}>
        <button className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")}>Activity Feed</button>
        {!isAuditor && <button className={tab === "log" ? "active" : ""} onClick={() => setTab("log")}>Controller Log</button>}
        <div style={{ flex: 1 }} />
        <label className="checkrow" style={{ margin: 0 }}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          <span className="faint">Auto-refresh</span>
        </label>
        <button className="btn ghost sm" onClick={load}>Refresh</button>
        {tab === "activity" && <button className="btn ghost sm" onClick={() => setActivity([])}>Clear view</button>}
      </div>

      {err && <div className="error-box">{err}</div>}

      {tab === "activity" && activity.length > 0 && (
        <div className="spread" style={{ gap: 10, marginBottom: 10, flexWrap: "wrap", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter user / host / action…"
                   style={{ minWidth: 200 }} />
            <select value={filterUser} onChange={(e) => setFilterUser(e.target.value)} title="Filter by user">
              <option value="">All users</option>
              {actors.map((u) => (
                <option key={u} value={u}>{u}{isAutomationActor(u) ? " (automation)" : ""}</option>
              ))}
            </select>
            <label className="checkrow" style={{ margin: 0 }}
                   title="Hide API-key / service-account rows from this view. The records are kept — this only filters what you see.">
              <input type="checkbox" checked={hideAuto} onChange={(e) => setHideAuto(e.target.checked)} />
              <span className="faint">Hide automation{hiddenAuto ? ` (${hiddenAuto} hidden)` : ""}</span>
            </label>
          </div>
          <span className="faint" style={{ fontSize: 12 }}>{shown.length} of {grouped.length} shown</span>
        </div>
      )}

      {tab === "activity" ? (
        activity.length === 0 ? <div className="empty">No activity recorded yet.</div> :
        shown.length === 0 ? <div className="empty">No activity matches this filter. {hiddenAuto > 0 && "Untick “Hide automation” or "}Clear the filters to see all {grouped.length} entries.</div> : (
          <div style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>Time</th><th>User</th><th>Host</th><th>Action</th></tr></thead>
            <tbody>
              {shown.map((a, i) => (
                <tr key={a.id ?? i} style={{ cursor: "pointer" }}
                    onClick={() => { setDetail({ ...a, host: a.hosts.join(", ") }); setCopied(false); }}
                    title="Click to see the exact command">
                  <td className="faint mono">{fmtTime(a.timestamp)}</td>
                  <td>{a.username || "(unknown)"}</td>
                  <td title={a.hosts.join(", ")}>{summarizeHosts(a.hosts, hostInv)}</td>
                  <td>{a.description || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )
      ) : (
        <pre className="card mono" style={{ whiteSpace: "pre-wrap", maxHeight: "70vh", overflowY: "auto", fontSize: 12.5 }}>
          {log || "(empty)"}
        </pre>
      )}

      {detail && (
        <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) setDetail(null); }}>
          <div className="modal" style={{ maxWidth: 640 }}>
            <h3 style={{ textAlign: "left" }}>{detail.description || detail.action || "Activity"}</h3>
            <div className="muted" style={{ fontSize: 13, marginBottom: 10 }}>
              {fmtTime(detail.timestamp ?? detail.time ?? detail.created_at)}
              {" · "}{detail.username || detail.admin || "(unknown)"}
              {detail.host ? ` · ${detail.host}` : ""}
            </div>
            <div className="section-title" style={{ marginTop: 0 }}>Exact command</div>
            {detail.command
              ? <pre className="cmd-preview" style={{ whiteSpace: "pre-wrap", maxHeight: "40vh", overflowY: "auto" }}>{detail.command}</pre>
              : <div className="faint">No command recorded for this entry.</div>}
            <div className="spread" style={{ marginTop: 16 }}>
              <button className="btn ghost sm" disabled={!detail.command}
                      onClick={() => navigator.clipboard?.writeText(detail.command || "").then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
                {copied ? "Copied ✓" : "Copy command"}
              </button>
              <button className="btn sm" onClick={() => setDetail(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
