import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Ask one read-only question across the whole fleet and get a table back:
// which hosts have a package / service / user / file / open port, or their
// kernel version. Reuses the BFF's parallel read-dispatch; no host account or
// sudo needed for these reads. Auditor-blocked upstream (require_operator).
const C = { ok: "#4ec07a", faint: "#7a7a7a", warn: "#e0a83a" };

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

  async function run(e) {
    if (e) e.preventDefault();
    if (needsArg && !arg.trim()) { setErr(`Enter a ${(meta.arg || "value").toLowerCase()}.`); return; }
    setBusy(true); setErr(""); setRes(null);
    try { setRes(await api.fleetQuery(qtype, arg.trim(), [])); }
    catch (ex) { setErr(ex.message); }
    finally { setBusy(false); }
  }

  const groups = useMemo(() => {
    if (!res) return [];
    const g = {};
    for (const r of res.rows) (g[r.env] = g[r.env] || []).push(r);
    return Object.entries(g).sort(([a], [b]) => a.localeCompare(b));
  }, [res]);

  // "present" is a match; kernel is always present (show its value plainly).
  const renderVal = (r) => {
    if (r.value == null) return <span style={{ color: C.warn }} title={r.error || ""}>error</span>;
    if (qtype === "kernel") return <span>{r.value}</span>;
    if (!r.present) return <span style={{ color: C.faint }}>—</span>;
    return <span style={{ color: C.ok, fontWeight: 600 }}>{r.value === "yes" ? "✓" : r.value}</span>;
  };

  return (
    <div>
      <form className="row" onSubmit={run} style={{ gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 }}>
        <label className="field" style={{ minWidth: 200 }}>
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
            <strong>{(types[res.qtype] || {}).label}{res.arg ? `: ${res.arg}` : ""}</strong>
            <span className="faint" style={{ fontSize: 12 }}>
              {qtype === "kernel"
                ? `${res.count} host${res.count === 1 ? "" : "s"}`
                : `${res.matched} of ${res.count} host${res.count === 1 ? "" : "s"} match`}
            </span>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
            <colgroup><col /><col style={{ width: 220 }} /></colgroup>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-faint)", fontSize: 11 }}>
                <th style={{ padding: "6px 12px" }}>Host</th>
                <th style={{ padding: "6px 12px" }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(([env, list]) => (
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
        </div>
      )}

      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
        Read-only queries run across all managed hosts in parallel. Nothing is changed on the hosts.
      </p>
    </div>
  );
}
