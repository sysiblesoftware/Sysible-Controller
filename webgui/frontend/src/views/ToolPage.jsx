import React, { useMemo, useState, useEffect } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";
import ParamField from "../components/ParamField.jsx";
import HostResults from "../components/HostResults.jsx";

// Delete / unmount / fstab-removal actions whose path is checked against the
// system-critical list (warn for superusers, block for sysadmins).
const CRITICAL_ACTIONS = new Set(["fs_rmdir", "fs_unmount", "fs_remove_fstab"]);

// One-line explanation shown under a group's title so a section says what it
// does at a glance (not just via per-button tooltips). Keyed by group title.
const GROUP_HELP = {
  "Auth Policy": "Set sshd's authentication rules on the checked hosts. Click the button for the exact policy you want — it applies that setting and reloads sshd.",
  "Authorized Keys": "Manage each user's ~/.ssh/authorized_keys — and the host's own SSH host keys — on the checked hosts.",
};

// Faithful desktop tool-page layout: host tree (left); actions organized into
// TABS -> titled GROUP sections (middle); a full-height results column (right).
// Within a group the parameter fields are SHARED across that group's actions
// (rendered once), and each action is a button that uses them — matching the
// desktop's "one Service name field + a row of action buttons" pattern.
export default function ToolPage({ tool, hosts, onRefreshHosts, prefill }) {
  const [targets, setTargets] = useState(prefill?.host ? [prefill.host] : []);
  const [groupParams, setGroupParams] = useState({}); // { [groupKey]: { [param]: value } }
  const [results, setResults] = useState([]);          // newest first
  const [runningAction, setRunningAction] = useState("");
  const [err, setErr] = useState("");
  const [resultsW, setResultsW] = useState(420);       // results column width (px), draggable
  const [expanded, setExpanded] = useState(false);     // results fill the whole pane
  const [activeResult, setActiveResult] = useState(0); // which result tab is shown
  const [role, setRole] = useState("");
  useEffect(() => { api.me().then((d) => setRole(d.role || "")).catch(() => {}); }, []);
  // Deep-link support: a "Fix →" action from the per-host posture view can open a
  // tool with a host pre-selected. Add it to the target set (idempotent) so the
  // operator lands ready to run without re-picking the host they came from.
  useEffect(() => {
    if (prefill?.host) setTargets((t) => (t.includes(prefill.host) ? t : [...t, prefill.host]));
  }, [prefill]);

  // Tool actions run through the host's Sysible agent, so only agent-managed
  // hosts are valid targets here. Pure-SSH connection records (no agent) are
  // filtered out of the picker — they were never actionable from a tool page and
  // only cluttered the tree. (We're phasing SSH connections out; manage those
  // from Sysible Connect.)
  const agentHosts = useMemo(() => (hosts || []).filter((h) => h.has_agent), [hosts]);
  const hiddenSsh = (hosts || []).length - agentHosts.length;

  // Drag the divider to resize the results column.
  function startResize(e) {
    e.preventDefault();
    const startX = e.clientX, startW = resultsW;
    const onMove = (ev) => {
      const next = Math.min(Math.max(startW + (startX - ev.clientX), 300), Math.round(window.innerWidth * 0.7));
      setResultsW(next);
    };
    const onUp = () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  // Actions per tool are organized tab -> group -> [actions]. A tool that declares
  // real tabs (e.g. Firewall) uses them as-is. A tool with a SINGLE implicit tab but
  // many titled groups (e.g. File System Management's 7 sections) is "too busy" as one
  // long vertical scroll, so we promote each group to its own tab — same content, far
  // less scrolling. Small single-pane tools (< AUTO_TAB_MIN groups) stay flat.
  const AUTO_TAB_MIN = 4;
  const tabs = useMemo(() => {
    const order = [];
    const map = new Map();
    for (const a of tool.actions) {
      const tab = a.tab || tool.tool;
      const group = a.group || "";
      if (!map.has(tab)) { map.set(tab, new Map()); order.push(tab); }
      const groups = map.get(tab);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(a);
    }
    // Auto-tab the busy single-pane tools.
    if (order.length === 1) {
      const only = map.get(order[0]);
      const titledGroups = [...only.keys()].filter((g) => g);
      if (titledGroups.length >= AUTO_TAB_MIN) {
        const newOrder = [];
        const newMap = new Map();
        for (const [g, acts] of only.entries()) {
          const tabName = g || order[0];
          newMap.set(tabName, new Map([[g, acts]]));
          newOrder.push(tabName);
        }
        return { order: newOrder, map: newMap, autoTabbed: true };
      }
    }
    return { order, map, autoTabbed: false };
  }, [tool]);

  const [activeTab, setActiveTab] = useState(tabs.order[0]);
  const currentTab = tabs.order.includes(activeTab) ? activeTab : tabs.order[0];

  function gkey(tab, group) { return tab + "\u241f" + group; }
  function gval(key, p) {
    const g = groupParams[key] || {};
    if (g[p.name] !== undefined) return g[p.name];
    return p.type === "checkbox" ? Boolean(p.default) : (p.default ?? "");
  }
  function setGParam(key, name, val) {
    setGroupParams((s) => ({ ...s, [key]: { ...(s[key] || {}), [name]: val } }));
  }

  async function run(action, key) {
    if (targets.length === 0) { setErr("Check one or more hosts on the left first."); return; }
    setErr("");
    const built = {};
    for (const p of action.params) built[p.name] = gval(key, p);

    // Block dispatch when a required text/number/select field is empty, instead
    // of sending a command with an empty argument that just fails on the host
    // with a red result row. (Checkboxes are always "set".)
    const missing = action.params.filter(
      (p) => p.required !== false && (p.type || "text") !== "checkbox"
             && (built[p.name] == null || String(built[p.name]).trim() === ""));
    if (missing.length) {
      setErr(`Fill in: ${missing.map((p) => p.label || p.name).join(", ")}`);
      return;
    }

    // System-critical guard: warn a superuser (who can confirm-through) and
    // block a sysadmin outright before a delete / unmount / fstab removal.
    if (CRITICAL_ACTIONS.has(action.name)) {
      const paths = action.params
        .filter((p) => (p.type || "text") !== "checkbox")
        .map((p) => built[p.name]).filter(Boolean);
      try {
        const c = await api.pathCritical(paths);
        if (c.critical) {
          if (role !== "superuser") {
            setErr(`Blocked — ${c.reason} Only a superuser can do this, after confirming.`);
            return;
          }
          if (!window.confirm(`⚠ SYSTEM-CRITICAL PATH\n\n${c.reason}\n\nThis can break the host and is almost always a mistake. Proceed anyway?`)) return;
          built.allow_critical = true;
        }
      } catch (e) { setErr(e.message); return; }
    }

    if (action.danger && !built.allow_critical &&
        !window.confirm(`${action.label} on ${targets.length} host(s)?`)) return;

    setRunningAction(action.name);
    try {
      if (action.name === "fs_compare") {
        // Cross-host file comparison: aggregated by the BFF, not per-host stdout.
        const data = await api.compareFile(built.path, targets);
        setResults((prev) => [{ label: `Compare ${built.path || "file"}`, compare: data, results: [], at: Date.now() }, ...prev]);
      } else {
        const r = await api.runTool(action.name, targets, built);
        setResults((prev) => [{ label: action.label, ...r, at: Date.now() }, ...prev]);
      }
      setActiveResult(0);
    } catch (e) { setErr(e.message); }
    finally { setRunningAction(""); }
  }

  const groups = tabs.map.get(currentTab) || new Map();

  function closeResult(idx) {
    setResults((prev) => prev.filter((_, i) => i !== idx));
    setActiveResult((cur) => (cur >= idx && cur > 0 ? cur - 1 : cur));
  }
  const active = results[Math.min(activeResult, results.length - 1)];

  const resultsPanel = (
    <div className="tool-results-col" style={expanded ? { flex: 1 } : { width: resultsW, flexShrink: 0 }}>
      <div className="results-head">
        <strong>Results</strong>
        <div className="row">
          <button className="btn ghost sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "⤡ Collapse" : "⤢ Expand"}
          </button>
          <button className="btn ghost sm" onClick={() => { setResults([]); setActiveResult(0); }} disabled={!results.length}>Clear All</button>
        </div>
      </div>

      {results.length === 0 ? (
        <div className="tool-results-scroll">
          <div className="empty" style={{ padding: 24 }}>Run an action — output appears here in tabs.</div>
        </div>
      ) : (
        <>
          <div className="result-tabs">
            {results.map((res, i) => (
              <div key={res.at + "-" + i}
                   className={"result-tab" + (i === Math.min(activeResult, results.length - 1) ? " active" : "")}
                   onClick={() => setActiveResult(i)} title={new Date(res.at).toLocaleTimeString()}>
                <span className={"dot " + (res.compare ? (res.compare.identical ? "ok" : "bad") : (res.results.every((r) => r.ok) ? "ok" : "bad"))} />
                <span className="rt-label">{res.label}</span>
                <span className="x" onClick={(e) => { e.stopPropagation(); closeResult(i); }}>✕</span>
              </div>
            ))}
          </div>
          <div className="tool-results-scroll">
            {active && (
              <div className="result">
                <div className="rh"><strong>{active.label}</strong>
                  <span className="faint mono" style={{ fontSize: 11 }}>{new Date(active.at).toLocaleTimeString()}</span></div>
                {active.compare ? <FileCompare data={active.compare} /> : <HostResults rows={active.results} />}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );

  // Expanded: results take the whole pane (host tree + actions hidden).
  if (expanded) return <div className="tool-flex">{resultsPanel}</div>;

  return (
    <div className="tool-flex">
      <HostTree hosts={agentHosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer={"Check one or more hosts, then run an action." +
                  (hiddenSsh > 0 ? ` (${hiddenSsh} SSH-only host${hiddenSsh === 1 ? "" : "s"} hidden — manage from Sysible Connect.)` : "")} />

      <div className="tool-actions-col">
        {tabs.order.length > 1 && (
          <div className="tabs">
            {tabs.order.map((t) => (
              <button key={t} className={t === currentTab ? "active" : ""} onClick={() => setActiveTab(t)}>{t}</button>
            ))}
          </div>
        )}
        <div className="tool-actions-scroll">
          {[...groups.entries()].map(([groupTitle, acts]) => (
            <GroupSection key={groupTitle || "_"}
                          title={tabs.autoTabbed && groupTitle === currentTab ? "" : groupTitle}
                          desc={GROUP_HELP[groupTitle]} acts={acts} targets={targets}
                          gkey={gkey(currentTab, groupTitle)} gval={gval} setGParam={setGParam}
                          runningAction={runningAction} onRun={run} />
          ))}
          {err && <div className="error-box">{err}</div>}
        </div>
      </div>

      <div className="col-resizer" onMouseDown={startResize} title="Drag to resize the results panel" />
      {resultsPanel}
    </div>
  );
}

// A titled group: shared parameter fields (the ordered union across the
// group's actions, rendered once) followed by a wrapping row of action
// buttons. Param-less groups are just a button row.
function GroupSection({ title, desc, acts, targets, gkey, gval, setGParam, runningAction, onRun }) {
  const sharedParams = useMemo(() => {
    const seen = new Map();
    for (const a of acts) for (const p of a.params) if (!seen.has(p.name)) seen.set(p.name, p);
    return [...seen.values()];
  }, [acts]);

  return (
    <fieldset className="tool-group-box">
      {title && <legend>{title}</legend>}
      {desc && <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>{desc}</div>}
      {sharedParams.length > 0 && (
        <div className="group-fields">
          {sharedParams.map((p) => (
            <div key={p.name} className="group-field">
              <ParamField p={p} value={gval(gkey, p)} targets={targets} onChange={(n, v) => setGParam(gkey, n, v)} />
            </div>
          ))}
        </div>
      )}
      <div className="group-buttons">
        {acts.map((a) => (
          <button key={a.name} className={"btn sm" + (a.danger ? " danger" : "")}
                  disabled={runningAction === a.name} title={a.description}
                  onClick={() => onRun(a, gkey)}>
            {runningAction === a.name ? <span className="spin" /> : a.label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

// Cross-host file comparison result (fs_compare): hosts grouped by content hash,
// so it's obvious at a glance which hosts have a different version of the file.
function fmtSize(s) {
  const n = parseInt(s, 10);
  if (!isFinite(n)) return s || "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function fmtMtime(m) {
  const n = parseInt(m, 10);
  return isFinite(n) ? new Date(n * 1000).toLocaleString() : "";
}
function CompareGroup({ title, hosts, color }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px", marginBottom: 8 }}>
      <div style={{ marginBottom: 6 }}>
        <span className="dot" style={{ background: color }} /><strong>{title}</strong>
        <span className="faint" style={{ marginLeft: 8 }}>{hosts.length}</span>
      </div>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        {hosts.map((h) => <span key={h} className="badge">{h}</span>)}
      </div>
    </div>
  );
}
function FileCompare({ data }) {
  const { path, versions = [], missing = [], unreadable = [], errors = [] } = data || {};
  const total = versions.reduce((n, v) => n + v.hosts.length, 0) + missing.length + unreadable.length + errors.length;
  const allSame = versions.length <= 1 && !missing.length && !unreadable.length && !errors.length;
  return (
    <div style={{ fontSize: 13 }}>
      <div className="faint mono" style={{ marginBottom: 8, wordBreak: "break-all" }}>{path}</div>
      <div style={{ marginBottom: 10, fontWeight: 600, color: allSame ? "var(--green-bright)" : "#e0a83a" }}>
        {allSame
          ? `✓ Identical across all ${total} host${total === 1 ? "" : "s"}`
          : `⚠ ${versions.length} distinct version${versions.length === 1 ? "" : "s"}`
            + (missing.length ? `, ${missing.length} missing` : "")
            + (unreadable.length ? `, ${unreadable.length} unreadable` : "")
            + (errors.length ? `, ${errors.length} error${errors.length === 1 ? "" : "s"}` : "")}
      </div>
      {versions.map((v, i) => (
        <div key={v.sha} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px", marginBottom: 8 }}>
          <div className="spread" style={{ marginBottom: 6 }}>
            <span>
              <span className="dot" style={{ background: i === 0 ? "#4ec07a" : "#e0a83a" }} />
              <strong>{i === 0 ? "Version A — most common" : `Version ${String.fromCharCode(65 + i)} — differs`}</strong>
              <span className="faint" style={{ marginLeft: 8 }}>{v.hosts.length} host{v.hosts.length === 1 ? "" : "s"}</span>
            </span>
            <span className="faint mono" style={{ fontSize: 11 }}>{(v.sha || "").slice(0, 12)} · {fmtSize(v.size)}</span>
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {v.hosts.map((h) => <span key={h} className="badge">{h}</span>)}
          </div>
          {v.mtime && <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>modified {fmtMtime(v.mtime)}</div>}
        </div>
      ))}
      {missing.length > 0 && <CompareGroup title="Missing (no such file)" hosts={missing} color="#7a7a7a" />}
      {unreadable.length > 0 && <CompareGroup title="Unreadable (permission)" hosts={unreadable} color="#7a7a7a" />}
      {errors.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>Errors</div>
          {errors.map((e) => <div key={e.host} className="faint" style={{ fontSize: 12 }}>{e.host}: {e.error}</div>)}
        </div>
      )}
    </div>
  );
}

