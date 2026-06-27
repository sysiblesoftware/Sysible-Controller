import React, { useEffect, useState, useCallback } from "react";
import { api } from "./api.js";
import Login from "./views/Login.jsx";
import Dashboard from "./views/Dashboard.jsx";
import ToolRunner from "./views/ToolRunner.jsx";
import Connect from "./views/Connect.jsx";
import LiveActivity from "./views/LiveActivity.jsx";
import Settings from "./views/Settings.jsx";
import HostEnrollment from "./views/HostEnrollment.jsx";
import SudoModal from "./components/SudoModal.jsx";
import StandaloneTerminal from "./components/StandaloneTerminal.jsx";

// Sections opened from a dashboard tile. Titles mirror the desktop.
const SECTIONS = {
  hosts: "Sysible Controller Host Enrollment",
  settings: "Sysible Controller Settings",
  connect: "Sysible Connect",
  sysadmin: "System Administration",
  live: "Live Activity & Logs",
};

function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
}

export default function App() {
  const [user, setUser] = useState(null);
  const [role, setRole] = useState("");
  const [checking, setChecking] = useState(true);
  const [view, setView] = useState(null); // null = dashboard
  const [target, setTarget] = useState(null); // {tool, tab} from task search
  const [edition, setEdition] = useState(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [theme, setTheme] = useState(localStorage.getItem("sysible_theme") || "dark");

  useEffect(() => { applyTheme(theme); }, [theme]);

  useEffect(() => {
    api.me()
      .then((d) => { setUser(d.username); setRole(d.role || ""); })
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (user) api.edition().then(setEdition).catch(() => setEdition({}));
  }, [user]);

  const onLoggedIn = useCallback((username, r) => { setUser(username); setRole(r || ""); }, []);
  const onLogout = useCallback(async () => {
    try { await api.logout(); } catch { /* ignore */ }
    setUser(null); setView(null);
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("sysible_theme", next);
  };

  if (checking) return <div className="login-wrap"><span className="spin" /></div>;
  if (!user) return <Login onLoggedIn={onLoggedIn} />;

  // Pop-out terminal window: ?term=<hostId> renders just a full-window shell.
  const qs = new URLSearchParams(location.search);
  if (qs.get("term")) {
    return <StandaloneTerminal hostId={qs.get("term")} label={qs.get("label") || ""} />;
  }

  const badge = (() => {
    if (!edition) return "";
    const ed = (edition.edition || "community");
    const name = ed.charAt(0).toUpperCase() + ed.slice(1) + " Edition";
    if (edition.host_limit) return `${name} · ${edition.host_count ?? 0}/${edition.host_limit} hosts`;
    return name;
  })();

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand" style={{ cursor: "pointer" }} onClick={() => setView(null)}>
          <img className="brand-mark" src="/sysible_logo.png" alt="Sysible"
               onError={(e) => { e.target.style.display = "none"; }} />
          <div>
            <h1>Sysible Controller</h1>
            <div className="sub">{view ? SECTIONS[view] : "Select a tool below to get started."}</div>
          </div>
        </div>

        {badge && <div className="edition-badge">{badge}</div>}

        <div className="header-spacer" />

        <span className="signed-in">Signed in as {user}{role ? ` (${role})` : ""}</span>
        <button className="btn ghost sm" onClick={() => setSudoOpen(true)}>Sudo Password</button>
        <button className="btn ghost sm" onClick={onLogout}>Log Out</button>
        <button className="iconbtn" title="Toggle light/dark" onClick={toggleTheme}>
          {theme === "dark" ? "☾" : "☀"}
        </button>
      </header>

      <div className="app-body">
        {view === null && <Dashboard role={role}
          onOpen={(section, opts) => { setView(section); setTarget(opts || null); }} />}
        {view !== null && (
          <>
            <div className="page-head">
              <span className="crumb" onClick={() => { setView(null); setTarget(null); }}>← Dashboard</span>
              <h2>{SECTIONS[view]}</h2>
            </div>
            {view === "hosts" && <HostEnrollment />}
            {view === "settings" && <Settings />}
            {view === "sysadmin" && <ToolRunner openTool={target?.tool} openTab={target?.tab}
              onConsumed={() => setTarget(null)} />}
            {view === "connect" && <Connect />}
            {view === "live" && <LiveActivity />}
          </>
        )}
      </div>

      {sudoOpen && <SudoModal onClose={() => setSudoOpen(false)} />}
    </div>
  );
}
