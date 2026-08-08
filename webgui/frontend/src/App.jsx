import React, { useEffect, useState, useCallback, useRef } from "react";
import { api, setUnauthorizedHandler } from "./api.js";
import Login from "./views/Login.jsx";
import ForcePasswordChange from "./views/ForcePasswordChange.jsx";
import Toasts from "./components/Toasts.jsx";
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
import HealthBanner from "./components/HealthBanner.jsx";
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
  // Reference — download the bundled, self-contained offline manual. `download`
  // makes this a file link (not a view switch); available to every role.
  { key: "docs", label: "Documentation", icon: "book", su: false, aud: true,
    download: "/api/docs/download" },
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
  book: <><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H19v15H6a2 2 0 0 0-2 2z"/><path d="M4 19.5A1.5 1.5 0 0 0 5.5 21H19"/></>,
};

function NavIcon({ name }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{ICONS[name]}</svg>
  );
}

// Selectable console skins. Each is a complete look defined as a token block in
// styles.css ([data-skin="…"]); "base" is the original Slate palette. There is no
// separate light/dark toggle — "Porcelain" is the light skin.
const SKINS = [
  { id: "base",  name: "Slate" },
  { id: "brand", name: "Sysible" },
  { id: "amber", name: "Amber Control Room" },
  { id: "nord",  name: "Nord Frost" },
  { id: "phos",  name: "Phosphor" },
  { id: "blue",  name: "Blueprint" },
  { id: "porc",  name: "Porcelain (light)" },
];
const SKIN_IDS = new Set(SKINS.map((s) => s.id));
function applySkin(s) { document.documentElement.setAttribute("data-skin", s); }

// `block` renders a full-width row (rail footer); the default compact pill is
// for the horizontal Connect toolbar.
function SkinPicker({ skin, onChange, block }) {
  return (
    <label className={"skin-picker" + (block ? " block" : "")} title="Console skin">
      <SkinIcon />
      {block && <span className="skin-picker-label">Skin</span>}
      <select value={skin} onChange={(e) => onChange(e.target.value)} aria-label="Console skin">
        {SKINS.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
    </label>
  );
}
function SkinIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="13.5" cy="6.5" r="2.5" /><circle cx="17.5" cy="10.5" r="2.5" />
      <circle cx="8.5" cy="7.5" r="2.5" /><circle cx="6.5" cy="12.5" r="2.5" />
      <path d="M12 2a10 10 0 0 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.2 0-1 .8-1.5 1.7-1.5H16a6 6 0 0 0 6-6c0-4.9-4.5-8-10-8Z" />
    </svg>
  );
}

// --- URL-addressable views ------------------------------------------------
// Nav items are real links (?view=<key>) so they can be opened in a new tab,
// bookmarked, and shared — not just JS buttons. Excludes the dashboard
// (null → ?view=dashboard), Sysible Connect (opens its own window), and the
// docs download link.
const VIEW_KEYS = new Set(
  NAV.filter((n) => n.key && n.key !== "connect" && !n.download).map((n) => n.key),
);
const NAV_BY_KEY = Object.fromEntries(NAV.filter((n) => n.key).map((n) => [n.key, n]));
// In-app DRILL-IN views: reached by clicking into something (e.g. a host from a
// dashboard finding, a topology node, or a fleet card), NOT from the sidebar — so
// they're absent from NAV/NAV_BY_KEY. They must be allowed here, or the role
// redirect below treats them as "unknown view" and bounces every drill-in back to
// the dashboard (which read as a page "refresh"). Per-host data access is still
// gated server-side (auth + EE env-scope); HostDetail itself is read-only for
// auditors via canAct.
const DRILL_VIEWS = new Set(["host"]);

