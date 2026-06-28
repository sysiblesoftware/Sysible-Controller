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

  function toCron() {
    const [hh, mm] = time.split(":").map(Number);
    if (freq === "At system boot") return "@reboot";
    if (freq === "Every N minutes") return `*/${nMin} * * * *`;
    if (freq === "Every N hours") return `${atMinute} */${nHours} * * *`;
    if (freq === "Daily") return `${mm} ${hh} * * *`;
    if (freq === "Weekly") return `${mm} ${hh} * * ${days.map((d) => CRON_DAY[d]).join(",")}`;
    if (freq === "Monthly") return `${mm} ${hh} ${monthDay} * *`;
    return "";
  }
  function toCalendar() {
    const [hh, mm] = time.split(":").map(Number);
    const hm = `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:00`;
    if (freq === "Every N minutes") return `*-*-* *:0/${nMin}:00`;
    if (freq === "Every N hours") return `*-*-* 0/${nHours}:${String(atMinute).padStart(2, "0")}:00`;
    if (freq === "Daily") return `*-*-* ${hm}`;
    if (freq === "Weekly") return `${days.join(",")} *-*-* ${hm}`;
    if (freq === "Monthly") return `*-*-${String(monthDay).padStart(2, "0")} ${hm}`;
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
