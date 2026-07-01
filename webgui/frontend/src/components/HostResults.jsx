import React, { useState } from "react";

// Shared per-host results renderer used by every multi-host result panel
// (ToolPage, ResultsPane, UserGroupPage, Connect). Turns a list of per-host
// results into a scannable summary + collapsible rows so a fleet-wide action
// across 25+ hosts isn't a wall of text:
//   - a summary bar: "N hosts · X ok · Y need attention", an "Only problems"
//     toggle, and a host/output filter;
//   - one collapsible row per host (status dot + host + exit + a one-line
//     summary), problems sorted to the top, OK hosts collapsed by default and
//     problem hosts expanded.
// `rows` items are { host, ok, code, stdout, stderr, error }.

// One-line summary: the first non-empty output line (e.g. "HEALTH: OK").
function firstLine(r) {
  const t = (r.stdout || "").trim() || (r.stderr || "").trim() || (r.error || "").trim();
  if (!t) return r.ok ? "" : "failed";
  const line = t.split("\n")[0];
  return line.length > 140 ? line.slice(0, 140) + "…" : line;
}

function HostRow({ r, single }) {
  const body = (r.stdout || "") + (r.stderr || "");
  const hasDetail = body.trim().length > 0 || !!r.error;
  const [open, setOpen] = useState(single || !r.ok); // OK collapsed, problems open
  const summary = firstLine(r);
  return (
    <div style={{ borderTop: "1px solid var(--border)" }}>
      <div className="rh" style={{ cursor: hasDetail ? "pointer" : "default", gap: 6 }}
           onClick={() => hasDetail && setOpen((o) => !o)}>
        <span className="faint" style={{ width: 10, display: "inline-block" }}>{hasDetail ? (open ? "▾" : "▸") : ""}</span>
        <span className={"dot " + (r.ok ? "ok" : "bad")} />
        <span>{r.host}</span>
        {r.code != null && <span className="faint">exit {r.code}</span>}
        {summary && <span className="faint" title={summary}
          style={{ marginLeft: 6, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{summary}</span>}
      </div>
      {open && r.error && <div className="error-box" style={{ margin: "6px 0 0" }}>{r.error}</div>}
      {open && body && <pre>{body}</pre>}
    </div>
  );
}

const envOf = (r) => r.environment || "Unassigned";

// Small env header with its own ok/problem tally.
function EnvHeader({ env, rows }) {
  const ok = rows.filter((r) => r.ok).length;
  const prob = rows.length - ok;
  return (
    <div className="spread" style={{ padding: "7px 4px 5px", borderTop: "1px solid var(--border)", alignItems: "center" }}>
      <span style={{ fontWeight: 700, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.4 }}>{env}</span>
      <span className="faint" style={{ fontSize: 12 }}>
        {rows.length} host{rows.length === 1 ? "" : "s"} · <span className="ok-text">{ok} ok</span>
        {prob > 0 && <> · <span className="badge amber">{prob}</span></>}
      </span>
    </div>
  );
}

export default function HostResults({ rows }) {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [filter, setFilter] = useState("");
  const list = rows || [];
  const okCount = list.filter((r) => r.ok).length;
  const probCount = list.length - okCount;
  const q = filter.trim().toLowerCase();
  const shown = list
    .filter((r) => !onlyProblems || !r.ok)
    .filter((r) => !q ||
      `${r.host} ${envOf(r)} ${r.stdout || ""} ${r.stderr || ""} ${r.error || ""}`.toLowerCase().includes(q))
    .slice().sort((a, b) => (a.ok === b.ok ? 0 : a.ok ? 1 : -1)); // problems first

  // Group by environment once more than one environment is present in the run —
  // a fleet-wide action reads as "Dev / Prod / Stage" sections, not one long
  // flat list. Single-environment (or single-host) runs stay flat.
  const envs = [...new Set(list.map(envOf))];
  const grouped = envs.length > 1;
  const sortedEnvs = [...envs].sort((a, b) =>
    a === "Unassigned" ? 1 : b === "Unassigned" ? -1 : a.localeCompare(b));

  return (
    <>
      {list.length > 1 && (
        <div className="result-summary row" style={{ gap: 10, flexWrap: "wrap", alignItems: "center", padding: "6px 0" }}>
          <span><b>{list.length}</b> hosts · <span className="ok-text">{okCount} ok</span>
            {probCount > 0 && <> · <span className="badge amber">{probCount} need attention</span></>}</span>
          {probCount > 0 && (
            <label className="checkrow" style={{ margin: 0 }}>
              <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
              <span className="faint">Only problems</span>
            </label>
          )}
          <input className="term-find" style={{ minWidth: 160 }} placeholder="Filter host or output…"
                 value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
      )}
      {grouped
        ? sortedEnvs.map((env) => {
            const envShown = shown.filter((r) => envOf(r) === env);
            if (envShown.length === 0) return null;
            return (
              <div key={env}>
                <EnvHeader env={env} rows={list.filter((r) => envOf(r) === env)} />
                {envShown.map((r, j) => <HostRow key={(r.host || "") + j} r={r} single={false} />)}
              </div>
            );
          })
        : shown.map((r, j) => <HostRow key={(r.host || "") + j} r={r} single={list.length <= 1} />)}
      {shown.length === 0 && <div className="empty" style={{ padding: 12 }}>No hosts match the filter.</div>}
    </>
  );
}
