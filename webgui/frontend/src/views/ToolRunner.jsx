import React, { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";
import ToolPage from "./ToolPage.jsx";
import UserGroupPage from "./UserGroupPage.jsx";

// Tools with a bespoke, desktop-faithful page (richer than the generic
// three-pane action runner). Keyed by catalog tool name.
const CUSTOM_PAGES = {
  "User & Group Administration": UserGroupPage,
};

// System Administration: a grid of tool tiles (from the action catalog);
// selecting one opens its three-pane page.
export default function ToolRunner() {
  const [catalog, setCatalog] = useState(null);
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState("");
  const [openTool, setOpenTool] = useState(null);

  const loadHosts = useCallback(() => {
    api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    api.tools().then((d) => setCatalog(d.tools || [])).catch((e) => setErr(e.message));
    loadHosts();
  }, [loadHosts]);

  if (err) return <div className="error-box">{err}</div>;
  if (catalog === null) return <div className="empty"><span className="spin" /></div>;

  if (openTool) {
    const Custom = CUSTOM_PAGES[openTool.tool];
    return (
      <div style={{ height: "calc(100vh - 180px)" }}>
        <div className="row" style={{ marginBottom: 12 }}>
          <button className="btn ghost sm" onClick={() => setOpenTool(null)}>← All tools</button>
          <strong>{openTool.tool}</strong>
        </div>
        {Custom
          ? <Custom />
          : <ToolPage tool={openTool} hosts={hosts} onRefreshHosts={loadHosts} />}
      </div>
    );
  }

  return (
    <div className="cards">
      {catalog.map((group) => (
        <button key={group.tool} className="card" style={{ textAlign: "left", cursor: "pointer" }}
                onClick={() => setOpenTool(group)}>
          <div style={{ fontWeight: 700, fontSize: 15 }}>{group.tool}</div>
          <div className="muted" style={{ marginTop: 6 }}>
            {group.actions.length} action{group.actions.length === 1 ? "" : "s"}
          </div>
        </button>
      ))}
    </div>
  );
}
