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

const SECTIONS = {
  hosts: "Host Enrollment",
  sysadmin: "System Administration",
  connect: "Sysible Connect",
  live: "Live Activity & Logs",
  settings: "Settings",
};

// Left-rail navigation. `su` = superuser-only (matches the desktop role gating).
const NAV = [
  { key: null, label: "Dashboard", icon: "grid", su: false },
  { key: "hosts", label: "Host Enrollment", icon: "server", su: true },
  { key: "sysadmin", label: "Administration", icon: "tools", su: false },
  { key: "connect", label: "Connect", icon: "terminal", su: false },
  { key: "live", label: "Activity & Logs", icon: "activity", su: true },
  { key: "settings", label: "Settings", icon: "cog", su: true },
];

const ICONS = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><line x1="7" y1="7" x2="7.01" y2="7"/><line x1="7" y1="17" x2="7.01" y2="17"/></>,
  tools: <><path d="M3 21h4L17 11l-4-4L3 17v4z"/><path d="M14.5 5.5l4 4"/></>,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></>,
  activity: <><path d="M4 12h4l2-6 4 12 2-6h4"/></>,
  cog: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></>,
};

function NavIcon({ name }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{ICONS[name]}</svg>
  );
}

function applyTheme(t) { document.documentElement.setAttribute("data-theme", t); }

export default function App() {
  const [user, setUser] = useState(null);
  const [role, setRole] = useState("");
  const [checking, setChecking] = useState(true);
  const [view, setView] = useState(null); // null = dashboard
  const [target, setTarget] = useState(null);
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

  const qs = new URLSearchParams(location.search);
  if (qs.get("term")) {
    return <StandaloneTerminal hostId={qs.get("term")} label={qs.get("label") || ""} />;
  }

  const isSuper = role === "superuser";
  const nav = NAV.filter((n) => !n.su || isSuper);

  const editionLabel = (() => {
    if (!edition) return "";
    const ed = (edition.edition || "community");
    const name = ed.charAt(0).toUpperCase() + ed.slice(1);
    if (edition.host_limit) return `${name} · ${edition.host_count ?? 0}/${edition.host_limit} hosts`;
    return name;
  })();

  return (
    <div className="shell">
      <nav className="rail">
        <div className="rail-brand" onClick={() => setView(null)}>
          <img className="rail-mark" src="/sysible_logo.png" alt=""
               onError={(e) => { e.target.style.display = "none"; e.target.nextSibling.style.display = "flex"; }} />
          <span className="rail-mark-fallback" style={{ display: "none" }}>S</span>
          <span className="rail-name">Sysible</span>
        </div>

        <div className="rail-nav">
          {nav.map((n) => (
            <button key={n.key ?? "dash"}
                    className={"rail-item" + (view === n.key ? " active" : "")}
                    onClick={() => { setView(n.key); setTarget(null); }}>
              <NavIcon name={n.icon} /><span>{n.label}</span>
            </button>
          ))}
        </div>

        <div className="rail-footer">
          {editionLabel && <div className="rail-edition">{editionLabel}</div>}
          <div className="rail-user">
            <span className="rail-avatar">{(user || "?").slice(0, 2).toUpperCase()}</span>
            <div className="rail-user-meta">
              <div className="rail-user-name">{user}</div>
              {role && <div className="rail-user-role">{role}</div>}
            </div>
          </div>
          <div className="rail-actions">
            <button className="btn ghost sm" onClick={() => setSudoOpen(true)}>Sudo Password</button>
            <button className="iconbtn" title="Toggle light/dark" onClick={toggleTheme}>
              {theme === "dark" ? "☾" : "☀"}
            </button>
          </div>
          <button className="btn ghost sm rail-logout" onClick={onLogout}>Log Out</button>
        </div>
      </nav>

      <main className="main">
        <div className="main-top">
          <h2>{view ? SECTIONS[view] : "Dashboard"}</h2>
          <div className="main-top-sub">{view ? "" : `Signed in as ${user}${role ? ` · ${role}` : ""}`}</div>
        </div>
        <div className="main-scroll">
          {view === null && <Dashboard role={role} edition={edition}
            onOpen={(section, opts) => { setView(section); setTarget(opts || null); }} />}
          {view === "hosts" && <HostEnrollment />}
          {view === "settings" && <Settings />}
          {view === "sysadmin" && <ToolRunner openTool={target?.tool} openTab={target?.tab}
            onConsumed={() => setTarget(null)} />}
          {view === "connect" && <Connect />}
          {view === "live" && <LiveActivity />}
        </div>
      </main>

      {sudoOpen && <SudoModal onClose={() => setSudoOpen(false)} />}
    </div>
  );
}
