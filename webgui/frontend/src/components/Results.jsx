import React from "react";

// Per-host result cards. Mirrors the desktop result tabs: a green/red
// status per host plus stdout/stderr. The command actually run is shown
// once at the top so the admin sees exactly what was dispatched.
export default function Results({ run }) {
  if (!run) return null;
  if (run.pending) return <div className="muted">Running…</div>;
  if (run.error) return <div className="alert error">{run.error}</div>;

  return (
    <div className="results">
      <div className="results-cmd">
        <span className="muted small">Command</span>
        <code>{run.command}</code>
      </div>
      {run.results.map((r, i) => (
        <div key={i} className={`result-card ${r.ok ? "ok" : "bad"}`}>
          <div className="result-head">
            <span className="result-host">{r.host}</span>
            <span className={`result-badge ${r.ok ? "ok" : "bad"}`}>
              {r.ok ? "OK" : r.error ? "ERROR" : `exit ${r.code}`}
            </span>
          </div>
          {r.error && <div className="result-error">{r.error}</div>}
          {r.stdout && <pre className="result-out">{r.stdout}</pre>}
          {r.stderr && <pre className="result-out err">{r.stderr}</pre>}
          {!r.stdout && !r.stderr && !r.error && (
            <div className="muted small">(no output)</div>
          )}
        </div>
      ))}
    </div>
  );
}
