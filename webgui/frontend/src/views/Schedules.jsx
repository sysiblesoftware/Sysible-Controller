import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Recurring fleet maintenance: patch scans, security/all updates, posture scans,
// reboots — set once, run unattended (dispatched as root on agent hosts). The
// runner lives on the controller; this is the CRUD + run-now UI.
export default function Schedules() {
  const [jobs, setJobs] = useState([]);
  const [meta, setMeta] = useState({ actions: {}, cadences: [] });
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [showNew, setShowNew] = useState(false);

  const load = useCallback(() => {
    api.schedules().then((d) => { setJobs(d.schedules || []); setMeta({ actions: d.actions || {}, cadences: d.cadences || [], arg_actions: d.arg_actions || {} }); })
      .catch((e) => setErr(e.message));
    api.hosts().then((d) => setHosts(d.hosts || [])).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  async function runNow(id) {
    setBusy(id); setErr(""); setMsg("");
    try { const r = await api.scheduleRunNow(id); setMsg(`Ran: ${r.status} — ${r.detail}`); load(); }
    catch (e) { setErr(e.message); } finally { setBusy(""); }
  }
  async function toggle(j) {
    try { await api.scheduleUpdate(j.id, { ...j, enabled: !j.enabled }); load(); }
    catch (e) { setErr(e.message); }
  }
  async function del(id) {
    if (!window.confirm("Delete this schedule?")) return;
    try { await api.scheduleDelete(id); load(); } catch (e) { setErr(e.message); }
  }

  const hostName = useMemo(() => Object.fromEntries(hosts.map((h) => [h.id, h.label])), [hosts]);
  const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";
  const cadenceText = (j) => j.cadence === "hourly" ? `hourly at :${j.at.split(":")[1]}`
    : j.cadence === "weekly" ? `${WEEKDAYS[j.weekday] || "Mon"} ${j.at}` : `daily ${j.at}`;
  const targetsText = (j) => !j.targets || j.targets.length === 0 ? "all hosts"
    : j.targets.map((id) => hostName[id] || id).join(", ");

  return (
    <div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      <div className="spread" style={{ marginBottom: 12 }}>
        <span className="faint">{jobs.length} scheduled job{jobs.length === 1 ? "" : "s"}</span>
        <button className="btn sm" onClick={() => setShowNew((v) => !v)}>{showNew ? "Cancel" : "New schedule"}</button>
      </div>

      {showNew && <NewSchedule meta={meta} hosts={hosts}
                    onDone={() => { setShowNew(false); load(); }} onErr={setErr} />}

      {jobs.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>No schedules yet. Create one for unattended patching, scans, or reboots.</div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr style={{ textAlign: "left", color: "var(--text-faint)", fontSize: 11 }}>
              {["Name", "Action", "When", "Targets", "Next run", "Last run", ""].map((h) => (
                <th key={h} style={{ padding: "7px 10px" }}>{h}</th>))}
            </tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} style={{ borderTop: "1px solid var(--border)", opacity: j.enabled ? 1 : 0.5 }}>
                  <td style={{ padding: "7px 10px", fontWeight: 600 }}>{j.name}</td>
                  <td style={{ padding: "7px 10px" }}>{meta.actions[j.action] || j.action}
                    {j.arg ? <span className="faint" style={{ fontSize: 12 }}> · {j.arg}</span> : null}</td>
                  <td style={{ padding: "7px 10px" }}>{cadenceText(j)}</td>
                  <td style={{ padding: "7px 10px", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={targetsText(j)}>{targetsText(j)}</td>
                  <td style={{ padding: "7px 10px" }} className="faint">{j.enabled ? fmt(j.next_run) : "paused"}</td>
                  <td style={{ padding: "7px 10px" }}>
                    {j.last_run ? <span style={{ color: j.last_status === "ok" ? "#4ec07a" : "#e06c6c" }}
                                        title={j.last_detail}>{j.last_status} · {fmt(j.last_run)}</span>
                               : <span className="faint">never</span>}
                  </td>
                  <td style={{ padding: "7px 10px", whiteSpace: "nowrap" }}>
                    <button className="btn ghost sm" disabled={busy === j.id} onClick={() => runNow(j.id)}>
                      {busy === j.id ? <span className="spin" /> : "Run now"}</button>{" "}
                    <button className="btn ghost sm" onClick={() => toggle(j)}>{j.enabled ? "Pause" : "Resume"}</button>{" "}
                    <button className="btn ghost sm danger" onClick={() => del(j.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
        Scheduled jobs run unattended on the controller and dispatch as root on agent hosts
        (no operator sudo password needed). Times are the controller's local time.
      </p>
    </div>
  );
}

function NewSchedule({ meta, hosts, onDone, onErr }) {
  const actionKeys = Object.keys(meta.actions);
  const argActions = meta.arg_actions || {};
  const [f, setF] = useState({ name: "", action: actionKeys[0] || "patch_scan",
    arg: "", cadence: "daily", at: "02:00", weekday: 0 });
  const [targets, setTargets] = useState([]);
  const [saving, setSaving] = useState(false);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const argLabel = argActions[f.action];

  async function save() {
    setSaving(true); onErr("");
    try {
      await api.scheduleCreate({ name: f.name, action: f.action, arg: f.arg, targets, cadence: f.cadence,
        at: f.at, weekday: Number(f.weekday), enabled: true });
      onDone();
    } catch (e) { onErr(e.message); } finally { setSaving(false); }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <strong>New schedule</strong>
      <div className="row" style={{ gap: 12, flexWrap: "wrap", marginTop: 10, alignItems: "flex-end" }}>
        <label className="field" style={{ minWidth: 200 }}><span>Name (optional)</span>
          <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Weekly Prod security patch" /></label>
        <label className="field"><span>Action</span>
          <select value={f.action} onChange={(e) => set("action", e.target.value)}>
            {actionKeys.map((k) => <option key={k} value={k}>{meta.actions[k]}</option>)}
          </select></label>
        {argLabel && (
          <label className="field" style={{ minWidth: 200 }}><span>{argLabel}</span>
            <input value={f.arg} onChange={(e) => set("arg", e.target.value)}
                   placeholder={f.action === "run_command" ? "e.g. dnf -y autoremove" : "e.g. nginx"} /></label>
        )}
        <label className="field"><span>Cadence</span>
          <select value={f.cadence} onChange={(e) => set("cadence", e.target.value)}>
            {(meta.cadences || []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select></label>
        {f.cadence === "weekly" && (
          <label className="field"><span>Weekday</span>
            <select value={f.weekday} onChange={(e) => set("weekday", e.target.value)}>
              {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select></label>
        )}
        <label className="field"><span>{f.cadence === "hourly" ? "Minute (:MM)" : "Time (HH:MM)"}</span>
          <input type="time" value={f.at} onChange={(e) => set("at", e.target.value)} /></label>
      </div>
      <div style={{ marginTop: 10 }}>
        <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>
          Target hosts (none checked = all hosts):
        </div>
        <HostTree hosts={hosts} value={targets} onChange={setTargets} />
      </div>
      <button className="btn" style={{ marginTop: 12 }} disabled={saving} onClick={save}>
        {saving ? <span className="spin" /> : "Create schedule"}</button>
    </div>
  );
}
