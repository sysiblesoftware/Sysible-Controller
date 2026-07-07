import React, { useEffect, useState, useCallback } from "react";
import { api } from "./api.js";
import Login from "./views/Login.jsx";
import Dashboard from "./views/Dashboard.jsx";
import Performance from "./views/Performance.jsx";
import ToolRunner from "./views/ToolRunner.jsx";
import Connect from "./views/Connect.jsx";
import LiveActivity from "./views/LiveActivity.jsx";
import Settings from "./views/Settings.jsx";
import HostEnrollment from "./views/HostEnrollment.jsx";
import HostDetail from "./views/HostDetail.jsx";
import Updates from "./views/Updates.jsx";
import Schedules from "./views/Schedules.jsx";
import Alerts from "./views/Alerts.jsx";
import FleetQuery from "./views/FleetQuery.jsx";
import Topology from "./views/Topology.jsx";
import UpdatesBadge from "./components/UpdatesBadge.jsx";
import SudoModal from "./components/SudoModal.jsx";
import StandaloneTerminal from "./components/StandaloneTerminal.jsx";

const SECTIONS = {
  hosts: "Host Enrollment",
  topology: "Network Topology",
  perf: "Fleet Performance",
  updates: "Update Hosts",
  schedules: "Scheduled Jobs",
  alerts: "Alerting",
  quickactions: "Quick System Actions",
  sysadmin: "System Administration",
  fleetquery: "Fleet Query",
  connect: "Sysible Connect",
  live: "Live Activity & Logs",
  settings: "Settings",
};

// Left-rail navigation. `su` = superuser-only. `aud` = visible to the read-only
// 'auditor' role (which sees ONLY these). Auditors get a strict allow-list:
// Dashboard, Performance, and the Activity feed — nothing that can act.
//
// Ordered to follow how the fleet is actually run, top to bottom, rather than a
// mixed list:
//   1. Observe   — Dashboard, Topology, Performance (see the fleet's state)
//   2. Onboard   — Host Enrollment (bring hosts under management first)
//   3. Operate   — Connect, Quick System Actions, System Administration Tools,
//                  Fleet Query (act on hosts, quick actions before deep tools)
//   4. Maintain  — Update Hosts, Schedules, Alerts (keep it healthy over time)
//   5. Oversee   — Activity & Logs, Settings (audit + configure)
const NAV = [
  // Observe
  { key: null, label: "Dashboard", icon: "grid", su: false, aud: true },
  { key: "topology", label: "Topology", icon: "share", su: false, aud: true },
  { key: "perf", label: "Performance", icon: "chart", su: false, aud: true },
  // Onboard
  { key: "hosts", label: "Host Enrollment", icon: "server", su: true },
  // Operate
  { key: "connect", label: "Connect", icon: "terminal", su: false },
  { key: "quickactions", label: "Quick System Actions", icon: "bolt", su: false },
  { key: "sysadmin", label: "System Administration Tools", icon: "tools", su: false },
  { key: "fleetquery", label: "Fleet Query", icon: "search", su: false },
  // Maintain
  { key: "updates", label: "Update Hosts", icon: "download", su: false, aud: true },
  { key: "schedules", label: "Schedules", icon: "clock", su: false },
  { key: "alerts", label: "Alerts", icon: "bell", su: false },
  // Oversee
  { key: "live", label: "Activity & Logs", icon: "activity", su: true, aud: true },
  { key: "settings", label: "Settings", icon: "cog", su: true },
];

