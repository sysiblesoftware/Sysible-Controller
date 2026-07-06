import React, { useState } from "react";
import HostResults from "./HostResults.jsx";

// Package-aware result view for Host Software Management. The install / update /
// remove / check commands emit machine-readable lines:
//     SYSIBLE_PKG <name> installed <version>
//     SYSIBLE_PKG <name> absent
// We turn those into a scannable per-host state (green ✓ installed <ver> /
// grey ✗ not installed) plus a fleet tally per package — so "did it work on
// every host?" is answerable at a glance instead of reading package-manager
// output. Runs with no SYSIBLE_PKG lines (Detect, Search, Info) fall back to the
// generic HostResults renderer.
const GREEN = "#4ec07a";
const AMBER = "#e0a83a";

function parsePkg(stdout) {
  const out = [];
  for (const line of (stdout || "").split("\n")) {
    const m = line.match(/^SYSIBLE_PKG\s+(\S+)\s+(installed|absent)(?:\s+(.*))?\s*$/);
    if (m) out.push({ name: m[1], installed: m[2] === "installed", version: (m[3] || "").trim() });
  }
  return out;
}

function Chip({ ok, children }) {
  return (
    <span className="badge" style={{
      background: ok ? "rgba(78,192,122,0.15)" : "rgba(122,122,122,0.14)",
      color: ok ? GREEN : "var(--text-faint)",
      border: "1px solid " + (ok ? "rgba(78,192,122,0.4)" : "var(--border)"),
      whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

function PkgHostRow({ r }) {
  const pkgs = parsePkg(r.stdout);
  const raw = ((r.stdout || "") + (r.stderr || "") + (r.error || "")).trim();
  const [open, setOpen] = useState(!r.ok);   // failures expanded, successes collapsed
  return (
    <div style={{ borderTop: "1px solid var(--border)" }}>
      <div className="rh" style={{ gap: 8, cursor: raw ? "pointer" : "default", alignItems: "center", flexWrap: "wrap" }}
           onClick={() => raw && setOpen((o) => !o)}>
        <span className="faint" style={{ width: 10, display: "inline-block" }}>{raw ? (open ? "▾" : "▸") : ""}</span>
        <span className={"dot " + (r.ok ? "ok" : "bad")} />
        <span style={{ fontWeight: 600 }}>{r.host}</span>
        {r.code != null && <span className="faint" style={{ fontSize: 12 }}>exit {r.code}</span>}
        <span style={{ display: "flex", gap: 6, flexWrap: "wrap", flex: 1, minWidth: 0 }}>
          {pkgs.map((p) => (
            <Chip key={p.name} ok={p.installed}>
              {p.installed ? "✓" : "✗"} {p.name}{p.installed && p.version ? " " + p.version : ""}
            </Chip>
          ))}
          {pkgs.length === 0 && !r.ok && <span style={{ color: "#e06c6c", fontSize: 12 }}>failed</span>}
        </span>
      </div>
      {open && raw && <pre>{raw}</pre>}
    </div>
  );
}

export default function PkgResults({ run }) {
  const rows = run.results || [];
  const [onlyProblems, setOnlyProblems] = useState(false);
  const hasPkg = rows.some((r) => parsePkg(r.stdout).length > 0);
  if (!hasPkg) return <HostResults rows={rows} />;

  const names = [...new Set(rows.flatMap((r) => parsePkg(r.stdout).map((p) => p.name)))];
  const tally = names.map((n) => {
    let inst = 0, tot = 0;
    for (const r of rows) {
      const p = parsePkg(r.stdout).find((x) => x.name === n);
      if (p) { tot++; if (p.installed) inst++; }
    }
    return { name: n, inst, tot };
  });
  const okCount = rows.filter((r) => r.ok).length;
  const failCount = rows.length - okCount;
  const shown = rows
    .filter((r) => !onlyProblems || !r.ok)
    .slice().sort((a, b) => (a.ok === b.ok ? 0 : a.ok ? 1 : -1));

  return (
    <>
      <div className="result-summary" style={{ padding: "6px 0" }}>
        <div className="row" style={{ gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          <span><b>{rows.length}</b> host{rows.length === 1 ? "" : "s"} · <span className="ok-text">{okCount} ok</span>
            {failCount > 0 && <> · <span className="badge amber">{failCount} failed</span></>}</span>
          {failCount > 0 && (
            <label className="checkrow" style={{ margin: 0 }}>
              <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
              <span className="faint">Only failures</span>
            </label>
          )}
        </div>
        <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 8 }}>
          {tally.map((t) => (
            <span key={t.name} className="badge" style={{
              background: t.inst === t.tot ? "rgba(78,192,122,0.15)" : "rgba(224,168,58,0.15)",
              color: t.inst === t.tot ? GREEN : AMBER,
              border: "1px solid " + (t.inst === t.tot ? "rgba(78,192,122,0.4)" : "rgba(224,168,58,0.4)"),
            }} title={`${t.name} is installed on ${t.inst} of ${t.tot} host(s)`}>
              {t.name}: {t.inst}/{t.tot} installed
            </span>
          ))}
        </div>
      </div>
      {shown.map((r, j) => <PkgHostRow key={(r.host || "") + j} r={r} />)}
      {shown.length === 0 && <div className="empty" style={{ padding: 12 }}>No hosts match.</div>}
    </>
  );
}
