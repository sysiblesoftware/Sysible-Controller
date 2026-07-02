import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Ask one read-only question across the whole fleet and get a table back:
// which hosts have a package / service / user / file / open port, journal
// matches, kernel version, or — for config drift — which hosts differ on a
// file's content. Reuses the BFF's parallel read-dispatch; no host account or
// sudo needed for these reads. Auditor-blocked upstream (require_operator).
const C = { ok: "#4ec07a", faint: "#7a7a7a", warn: "#e0a83a", crit: "#e06c6c" };

export default function FleetQuery() {
  const [types, setTypes] = useState({});
  const [qtype, setQtype] = useState("package");
  const [arg, setArg] = useState("");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => { api.fleetQueryTypes().then((d) => setTypes(d.types || {})).catch(() => {}); }, []);
  const meta = types[qtype] || {};
  const needsArg = qtype !== "kernel";
  const resMeta = res ? (types[res.qtype] || {}) : {};

  async function run(e) {
    if (e) e.preventDefault();
    if (needsArg && !arg.trim()) { setErr(`Enter a ${(meta.arg || "value").toLowerCase()}.`); return; }
    setBusy(true); setErr(""); setRes(null);
    try { setRes(await api.fleetQuery(qtype, arg.trim(), [])); }
    catch (ex) { setErr(ex.message); }
    finally { setBusy(false); }
  }

  // Env-grouped rows (default). Drift types render by value instead (below).
  const envGroups = useMemo(() => {
    if (!res) return [];
    const g = {};
    for (const r of res.rows) (g[r.env] = g[r.env] || []).push(r);
    return Object.entries(g).sort(([a], [b]) => a.localeCompare(b));
  }, [res]);

  // Drift: cluster hosts by identical value; largest cluster is the "majority",
  // the rest are drifted.
  const clusters = useMemo(() => {
    if (!res || resMeta.group !== "value") return [];
    const byVal = {};
    for (const r of res.rows) { const k = r.value == null ? "(error)" : r.value; (byVal[k] = byVal[k] || []).push(r); }
    return Object.entries(byVal).sort((a, b) => b[1].length - a[1].length);
  }, [res, resMeta.group]);

  const renderVal = (r) => {
    if (r.value == null) return <span style={{ color: C.warn }} title={r.error || ""}>error</span>;
    if (res.qtype === "kernel") return <span>{r.value}</span>;
    if (res.qtype === "service") return <span style={{ color: r.value === "active" ? C.ok : C.faint, fontWeight: r.value === "active" ? 600 : 400 }}>{r.value}</span>;
    if (res.qtype === "journal") return <span style={{ color: r.value !== "0" ? C.warn : C.faint, fontWeight: r.value !== "0" ? 700 : 400 }}>{r.value}</span>;
    if (!r.present) return <span style={{ color: C.faint }}>—</span>;
    return <span style={{ color: C.ok, fontWeight: 600 }}>{r.value === "yes" ? "✓" : r.value}</span>;
  };

  return (
    <div>
      <form className="row" onSubmit={run} style={{ gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 }}>
        <label className="field" style={{ minWidth: 220 }}>
          <span>Question</span>
          <select value={qtype} onChange={(e) => { setQtype(e.target.value); setRes(null); setArg(""); }}>
            {Object.entries(types).map(([k, t]) => <option key={k} value={k}>{t.label}</option>)}
          </select>
        </label>
        {needsArg && (
          <label className="field" style={{ minWidth: 260 }}>
            <span>{meta.arg || "Value"}</span>
            <input value={arg} onChange={(e) => setArg(e.target.value)}
                   placeholder={meta.example ? `e.g. ${meta.example}` : ""} />
          </label>
        )}
        <button className="btn" type="submit" disabled={busy}>
          {busy ? <span className="spin" /> : "Query fleet"}</button>
      </form>

      {err && <div className="error-box">{err}</div>}

      {res && (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <div className="spread" style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)" }}>
            <strong>{resMeta.label}{res.arg ? `: ${res.arg}` : ""}</strong>
            <span className="faint" style={{ fontSize: 12 }}>
              {resMeta.group === "value"
                ? (clusters.length > 1
                    ? <span style={{ color: C.warn, fontWeight: 700 }}>⚠ drift — {clusters.length} distinct across {res.count} host{res.count === 1 ? "" : "s"}</span>
                    : <span style={{ color: C.ok }}>all {res.count} host{res.count === 1 ? "" : "s"} identical</span>)
                : resMeta.summary === "count"
                  ? `${res.count} host${res.count === 1 ? "" : "s"}`
                  : `${res.matched} of ${res.count} host${res.count === 1 ? "" : "s"} match`}
            </span>
          </div>

          {resMeta.group === "value" ? (
            // Drift view: one block per distinct value; non-majority = drifted.
            <div style={{ display: "grid", gap: 8, padding: 10 }}>
              {clusters.map(([val, hosts], i) => {
                const drifted = i > 0;  // largest cluster first = the baseline
                const col = val === "missing" || val === "unreadable" || val === "(error)" ? C.crit : (drifted ? C.warn : C.faint);
                return (
                  <div key={val} style={{ border: `1px solid ${drifted ? C.warn : "var(--border)"}`, borderRadius: 6, padding: "6px 10px" }}>
                    <div className="spread" style={{ marginBottom: 4 }}>
                      <span style={{ fontFamily: "ui-monospace, Menlo, monospace", color: col }}>
                        {val}{drifted ? " · drifted" : (clusters.length > 1 ? " · baseline" : "")}
                      </span>
                      <span className="faint" style={{ fontSize: 12 }}>{hosts.length} host{hosts.length === 1 ? "" : "s"}</span>
                    </div>
                    <div className="faint" style={{ fontSize: 12 }}>{hosts.map((h) => h.host).join(", ")}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
              <colgroup><col /><col style={{ width: 220 }} /></colgroup>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-faint)", fontSize: 11 }}>
                  <th style={{ padding: "6px 12px" }}>Host</th>
                  <th style={{ padding: "6px 12px" }}>Result</th>
                </tr>
              </thead>
              <tbody>
                {envGroups.map(([env, list]) => (
                  <React.Fragment key={env}>
                    <tr><td colSpan={2} style={{ padding: "7px 12px", borderTop: "1px solid var(--border)",
                        fontWeight: 700, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4,
                        color: "var(--text-faint)" }}>{env}</td></tr>
                    {list.map((r) => (
                      <tr key={r.host} style={{ borderTop: "1px solid var(--border)" }}>
                        <td style={{ padding: "5px 12px" }}>{r.host}</td>
                        <td style={{ padding: "5px 12px" }}>{renderVal(r)}</td>
                      </tr>
                    ))}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
        Read-only queries run across all managed hosts in parallel. Nothing is changed on the hosts.
        “Journal matches” counts lines in the last 2000 journal entries; “File content — drift” groups
        hosts by a file’s content hash so odd-one-out configs stand out.
      </p>
    </div>
  );
}
