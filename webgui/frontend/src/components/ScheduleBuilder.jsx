import React, { useEffect, useState } from "react";

// Human-friendly schedule builder mirroring the desktop's HumanScheduleBuilder:
// builds a 5-field cron string (mode="cron") or a systemd OnCalendar= string
// (mode="calendar") from friendly presets. Calls onChange(expression).
const CRON_DAY = { Sun: "0", Mon: "1", Tue: "2", Wed: "3", Thu: "4", Fri: "5", Sat: "6" };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function ScheduleBuilder({ mode = "cron", onChange }) {
  const [freq, setFreq] = useState("Daily");
  const [nMin, setNMin] = useState(15);
  const [nHours, setNHours] = useState(1);
  const [atMinute, setAtMinute] = useState(0);
  const [time, setTime] = useState("02:00");
  const [days, setDays] = useState(["Mon"]);
  const [monthDay, setMonthDay] = useState(1);
  const [advanced, setAdvanced] = useState(false);
  const [adv, setAdv] = useState("");

  const freqs = ["Every N minutes", "Every N hours", "Daily", "Weekly", "Monthly", ...(mode === "cron" ? ["At system boot"] : [])];

  // Clearing a number input makes +"" === 0 and typing junk makes it NaN; the
  // HTML min/max attrs don't constrain typed/cleared values. Without clamping,
  // "Every N minutes" could emit `*/0` or `*/NaN`, which installs into crontab
  // and then silently never fires. Clamp every numeric field to its valid range
  // at emit time so the builder can only ever produce a runnable expression.
  const clamp = (v, lo, hi, def) => {
    const n = Math.floor(Number(v));
    if (!Number.isFinite(n)) return def;
    return Math.min(hi, Math.max(lo, n));
  };
  function toCron() {
    const [hh, mm] = time.split(":").map(Number);
    const H = clamp(hh, 0, 23, 0), M = clamp(mm, 0, 59, 0);
    if (freq === "At system boot") return "@reboot";
    if (freq === "Every N minutes") return `*/${clamp(nMin, 1, 59, 15)} * * * *`;
    if (freq === "Every N hours") return `${clamp(atMinute, 0, 59, 0)} */${clamp(nHours, 1, 23, 1)} * * *`;
    if (freq === "Daily") return `${M} ${H} * * *`;
    if (freq === "Weekly") return `${M} ${H} * * ${(days.length ? days : ["Mon"]).map((d) => CRON_DAY[d]).join(",")}`;
    if (freq === "Monthly") return `${M} ${H} ${clamp(monthDay, 1, 31, 1)} * *`;
    return "";
  }
  function toCalendar() {
    const [hh, mm] = time.split(":").map(Number);
    const H = clamp(hh, 0, 23, 0), M = clamp(mm, 0, 59, 0);
    const hm = `${String(H).padStart(2, "0")}:${String(M).padStart(2, "0")}:00`;
    if (freq === "Every N minutes") return `*-*-* *:0/${clamp(nMin, 1, 59, 15)}:00`;
    if (freq === "Every N hours") return `*-*-* 0/${clamp(nHours, 1, 23, 1)}:${String(clamp(atMinute, 0, 59, 0)).padStart(2, "0")}:00`;
    if (freq === "Daily") return `*-*-* ${hm}`;
    if (freq === "Weekly") return `${(days.length ? days : ["Mon"]).join(",")} *-*-* ${hm}`;
    if (freq === "Monthly") return `*-*-${String(clamp(monthDay, 1, 31, 1)).padStart(2, "0")} ${hm}`;
    return "";
  }
  const expr = advanced ? adv.trim() : (mode === "cron" ? toCron() : toCalendar());
  useEffect(() => { onChange && onChange(expr); }, [expr]); // eslint-disable-line

  const toggleDay = (d) => setDays((s) => s.includes(d) ? s.filter((x) => x !== d) : [...s, d]);

  return (
    <div className="card" style={{ padding: 12, background: "var(--panel-2)" }}>
      {!advanced && (
        <>
          <label className="field" style={{ marginTop: 0 }}><span>Frequency</span>
            <select value={freq} onChange={(e) => setFreq(e.target.value)}>
              {freqs.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </label>
          {freq === "Every N minutes" && (
            <label className="field"><span>Every</span>
              <input type="number" min="1" max="59" value={nMin} onChange={(e) => setNMin(+e.target.value)} /></label>
          )}
          {freq === "Every N hours" && (
            <div className="row" style={{ gap: 8 }}>
              <label className="field"><span>Every (hours)</span><input type="number" min="1" max="23" value={nHours} onChange={(e) => setNHours(+e.target.value)} /></label>
              <label className="field"><span>at minute</span><input type="number" min="0" max="59" value={atMinute} onChange={(e) => setAtMinute(+e.target.value)} /></label>
            </div>
          )}
          {(freq === "Daily" || freq === "Weekly" || freq === "Monthly") && (
            <label className="field"><span>Time</span><input type="time" value={time} onChange={(e) => setTime(e.target.value)} /></label>
          )}
          {freq === "Weekly" && (
            <div style={{ marginTop: 8 }}>
              <span className="faint">Days:</span>
              <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                {DAYS.map((d) => (
                  <label key={d} className="target-chip"><input type="checkbox" checked={days.includes(d)} onChange={() => toggleDay(d)} />{d}</label>
                ))}
              </div>
            </div>
          )}
          {freq === "Monthly" && (
            <label className="field"><span>Day of month</span><input type="number" min="1" max="31" value={monthDay} onChange={(e) => setMonthDay(+e.target.value)} /></label>
          )}
        </>
      )}
      {advanced && (
        <label className="field" style={{ marginTop: 0 }}><span>{mode === "cron" ? "Cron expression" : "OnCalendar expression"}</span>
          <input value={adv} onChange={(e) => setAdv(e.target.value)}
                 placeholder={mode === "cron" ? "e.g. */15 * * * * or @daily" : "e.g. *-*-* 02:00:00"} /></label>
      )}
      <div className="checkrow"><input id={`adv_${mode}`} type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /><label htmlFor={`adv_${mode}`}>Advanced (type the expression)</label></div>
      <div className="cmd-preview" style={{ marginTop: 8 }}>{expr || "(empty)"}</div>
    </div>
  );
}
