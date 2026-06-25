import React, { useEffect, useState, useCallback } from "react";
import { api } from "./api.js";
import Login from "./views/Login.jsx";
import Dashboard from "./views/Dashboard.jsx";
import Hosts from "./views/Hosts.jsx";
import ToolRunner from "./views/ToolRunner.jsx";
import Terminal from "./views/Terminal.jsx";
import Files from "./views/Files.jsx";

const NAV = [
  { id: "dashboard", label: "Dashboard", ico: "▦" },
  { id: "hosts", label: "Hosts", ico: "▤" },
  { id: "tools", label: "Tools", ico: "⚙" },
  { id: "terminal", label: "Terminal", ico: "▷" },
  { id: "files", label: "Files", ico: "↑↓" },
];

const TITLES = {
  dashboard: "Dashboard",
  hosts: "Hosts",
  tools: "Tool Runner",
  terminal: "Sysible Connect — Terminal",
  files: "File Transfer",
};

export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  const [view, setView] = useState("dashboard");

  useEffect(() => {
    api.me()
      .then((d) => setUser(d.username))
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  const onLoggedIn = useCallback((username) => setUser(username), []);
  const onLogout = useCallback(async () => {
    try { await api.logout(); } catch { /* ignore */ }
    setUser(null);
  }, []);

  if (checking) {
    return <div className="login-wrap"><span className="spin" /></div>;
  }
  if (!user) {
    return <Login onLoggedIn={onLoggedIn} />;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">S</div>
          <h1>Sysible</h1>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "active" : ""}
              onClick={() => setView(n.id)}
            >
              <span className="nav-ico">{n.ico}</span>
              {n.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="muted">Signed in as</div>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>{user}</div>
          <button className="btn secondary sm full" onClick={onLogout}>Log out</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h2>{TITLES[view]}</h2>
        </header>
        <section className="content">
          {view === "dashboard" && <Dashboard onNavigate={setView} />}
          {view === "hosts" && <Hosts />}
          {view === "tools" && <ToolRunner />}
          {view === "terminal" && <Terminal />}
          {view === "files" && <Files />}
        </section>
      </main>
    </div>
  );
}
