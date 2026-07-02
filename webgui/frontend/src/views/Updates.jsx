import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostResults from "../components/HostResults.jsx";

const C = { sec: "#e06c6c", upd: "#e0a83a", ok: "#4ec07a", reboot: "#e0a83a", faint: "#7a7a7a" };

// Fleet patch status: pending updates, security updates, and reboot-required per
// host — with one-click "install security" / "install all" / "reboot" across the
// selected hosts. Read-only for auditors (the install/reboot actions 403).
export default function Updates({ role }) {
  const canAct = role !== "auditor";
  const [data, setData] = useState({ hosts: [], ts: 0 });
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [checked, setChecked] = useState([]);   // host ids
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState("");

  const [liveLoading, setLiveLoading] = useState(false);
  const load = useCallback((refresh = 0, live = 0) => {
    (live ? setLiveLoading : setLoading)(true); setErr("");
    api.fleetUpdates(refresh, live)
      .then((d) => setData({ hosts: d.hosts || [], ts: (d.ts ? d.ts * 1000 : Date.now()) }))
      .catch((e) => setErr(e.message))
      .finally(() => { setLoading(false); setLiveLoading(false); });
  }, []);
  useEffect(() => { load(0); }, [load]);

  const hosts = data.hosts;
  const summary = useMemo(() => {
    let withUpd = 0, sec = 0, reboot = 0, offline = 0;
    for (const h of hosts) {
      if (h.online === false) { offline++; continue; }
      if ((h.total || 0) > 0) withUpd++;
      sec += h.security || 0;
      if (h.reboot) reboot++;
    }
    return { withUpd, sec, reboot, offline, total: hosts.length };
  }, [hosts]);

  const groups = useMemo(() => {
    const g = {};
    for (const h of hosts) (g[h.environment || "Unassigned"] = g[h.environment || "Unassigned"] || []).push(h);
    return Object.entries(g).sort(([a], [b]) => (a === "Unassigned" ? 1 : b === "Unassigned" ? -1 : a.localeCompare(b)));
  }, [hosts]);

  const actionable = hosts.filter((h) => h.online !== false && (h.total || 0) > 0).map((h) => h.id);
  const toggle = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  const [installing, setInstalling] = useState("");   // message while a background install runs

  async function run(kind) {
    if (!checked.length) { setErr("Select one or more hosts first."); return; }
    if (kind === "reboot") {
      if (!window.confirm("Reboot the selected hosts now?")) return;
      setBusy("reboot"); setErr(""); setResults(null);
      try { const r = await api.fleet("reboot", checked); setResults({ label: "Reboot host", rows: r.results || [] }); load(1); }
      catch (e) { setErr(e.message); } finally { setBusy(""); }
      return;
    }
    // Installs run in the BACKGROUND (a package upgrade can take minutes — a
    // synchronous call would just spin). Kick it, then poll the counts.
    const label = kind === "security" ? "security updates" : "all updates";
    if (!window.confirm(`Install ${label} on ${checked.length} host(s)? This runs in the background; counts refresh as it completes.`)) return;
    setBusy(kind); setErr(""); setResults(null); setInstalling("");
    try {
      const r = await api.fleetInstall(checked, kind);
      setInstalling(`Installing ${label} on ${r.hosts || checked.length} host(s) in the background… counts update as hosts finish.`);
      // Poll for a few minutes so the table reflects the drop without a manual rescan.
      let n = 0;
      const t = setInterval(() => { n += 1; load(1); if (n >= 30) { clearInterval(t); setInstalling(""); } }, 15000);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  const Cell = ({ n, color }) => (
    <span style={{ fontWeight: n > 0 ? 700 : 400, color: n > 0 ? color : C.faint }}>{n == null ? "—" : n}</span>
  );

  return (
    <div>
      <div className="spread" style={{ marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "baseline" }}>
          <span><b style={{ fontSize: 18 }}>{summary.withUpd}</b> <span className="faint">host{summary.withUpd === 1 ? "" : "s"} with updates</span></span>
          <span><b style={{ fontSize: 18, color: summary.sec ? C.sec : undefined }}>{summary.sec}</b> <span className="faint">security</span></span>
          <span><b style={{ fontSize: 18, color: summary.reboot ? C.reboot : undefined }}>{summary.reboot}</b> <span className="faint">need reboot</span></span>
          {summary.offline > 0 && <span className="faint">{summary.offline} offline</span>}
        </div>
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          {data.ts > 0 && <span className="faint" style={{ fontSize: 12 }}>scanned {new Date(data.ts).toLocaleTimeString()}</span>}
          <button className="btn ghost sm" onClick={() => load(1)} disabled={loading || liveLoading}
                  title="Recount using each host's cached repo metadata (fast)">
            {loading ? <span className="spin" /> : "Rescan"}</button>
          <button className="btn ghost sm" onClick={() => load(1, 1)} disabled={loading || liveLoading}
                  title="Refresh each host's repo metadata first, then count (live, slower)">
            {liveLoading ? <span className="spin" /> : "Refresh metadata & rescan"}</button>
        </div>
      </div>

      {err && <div className="error-box">{err}</div>}
      {installing && <div className="ok-text" style={{ marginBottom: 10 }}>⏳ {installing}</div>}

      {canAct && (
        <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn ghost sm" onClick={() => setChecked(actionable)}>Select all with updates</button>
          <button className="btn ghost sm" onClick={() => setChecked([])}>Clear</button>
          <span className="faint" style={{ fontSize: 12 }}>{checked.length} selected</span>
          <div style={{ flex: 1 }} />
          <button className="btn sm" disabled={!checked.length || busy} onClick={() => run("security")}>
            {busy === "security" ? <span className="spin" /> : "Install security updates"}</button>
          <button className="btn sm" disabled={!checked.length || busy} onClick={() => run("all")}>
            {busy === "all" ? <span className="spin" /> : "Install all updates"}</button>
          <button className="btn sm danger" disabled={!checked.length || busy} onClick={() => run("reboot")}>
            {busy === "reboot" ? <span className="spin" /> : "Reboot"}</button>
        </div>
      )}

      {hosts.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>
          {loading ? "Scanning the fleet for pending updates…" : "No hosts, or no scan yet. Click Rescan."}
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-faint)", fontSize: 11 }}>
                {canAct && <th style={{ padding: "6px 8px", width: 28 }}></th>}
                <th style={{ padding: "6px 8px" }}>Host</th>
                <th style={{ padding: "6px 8px", textAlign: "right" }}>Pending</th>
                <th style={{ padding: "6px 8px", textAlign: "right" }}>Security</th>
                <th style={{ padding: "6px 8px", textAlign: "center" }}>Reboot</th>
                <th style={{ padding: "6px 8px" }}>Mgr</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(([env, list]) => (
                <React.Fragment key={env}>
                  <tr><td colSpan={canAct ? 6 : 5} style={{ padding: "7px 8px", borderTop: "1px solid var(--border)",
                      fontWeight: 700, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: "var(--text-faint)" }}>{env}</td></tr>
                  {list.map((h) => {
                    const off = h.online === false;
                    return (
                      <tr key={h.id} style={{ borderTop: "1px solid var(--border)", opacity: off ? 0.55 : 1 }}>
                        {canAct && <td style={{ padding: "5px 8px" }}>
                          <input type="checkbox" disabled={off || !(h.total > 0)} checked={checked.includes(h.id)}
                                 onChange={() => toggle(h.id)} /></td>}
                        <td style={{ padding: "5px 8px" }}>{h.host}
                          {off && <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>offline</span>}
                          {h.error && !off && <span className="faint" style={{ marginLeft: 6, fontSize: 11 }} title={h.error}>· {h.error}</span>}
                        </td>
                        <td style={{ padding: "5px 8px", textAlign: "right" }}><Cell n={h.total} color={C.upd} /></td>
                        <td style={{ padding: "5px 8px", textAlign: "right" }}><Cell n={h.security} color={C.sec} /></td>
                        <td style={{ padding: "5px 8px", textAlign: "center" }}>{h.reboot ? <span style={{ color: C.reboot, fontWeight: 700 }}>yes</span> : <span className="faint">—</span>}</td>
                        <td style={{ padding: "5px 8px", color: "var(--text-faint)" }}>{h.mgr || "—"}</td>
                      </tr>
                    );
                  })}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {results && (
        <div className="card" style={{ marginTop: 14 }}>
          <strong>{results.label}</strong>
          <HostResults rows={results.rows} />
        </div>
      )}

      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
        "Rescan" recounts from each host's cached repo metadata (fast). "Refresh metadata &amp; rescan"
        forces a repo refresh first for live counts (slower, hits mirrors). Security counts are best-effort
        per package manager.
      </p>
    </div>
  );
}
