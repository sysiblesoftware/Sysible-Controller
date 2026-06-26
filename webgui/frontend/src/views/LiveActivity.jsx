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

export default function LiveActivity() {
  const [tab, setTab] = useState("activity");
  const [activity, setActivity] = useState([]);
  const [log, setLog] = useState("");
  const [err, setErr] = useState("");
  const [auto, setAuto] = useState(true);
  const [detail, setDetail] = useState(null);
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);

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

  return (
    <div>
      <div className="tabs" style={{ marginBottom: 14 }}>
        <button className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")}>Activity Feed</button>
        <button className={tab === "log" ? "active" : ""} onClick={() => setTab("log")}>Controller Log</button>
        <div style={{ flex: 1 }} />
        <label className="checkrow" style={{ margin: 0 }}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          <span className="faint">Auto-refresh</span>
        </label>
        <button className="btn ghost sm" onClick={load}>Refresh</button>
      </div>

      {err && <div className="error-box">{err}</div>}

      {tab === "activity" ? (
        activity.length === 0 ? <div className="empty">No activity recorded yet.</div> : (
          <table>
            <thead><tr><th>Time</th><th>User</th><th>Host</th><th>Action</th></tr></thead>
            <tbody>
              {activity.map((a, i) => (
                <tr key={a.id ?? i} style={{ cursor: "pointer" }} onClick={() => { setDetail(a); setCopied(false); }}
                    title="Click to see the exact command">
                  <td className="faint mono">{fmtTime(a.timestamp ?? a.time ?? a.created_at)}</td>
                  <td>{a.username || a.admin || a.actor || a.user || ""}</td>
                  <td>{a.host || a.host_label || ""}</td>
                  <td>{a.description || a.action || a.summary || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : (
        <pre className="card mono" style={{ whiteSpace: "pre-wrap", maxHeight: "70vh", overflowY: "auto", fontSize: 12.5 }}>
          {log || "（empty）"}
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