const ICONS = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><line x1="7" y1="7" x2="7.01" y2="7"/><line x1="7" y1="17" x2="7.01" y2="17"/></>,
  tools: <><path d="M3 21h4L17 11l-4-4L3 17v4z"/><path d="M14.5 5.5l4 4"/></>,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></>,
  activity: <><path d="M4 12h4l2-6 4 12 2-6h4"/></>,
  chart: <><path d="M4 19V5M4 19h16M8 16l3-4 3 3 4-6"/></>,
  cog: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></>,
  bolt: <><path d="M13 3L4 14h6l-1 7 9-11h-6z"/></>,
  download: <><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  bell: <><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10 20a2 2 0 0 0 4 0"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.8-3.8"/></>,
  share: <><circle cx="6" cy="12" r="2.4"/><circle cx="18" cy="5.5" r="2.4"/><circle cx="18" cy="18.5" r="2.4"/><path d="M8.2 10.9l7.6-4M8.2 13.1l7.6 4"/></>,
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
  const [history, setHistory] = useState([]); // breadcrumb stack of {view,target}
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
    setUser(null); setView(null); setHistory([]);
  }, []);

  // Navigate, remembering where we came from so a global Back button can return
  // there (e.g. Performance → a tool → Back to Performance) without walking the
  // menus again. Used by both the sidebar and in-app "Fix in…"/drill links.
  const go = useCallback((v, t = null) => {
    // Sysible Connect always opens in its own browser tab (a named window, so
    // re-triggering focuses the existing one) — never inline. Centralised here
    // so every entry point (nav, feature search, drill-ins) behaves the same.
    if (v === "connect") { window.open("/?view=connect", "sysible_connect"); return; }
    if (v === view && JSON.stringify(t) === JSON.stringify(target)) return;
    setHistory((h) => [...h, { view, target }]);
    setView(v); setTarget(t);
  }, [view, target]);
  const goBack = useCallback(() => {
    setHistory((h) => {
      if (!h.length) return h;
      const prev = h[h.length - 1];
      setView(prev.view); setTarget(prev.target || null);
      return h.slice(0, -1);
    });
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
  // Sysible Connect runs in its own browser tab (opened from the nav), as a
  // focused window with just its own header — no left rail. Host terminals
  // opened from here pop out into their own windows (see Connect.openTerm).
  if (qs.get("view") === "connect") {
    return (
      <div className="shell connect-standalone">
        <main className="main">
          <div className="main-top">
            <div className="row" style={{ alignItems: "center", gap: 10, minWidth: 0 }}>
              <img className="rail-mark" src="/sysible_logo.png" alt="" style={{ width: 22, height: 22 }}
                   onError={(e) => { e.target.style.display = "none"; }} />
              <h2 style={{ margin: 0 }}>Sysible Connect</h2>
            </div>
            <div className="row" style={{ alignItems: "center", gap: 12 }}>
              <button className="btn ghost sm" onClick={() => setSudoOpen(true)}>Sudo Password</button>
              <button className="iconbtn" title="Toggle light/dark" onClick={toggleTheme}>
                {theme === "dark" ? "☾" : "☀"}
              </button>
            </div>
          </div>
          <div className="main-scroll"><Connect /></div>
        </main>
        {sudoOpen && <SudoModal onClose={() => setSudoOpen(false)} />}
      </div>
    );
  }

  const isSuper = role === "superuser";
  const isAuditor = role === "auditor";
  // Auditors get a strict allow-list (read-only surfaces only); everyone else
  // sees everything except superuser-gated items.
  const nav = NAV.filter((n) => (isAuditor ? n.aud : (!n.su || isSuper)));

  // A plain edition badge — no usage-vs-limit counts (the edition is uncapped).
  const editionLabel = (() => {
    if (!edition) return "";
    const ed = (edition.edition || "community");
    const name = ed.charAt(0).toUpperCase() + ed.slice(1);
    return `${name} Edition`;
  })();

  return (
    <div className="shell">
      <nav className="rail">
        <div className="rail-brand" onClick={() => go(null)}>
          <img className="rail-mark" src="/sysible_logo.png" alt=""
               onError={(e) => { e.target.style.display = "none"; e.target.nextSibling.style.display = "flex"; }} />
          <span className="rail-mark-fallback" style={{ display: "none" }}>S</span>
          <span className="rail-name">Sysible Controller</span>
        </div>

        <div className="rail-nav">
          {nav.map((n) => (
            <button key={n.key ?? "dash"}
                    className={"rail-item" + (view === n.key ? " active" : "")}
                    title={n.key === "connect" ? "Opens in a new tab" : undefined}
                    onClick={() => go(n.key, null)}>
              <NavIcon name={n.icon} /><span>{n.label}</span>
              {n.key === "connect" && <span className="rail-ext" aria-hidden="true">↗</span>}
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
          <div className="row" style={{ alignItems: "center", gap: 10, minWidth: 0 }}>
            {history.length > 0 && (
              <button className="btn ghost sm" onClick={goBack} title="Back to the previous screen">← Back</button>
            )}
            <h2 style={{ margin: 0 }}>{view === "host" ? (target?.label || "Host Posture") : (view ? SECTIONS[view] : "Dashboard")}</h2>
          </div>
          <div className="row" style={{ alignItems: "center", gap: 12, minWidth: 0 }}>
            {view === null && isSuper && <UpdatesBadge onOpen={() => go("settings", { tab: "controller" })} />}
            <div className="main-top-sub">{view ? "" : `Signed in as ${user}${role ? ` · ${role}` : ""}`}</div>
          </div>
        </div>
        <div className="main-scroll">
          {view === null && <Dashboard role={role} edition={edition}
            onOpen={(section, opts) => go(section, opts || null)} />}
          {view === "topology" && <Topology onOpen={(section, opts) => go(section, opts || null)} />}
          {view === "perf" && <Performance />}
          {view === "updates" && <Updates role={role} />}
          {!isAuditor && view === "schedules" && <Schedules />}
          {!isAuditor && view === "alerts" && <Alerts />}
          {view === "host" && <HostDetail hostId={target?.id} label={target?.label} canAct={!isAuditor}
            onBack={() => (history.length > 0 ? goBack() : go(null))}
            onOpen={(section, opts) => go(section, opts || null)} />}
          {!isAuditor && view === "hosts" && <HostEnrollment />}
          {!isAuditor && view === "settings" && <Settings initialTab={target?.tab} />}
          {!isAuditor && view === "quickactions" && <ToolRunner solo="Quick System Actions" />}
          {!isAuditor && view === "fleetquery" && <FleetQuery />}
          {!isAuditor && view === "sysadmin" && <ToolRunner openTool={target?.tool} openTab={target?.tab}
            openPrefill={target?.prefill} onConsumed={() => setTarget(null)} />}
          {view === "live" && <LiveActivity role={role} />}
        </div>
      </main>

      {sudoOpen && <SudoModal onClose={() => setSudoOpen(false)} />}
    </div>
  );
}
