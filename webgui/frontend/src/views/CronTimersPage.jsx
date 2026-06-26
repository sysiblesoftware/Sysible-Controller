import React, { useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ScheduleBuilder from "../components/ScheduleBuilder.jsx";
import ResultsPane from "../components/ResultsPane.jsx";

// Bespoke Cron & Systemd Timers page: friendly schedule builder for cron jobs
// and systemd OnCalendar timers, plus the timer control actions.
export default function CronTimersPage({ hosts = [], onRefreshHosts }) {
  const [targets, setTargets] = useState([]);
  const [busy, setBusy] = useState(""); const [err, setErr] = useState(""); const [results, setResults] = useState([]);
  // cron
  const [cronSchedule, setCronSchedule] = useState("");
  const [cronCommand, setCronCommand] = useState(""); const [cronComment, setCronComment] = useState("");
  const [cronMatch, setCronMatch] = useState("");
  // timers
  const [timerName, setTimerName] = useState("");
  const [onCalendar, setOnCalendar] = useState("");
  const [ts, setTs] = useState({ name: "", exec_start: "", description: "", run_as_user: "root", enable_now: true });
  const [showCreate, setShowCreate] = useState(false);
  const [expanded, setExpanded] = useState(false);

  async function run(action, params, label) {
    if (targets.length === 0) { setErr("Check one or more hosts first."); return; }
    setBusy(action); setErr("");
    try { const r = await api.runTool(action, targets, params); setResults((p) => [{ label, ...r, at: Date.now() }, ...p]); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div className="tool-flex">
      {!expanded && <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check one or more hosts, then run an action." />}

      {!expanded && (
      <div className="tool-actions-col"><div className="tool-actions-scroll">
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Cron Jobs</legend>
          <div className="group-buttons" style={{ marginBottom: 10 }}>
            <button className="btn sm" disabled={busy} onClick={() => run("cron_list", {}, "List cron jobs")}>List Cron Jobs</button>
          </div>
          <span className="faint">Schedule</span>
          <ScheduleBuilder mode="cron" onChange={setCronSchedule} />
          <label className="field"><span>Command</span><input value={cronCommand} onChange={(e) => setCronCommand(e.target.value)} placeholder="/usr/local/bin/backup.sh" /></label>
          <label className="field"><span>Comment (optional)</span><input value={cronComment} onChange={(e) => setCronComment(e.target.value)} /></label>
          <button className="btn sm" style={{ marginTop: 10 }} disabled={busy || !cronSchedule || !cronCommand}
                  onClick={() => run("cron_add", { schedule: cronSchedule, command: cronCommand, comment: cronComment }, "Add cron job")}>Add Cron Job</button>
          <div className="row" style={{ marginTop: 12, gap: 8 }}>
            <input style={{ flex: 1 }} value={cronMatch} onChange={(e) => setCronMatch(e.target.value)} placeholder="Text to match the job to remove" />
            <button className="btn sm danger" disabled={busy || !cronMatch} onClick={() => run("cron_remove", { match_text: cronMatch }, "Remove cron job")}>Remove Cron Job</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Systemd Timers</legend>
          <label className="field" style={{ marginTop: 0 }}><span>Timer name (for control actions)</span>
            <input value={timerName} onChange={(e) => setTimerName(e.target.value)} placeholder="e.g. backup.timer" /></label>
          <div className="group-buttons">
            <button className="btn sm" disabled={busy} onClick={() => run("timer_list", {}, "List timers")}>List All Timers</button>
            <button className="btn sm" disabled={busy || !timerName} onClick={() => run("timer_status", { name: timerName }, `Status ${timerName}`)}>Check Status</button>
            <button className="btn sm" disabled={busy || !timerName} onClick={() => run("timer_start", { name: timerName }, `Start ${timerName}`)}>Start</button>
            <button className="btn sm danger" disabled={busy || !timerName} onClick={() => run("timer_stop", { name: timerName }, `Stop ${timerName}`)}>Stop</button>
            <button className="btn sm" disabled={busy || !timerName} onClick={() => run("timer_enable", { name: timerName }, `Enable ${timerName}`)}>Enable At Boot</button>
            <button className="btn sm" disabled={busy || !timerName} onClick={() => run("timer_disable", { name: timerName }, `Disable ${timerName}`)}>Disable At Boot</button>
            <button className="btn sm danger" disabled={busy || !timerName} onClick={() => run("timer_delete", { name: timerName, delete_service: true }, `Delete ${timerName}`)}>Delete Timer</button>
          </div>

          <button className="btn ghost sm" style={{ marginTop: 12 }} onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? "▾" : "▸"} Create Systemd Timer
          </button>
          {showCreate && (
            <div style={{ marginTop: 8 }}>
              <label className="field"><span>Unit name</span><input value={ts.name} onChange={(e) => setTs({ ...ts, name: e.target.value })} placeholder="backup" /></label>
              <label className="field"><span>ExecStart</span><input value={ts.exec_start} onChange={(e) => setTs({ ...ts, exec_start: e.target.value })} placeholder="/usr/local/bin/backup.sh" /></label>
              <label className="field"><span>Description</span><input value={ts.description} onChange={(e) => setTs({ ...ts, description: e.target.value })} /></label>
              <label className="field"><span>Run as user</span><input value={ts.run_as_user} onChange={(e) => setTs({ ...ts, run_as_user: e.target.value })} /></label>
              <span className="faint">Schedule (OnCalendar)</span>
              <ScheduleBuilder mode="calendar" onChange={setOnCalendar} />
              <div className="checkrow"><input id="ten" type="checkbox" checked={ts.enable_now} onChange={(e) => setTs({ ...ts, enable_now: e.target.checked })} /><label htmlFor="ten">Enable now</label></div>
              <button className="btn" style={{ marginTop: 12 }} disabled={busy || !ts.name || !ts.exec_start || !onCalendar}
                      onClick={() => run("timer_create", { ...ts, on_calendar: onCalendar }, `Create timer ${ts.name}`)}>Create Timer</button>
            </div>
          )}
        </fieldset>
        {err && <div className="error-box">{err}</div>}
      </div></div>
      )}

      <ResultsPane results={results} setResults={setResults} expanded={expanded}
                   onToggleExpand={() => setExpanded((v) => !v)}
                   empty="Run an action — output appears here." />
    </div>
  );
}