// Single source of truth for "may this role open this view", used by BOTH the rail
// filter and the render/redirect below so they can't drift. Superuser-gated views
// (su:true) require superuser; an auditor gets a strict allow-list (aud:true). A
// null key is the always-available dashboard; an unknown key is denied. Without a
// shared gate the rail hid su views but the render only checked !isAuditor, so a
// sysadmin could open Settings/Host Enrollment by URL (?view=settings).
function canSeeView(key, role) {
  if (key == null) return true;                 // dashboard
  if (DRILL_VIEWS.has(key)) return true;        // in-app drill-in (e.g. host detail)
  const n = NAV_BY_KEY[key];
  if (!n) return false;                         // unknown view
  if (role === "auditor") return !!n.aud;
  return !n.su || role === "superuser";
}
function hrefForView(key) { return "?view=" + (key || "dashboard"); }
function viewFromSearch() {
  try {
    const v = new URLSearchParams(window.location.search).get("view");
    if (v && VIEW_KEYS.has(v)) return v;
  } catch { /* ignore */ }
  return null; // dashboard
}
function setSearchView(v) {
  // Keep the address bar in sync with the current top-level view via
  // replaceState — no new history entries (the in-app "← Back" owns view
  // history), just a shareable/refresh-safe URL.
  try {
    const u = new URL(window.location.href);
    u.searchParams.set("view", v || "dashboard");
    window.history.replaceState(null, "", u.pathname + u.search);
  } catch { /* ignore */ }
}

