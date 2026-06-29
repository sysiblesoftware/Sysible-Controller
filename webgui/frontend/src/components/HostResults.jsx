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
      `${r.host} ${r.stdout || ""} ${r.stderr || ""} ${r.error || ""}`.toLowerCase().includes(q))
    .slice().sort((a, b) => (a.ok === b.ok ? 0 : a.ok ? 1 : -1)); // problems first

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
      {shown.map((r, j) => <HostRow key={(r.host || "") + j} r={r} single={list.length <= 1} />)}
      {shown.length === 0 && <div className="empty" style={{ padding: 12 }}>No hosts match the filter.</div>}
    </>
  );
}
