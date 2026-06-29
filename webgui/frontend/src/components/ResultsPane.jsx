import React, { useState } from "react";
import HostResults from "./HostResults.jsx";

// Shared dynamic results panel used by the bespoke tool pages so they match the
// generic ToolPage: draggable width, expand/collapse to fill the pane, and one
// tab per run. Each result entry is { label, command?, results:[{host,ok,code,
// stdout,stderr,error}], at }.
//
// `expanded` is controlled by the parent so it can hide its other panes while
// the results fill the page; width and the active tab are managed internally.
export default function ResultsPane({ results, setResults, expanded, onToggleExpand, empty }) {
  const [width, setWidth] = useState(420);
  const [active, setActive] = useState(0);

  function startResize(e) {
    e.preventDefault();
    const startX = e.clientX, startW = width;
    const onMove = (ev) =>
      setWidth(Math.min(Math.max(startW + (startX - ev.clientX), 300), Math.round(window.innerWidth * 0.7)));
    const onUp = () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function clearAll() { setResults([]); setActive(0); }
  function closeTab(idx) {
    setResults((prev) => prev.filter((_, i) => i !== idx));
    setActive((cur) => (cur >= idx && cur > 0 ? cur - 1 : cur));
  }

  const idx = Math.min(active, Math.max(results.length - 1, 0));
  const cur = results[idx];
  const allOk = (res) => res.results.every((r) => r.ok);

  const panel = (
    <div className="tool-results-col" style={expanded ? { flex: 1 } : { width, flexShrink: 0 }}>
      <div className="results-head">
        <strong>Results</strong>
        <div className="row">
          <button className="btn ghost sm" onClick={onToggleExpand}>{expanded ? "⤡ Collapse" : "⤢ Expand"}</button>
          <button className="btn ghost sm" onClick={clearAll} disabled={!results.length}>Clear All</button>
        </div>
      </div>

      {results.length === 0 ? (
        <div className="tool-results-scroll">
          <div className="empty" style={{ padding: 24 }}>{empty || "Run an action — output appears here in tabs."}</div>
        </div>
      ) : (
        <>
          <div className="result-tabs">
            {results.map((res, i) => (
              <div key={res.at + "-" + i}
                   className={"result-tab" + (i === idx ? " active" : "")}
                   onClick={() => setActive(i)} title={new Date(res.at).toLocaleTimeString()}>
                <span className={"dot " + (allOk(res) ? "ok" : "bad")} />
                <span className="rt-label">{res.label}</span>
                <span className="x" onClick={(e) => { e.stopPropagation(); closeTab(i); }}>✕</span>
              </div>
            ))}
          </div>
          <div className="tool-results-scroll">
            {cur && (
              <div className="result">
                <div className="rh"><strong>{cur.label}</strong>
                  <span className="faint mono" style={{ fontSize: 11 }}>{new Date(cur.at).toLocaleTimeString()}</span></div>
                <HostResults rows={cur.results} />
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );

  if (expanded) return panel;
  return (
    <>
      <div className="col-resizer" onMouseDown={startResize} title="Drag to resize the results panel" />
      {panel}
    </>
  );
}