export default function App() {
  const [user, setUser] = useState(null);
  const [role, setRole] = useState("");
  const [checking, setChecking] = useState(true);
  const [mustChange, setMustChange] = useState(false);
  // Bumped on every nav click so a stateful view (e.g. the tools grid) can reset
  // its own sub-page when its nav item is clicked again — see ToolRunner.
  const [navTick, setNavTick] = useState(0);
  const [view, setView] = useState(viewFromSearch); // null = dashboard; seeded from ?view=
  const [target, setTarget] = useState(null);
  const [history, setHistory] = useState([]); // breadcrumb stack of {view,target}
  const [edition, setEdition] = useState(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [skin, setSkin] = useState(() => {
    const s = localStorage.getItem("sysible_skin");
    return s && SKIN_IDS.has(s) ? s : "brand";
  });

  // Transient toast notifications (host enrolled, etc.). App owns the auto-
  // dismiss timers; a monotonic ref counter keeps ids unique.
  const [toasts, setToasts] = useState([]);
  const toastId = useRef(0);
  const pushToast = useCallback((msg, opts = {}) => {
    const id = ++toastId.current;
    setToasts((t) => [...t, { id, msg, title: opts.title, kind: opts.kind || "info" }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), opts.ttl || 9000);
  }, []);
  const dismissToast = useCallback((id) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  useEffect(() => { applySkin(skin); }, [skin]);

  // Notify when a host enrolls: poll the agent inventory and toast any host_id
  // that appears after the first (baseline) fetch. Runs while signed in.
  const seenHostsRef = useRef(null);
  useEffect(() => {
    if (!user) { seenHostsRef.current = null; return; }
    let stopped = false;
    async function poll() {
      try {
        const list = (await api.agents()).agents || [];
        if (stopped) return;
        if (seenHostsRef.current === null) {
          seenHostsRef.current = new Set(list.map((a) => a.host_id));  // baseline: no toasts
          return;
        }
        for (const a of list) {
          if (!seenHostsRef.current.has(a.host_id)) {
            seenHostsRef.current.add(a.host_id);
            pushToast(`${a.hostname || a.host_id} just enrolled${a.environment ? " · " + a.environment : ""}`,
                      { title: "New host enrolled", kind: "success" });
          }
        }
      } catch { /* transient — try again next tick */ }
    }
    poll();
    const iv = setInterval(poll, 20000);
    return () => { stopped = true; clearInterval(iv); };
  }, [user, pushToast]);

  useEffect(() => {
    api.me()
      .then((d) => { setUser(d.username); setRole(d.role || ""); setMustChange(!!d.must_change_password); })
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  // When any API call sees a 401 (session expired/revoked), drop the stale session
  // and return to the login screen — once, and only if we currently think we're
  // logged in (so a background poll firing during logout doesn't double-toast).
  const userRef = useRef(null);
  userRef.current = user;
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (!userRef.current) return;
      setUser(null); setView(null); setHistory([]); setMustChange(false);
      pushToast("Your session expired — please sign in again.", { kind: "error" });
    });
    return () => setUnauthorizedHandler(null);
  }, [pushToast]);

  useEffect(() => {
    if (user) api.edition().then(setEdition).catch(() => setEdition({}));
  }, [user]);

  const onLoggedIn = useCallback((username, r, mcp) => {
    setUser(username); setRole(r || ""); setMustChange(!!mcp);
  }, []);
  const onLogout = useCallback(async () => {
    try { await api.logout(); } catch { /* ignore */ }
    setUser(null); setView(null); setHistory([]); setMustChange(false);
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
    // Keep the URL in sync for top-level views (dashboard or a nav key); leave it
    // untouched for in-app drill-ins (e.g. a specific host), which aren't
    // deep-linkable on their own.
    if (v === null || VIEW_KEYS.has(v)) setSearchView(v);
  }, [view, target]);
  const goBack = useCallback(() => {
    setHistory((h) => {
      if (!h.length) return h;
      const prev = h[h.length - 1];
      setView(prev.view); setTarget(prev.target || null);
      if (prev.view === null || VIEW_KEYS.has(prev.view)) setSearchView(prev.view);
      return h.slice(0, -1);
    });
  }, []);

  const chooseSkin = (next) => {
    setSkin(next);
    localStorage.setItem("sysible_skin", next);
  };

  // Redirect away from a view the current role can't access — a URL-seeded ?view=
  // (bookmark, shared link) or a mid-session role change. Bouncing to the dashboard
  // avoids both disclosing a superuser surface to a lesser role AND dead-ending on a
  // blank pane under a section title. Runs only once role is known.
  useEffect(() => {
    if (role && !canSeeView(view, role)) go(null);
  }, [role, view, go]);

  if (checking) return <div className="login-wrap"><span className="spin" /></div>;
  if (!user) return <Login onLoggedIn={onLoggedIn} />;
  // A temporary password must be rotated before anything else is reachable.
  if (mustChange) {
    return <ForcePasswordChange username={user} onDone={() => setMustChange(false)} onLogout={onLogout} />;
  }

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
              <SkinPicker skin={skin} onChange={chooseSkin} />
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
  // sees everything except superuser-gated items. Same gate as the render/redirect.
  const nav = NAV.filter((n) => canSeeView(n.key, role));

  // A plain edition badge — no usage-vs-limit counts (the edition is uncapped).
  const editionLabel = (() => {
    if (!edition) return "";
    const ed = (edition.edition || "community");
    const name = ed.charAt(0).toUpperCase() + ed.slice(1);
    return `${name} Edition`;
  })();

  return (
    <div className="shell">
      <Toasts items={toasts} onDismiss={dismissToast} />
      <nav className="rail">
        <div className="rail-brand" onClick={() => go(null)}>
          <img className="rail-mark" src="/sysible_logo.png" alt=""
               onError={(e) => { e.target.style.display = "none"; e.target.nextSibling.style.display = "flex"; }} />
          <span className="rail-mark-fallback" style={{ display: "none" }}>S</span>
          <span className="rail-name">Sysible Controller</span>
        </div>

        <div className="rail-nav">
          {nav.map((n) => (
            n.download ? (
              <a key={n.key ?? "dash"} className="rail-item" href={n.download} download
                 title="Download the offline documentation">
                <NavIcon name={n.icon} /><span>{n.label}</span>
                <span className="rail-ext" aria-hidden="true">↓</span>
              </a>
            ) : (
              <a key={n.key ?? "dash"} href={hrefForView(n.key)}
                 className={"rail-item" + (view === n.key ? " active" : "")}
                 title={n.key === "connect" ? "Opens in a new tab" : undefined}
                 onClick={(e) => {
                   // Let the browser handle modifier-clicks (⌘/Ctrl/Shift/Alt) so
                   // "open in new tab/window" works; otherwise navigate in-app.
                   if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                   e.preventDefault();
                   setNavTick((t) => t + 1);
                   go(n.key, null);
                 }}>
                <NavIcon name={n.icon} /><span>{n.label}</span>
                {n.key === "connect" && <span className="rail-ext" aria-hidden="true">↗</span>}
              </a>
            )
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
          </div>
          <SkinPicker skin={skin} onChange={chooseSkin} block />
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
          {isSuper && <HealthBanner />}
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
          {isSuper && view === "hosts" && <HostEnrollment />}
          {isSuper && view === "settings" && <Settings initialTab={target?.tab} />}
          {!isAuditor && view === "quickactions" && <ToolRunner solo="Quick System Actions" />}
          {!isAuditor && view === "fleetquery" && <FleetQuery />}
          {!isAuditor && view === "sysadmin" && <ToolRunner openTool={target?.tool} openTab={target?.tab}
            openPrefill={target?.prefill} onConsumed={() => setTarget(null)} resetKey={navTick} />}
          {(isSuper || isAuditor) && view === "live" && <LiveActivity role={role} />}
        </div>
      </main>

      {sudoOpen && <SudoModal onClose={() => setSudoOpen(false)} />}
    </div>
  );
}
