import React, { useEffect, useState, useCallback, useRef } from "react";
import { api, apiUrl, setUnauthorizedHandler } from "./api.js";
import Logo from "./Logo.jsx";
import Login from "./views/Login.jsx";
import Setup from "./views/Setup.jsx";
import ForcePasswordChange from "./views/ForcePasswordChange.jsx";
import Toasts from "./components/Toasts.jsx";
import Dashboard from "./views/Dashboard.jsx";
import ToolRunner from "./views/ToolRunner.jsx";
import LiveActivity from "./views/LiveActivity.jsx";
import Settings from "./views/Settings.jsx";
import HostEnrollment from "./views/HostEnrollment.jsx";
import Migrate from "./views/Migrate.jsx";
import HostDetail from "./views/HostDetail.jsx";
import Updates from "./views/Updates.jsx";
import Schedules from "./views/Schedules.jsx";
import Alerts from "./views/Alerts.jsx";
import FleetQuery from "./views/FleetQuery.jsx";
import UpdatesBadge from "./components/UpdatesBadge.jsx";
import HealthBanner from "./components/HealthBanner.jsx";
import SudoModal from "./components/SudoModal.jsx";

const SECTIONS = {
  hosts: "Host Enrollment",
  migrate: "Migrate Controller",
  updates: "Update Hosts",
  schedules: "Scheduled Jobs",
  alerts: "Alerting",
  quickactions: "Quick System Actions",
  sysadmin: "System Administration",
  fleetquery: "Fleet Query",
  live: "Live Activity & Logs",
  settings: "Settings",
};

// Left-rail navigation. `su` = superuser-only. `aud` = visible to the read-only
// 'auditor' role (which sees ONLY these). Auditors get a strict allow-list:
// Dashboard, Update Hosts, Activity & Logs, and Documentation — nothing that can act.
//
// Ordered to follow how the fleet is actually run, top to bottom, rather than a
// mixed list:
//   1. Observe   — Dashboard (see the fleet's state)
//   2. Onboard   — Host Enrollment (bring hosts under management first)
//   3. Operate   — Quick System Actions, System Administration Tools,
//                  Fleet Query (act on hosts, quick actions before deep tools)
//   4. Maintain  — Update Hosts, Schedules, Alerts (keep it healthy over time)
//   5. Oversee   — Activity & Logs, Settings (audit + configure)
const NAV = [
  // Observe
  { key: null, label: "Dashboard", icon: "grid", su: false, aud: true },
  // Onboard
  { key: "hosts", label: "Host Enrollment", icon: "server", su: true },
  { key: "migrate", label: "Migrate", icon: "move", su: true },
  // Operate
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
  // Reference — open the bundled, self-contained manual as a webpage in a new
  // tab (not a view switch, not a download); available to every role.
  { key: "docs", label: "Documentation", icon: "book", su: false, aud: true,
    external: apiUrl("/api/docs/view") },
];

const ICONS = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  move: <><path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20"/></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><line x1="7" y1="7" x2="7.01" y2="7"/><line x1="7" y1="17" x2="7.01" y2="17"/></>,
  tools: <><path d="M3 21h4L17 11l-4-4L3 17v4z"/><path d="M14.5 5.5l4 4"/></>,
  activity: <><path d="M4 12h4l2-6 4 12 2-6h4"/></>,
  cog: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></>,
  bolt: <><path d="M13 3L4 14h6l-1 7 9-11h-6z"/></>,
  download: <><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  bell: <><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10 20a2 2 0 0 0 4 0"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.8-3.8"/></>,
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
// The console ships a single look: Sysible (the brand palette). Other skins were
// removed; the picker is gone and the app always renders data-skin="brand".
const SKINS = [
  { id: "brand", name: "Sysible" },
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
// (null → ?view=dashboard) and the docs external/download link.
const VIEW_KEYS = new Set(
  NAV.filter((n) => n.key && !n.download && !n.external).map((n) => n.key),
);
const NAV_BY_KEY = Object.fromEntries(NAV.filter((n) => n.key).map((n) => [n.key, n]));
// In-app DRILL-IN views: reached by clicking into something (e.g. a host from a
// dashboard finding or a fleet card), NOT from the sidebar — so
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
    // Drill-in views aren't in the sidebar, so they're absent from VIEW_KEYS —
    // but ?view=host&id=… is a real deep link (the Visualizer's fleet map opens a
    // node here). Only honour it with an id; canSeeView already allows DRILL_VIEWS
    // and per-host access is still gated server-side.
    if (v && DRILL_VIEWS.has(v) && new URLSearchParams(window.location.search).get("id")) return v;
  } catch { /* ignore */ }
  return null; // dashboard
}
function targetFromSearch() {
  // Seed the view's target from the URL so a deep link can open a specific
  // sub-tab — e.g. the SLOP Administration console linking straight to
  // Settings → Enrollment Access via ?view=settings&tab=enrollacl. Only the
  // tabbed views (Settings, the sysadmin tools grid) read target.tab.
  try {
    const q = new URLSearchParams(window.location.search);
    const v = q.get("view");
    const tab = q.get("tab");
    if (tab && (v === "settings" || v === "sysadmin")) return { tab };
    // ?view=host&id=<host id>[&label=<name>] — a deep link straight to one host's
    // posture page, used by the Sysible Visualizer fleet map. The label is only a
    // heading hint while HostDetail loads; the id is what actually fetches.
    if (v === "host") {
      const id = q.get("id");
      if (id) return { id, label: q.get("label") || "" };
    }
  } catch { /* ignore */ }
  return null;
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

// Where the platform's sign-in lives, from wherever this console is mounted.
// Under the gateway the console is served at /controller/, so a bare "/login"
// is right — the gateway owns the origin — but this is written out rather than
// assumed so a console mounted somewhere else still points at the front door.
function platformLoginUrl() {
  try {
    const next = window.location.pathname + window.location.search;
    return "/login?next=" + encodeURIComponent(next);
  } catch {
    return "/login";
  }
}

// SLOP owns the accounts, so there is nothing for this console to ASK. Send the
// browser to the platform sign-in instead of showing it a page with a button on
// it: asked for directly — "why is there a controller login page that just says
// sign in on SLOP? It should redirect to the SLOP login if needed. It should
// all be the same."
//
// Once per page load. A console that cannot establish a session for an account
// that IS signed in to SLOP would otherwise bounce between the two forever,
// with the reason never on screen long enough to read — so the second time
// round it stops and says so.
const _SSO_BOUNCED = "sysible.sso.bounced";

function ssoBounceOnce() {
  try {
    if (window.sessionStorage.getItem(_SSO_BOUNCED)) return false;
    window.sessionStorage.setItem(_SSO_BOUNCED, "1");
  } catch {
    return false;   // no sessionStorage to remember with: show the card instead
  }
  try {
    window.location.assign(platformLoginUrl());
    return true;
  } catch {
    return false;
  }
}

// Shown when SLOP is this console's identity authority, the browser has no
// usable SSO session, AND sending it to the platform sign-in did not work or
// has already been tried. The console has no login of its own in that mode, so
// offering the form would strand the operator on a screen whose every answer is
// 403.
function SsoSignIn() {
  return (
    <div className="login-wrap">
      <div className="card" style={{ maxWidth: 460, padding: 24 }}>
        <h2 style={{ marginTop: 0 }}>Sign in at Sysible Linux Operations Platform</h2>
        <p className="faint">
          This console has no separate login — accounts live in SLOP. Sign in at the
          platform front door and open Controller from the portal.
        </p>
        <div className="row" style={{ gap: 8, marginTop: 14 }}>
          <a className="btn" href={platformLoginUrl()}>Go to sign-in</a>
          <button className="btn ghost" onClick={() => window.location.reload()}>Try again</button>
        </div>
        <p className="faint" style={{ fontSize: 12, marginTop: 14, marginBottom: 0 }}>
          Already signed in to SLOP and still seeing this? The controller could not
          establish a session for your account — check the web console log
          (<span className="mono">sysiblectl controller logs</span>) for the reason.
        </p>
      </div>
    </div>
  );
}


export default function App() {
  const [user, setUser] = useState(null);
  const [role, setRole] = useState("");
  const [checking, setChecking] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  // SLOP owns identity here, but this browser has no usable SSO session. The
  // login form would be a dead end — under SSO the login POST answers 403 to
  // every credential — so say what is actually wrong instead.
  const [ssoBlocked, setSsoBlocked] = useState(false);
  const [mustChange, setMustChange] = useState(false);
  // True when SLOP owns identity: accounts are managed in SLOP Administration,
  // so this console must not offer a second set of them.
  const [sso, setSso] = useState(false);
  // Bumped on every nav click so a stateful view (e.g. the tools grid) can reset
  // its own sub-page when its nav item is clicked again — see ToolRunner.
  const [navTick, setNavTick] = useState(0);
  const [view, setView] = useState(viewFromSearch); // null = dashboard; seeded from ?view=
  const [target, setTarget] = useState(targetFromSearch); // seeded from ?tab= for tabbed views
  const [history, setHistory] = useState([]); // breadcrumb stack of {view,target}
  const [edition, setEdition] = useState(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [skin, setSkin] = useState(() => {
    const s = localStorage.getItem("sysible_skin");
    return s && SKIN_IDS.has(s) ? s : "brand";
  });
  // Light / dark theme (persisted). Dark is the default brand look; "light" sets
  // data-theme on <html>, which the styles.css light block reads to flip surfaces.
  // The INITIAL value comes from whatever index.html already applied before
  // first paint — never from localStorage directly. index.html owns the one
  // "sysible-theme" key the whole suite shares (and the migration off the old
  // per-app keys); reading storage a second time here is how the Controller
  // came to disagree with SLOP about what the theme was.
  const [theme, setTheme] = useState(() => {
    try {
      const t = window.sysibleTheme?.get();
      if (t === "light" || t === "dark") return t;
    } catch { /* no-op */ }
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light" : "dark";
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
  useEffect(() => {
    // Hand it to the shared helper: it sets data-theme AND writes the one key
    // every Sysible console reads, so switching here switches SLOP and SLEP too.
    // Note it sets data-theme="dark" explicitly rather than removing the
    // attribute — styles.css only keys off [data-theme="light"], so dark is
    // unaffected, and an explicit value is what the other consoles render.
    try {
      window.sysibleTheme.set(theme);
    } catch {
      const root = document.documentElement;
      if (theme === "light") root.setAttribute("data-theme", "light");
      else root.removeAttribute("data-theme");
    }
  }, [theme]);

  // Another console changed it while this tab was open. index.html already
  // repainted <html>; this keeps React's idea of the theme (and the toggle's
  // label) in step with what is on screen.
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === "sysible-theme" && (e.newValue === "light" || e.newValue === "dark")) {
        setTheme(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

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
      .then((d) => { setUser(d.username); setRole(d.role || ""); setMustChange(!!d.must_change_password);
                     setSso(!!d.sso);
                     // Signed in: forget that we bounced, so a session that
                     // expires later gets the same one free trip to the
                     // platform sign-in rather than the explanatory card.
                     try { window.sessionStorage.removeItem(_SSO_BOUNCED); } catch { /* none */ } })
      .catch(async () => {
        setUser(null);
        // Which kind of "not signed in" is this? Under SLOP SSO the console has no
        // login of its own, so falling through to the form leaves the operator
        // typing credentials at a screen that can only ever answer 403.
        try {
          const m = await api.authMode();
          if (m && m.sso) {
            // Straight to the platform sign-in. The card is the fallback, not
            // the destination.
            if (!ssoBounceOnce()) setSsoBlocked(true);
            return;
          }
        } catch (e) {
          // Could not even ASK which mode this is. Falling through to the local
          // form is only right for a standalone controller; behind the gateway
          // it is the dead end this whole branch exists to avoid, and a reader
          // cannot tell the two apart from here. A redirected or HTML answer
          // means a gateway was in the way, which is answer enough.
          if (e && (e.status === 401 || e.gatewayError)) {
            if (!ssoBounceOnce()) setSsoBlocked(true);
            return;
          }
        }
        // Not signed in — on a brand-new controller with no admin yet, show the
        // first-run create-administrator screen instead of an unusable login form.
        try {
          const s = await api.setupRequired();
          setSetupRequired(!!(s && s.setup_required));
        } catch { /* fail safe → login */ }
      })
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
  // there (e.g. Dashboard → a tool → Back to Dashboard) without walking the
  // menus again. Used by both the sidebar and in-app "Fix in…"/drill links.
  const go = useCallback((v, t = null) => {
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
  // Fresh controller, no admin yet → first-run create-administrator screen.
  if (!user && ssoBlocked) return <SsoSignIn />;
  if (!user && setupRequired) {
    return <Setup onDone={(username, r) => { setSetupRequired(false); onLoggedIn(username, r, false); }} />;
  }
  if (!user) return <Login onLoggedIn={onLoggedIn} />;
  // A temporary password must be rotated before anything else is reachable.
  if (mustChange) {
    return <ForcePasswordChange username={user} onDone={() => setMustChange(false)} onLogout={onLogout} />;
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
          <Logo size={30} />
          <span className="rail-name">Sysible Controller</span>
        </div>

        <div className="rail-nav">
          {nav.map((n) => (
            n.external ? (
              <a key={n.key ?? "dash"} className="rail-item" href={apiUrl(n.external)}
                 target="_blank" rel="noopener"
                 title="Open the documentation in a new tab">
                <NavIcon name={n.icon} /><span>{n.label}</span>
                <span className="rail-ext" aria-hidden="true">↗</span>
              </a>
            ) : n.download ? (
              <a key={n.key ?? "dash"} className="rail-item" href={apiUrl(n.download)} download
                 title="Download the offline documentation">
                <NavIcon name={n.icon} /><span>{n.label}</span>
                <span className="rail-ext" aria-hidden="true">↓</span>
              </a>
            ) : (
              <a key={n.key ?? "dash"} href={hrefForView(n.key)}
                 className={"rail-item" + (view === n.key ? " active" : "")}
                 onClick={(e) => {
                   // Let the browser handle modifier-clicks (⌘/Ctrl/Shift/Alt) so
                   // "open in new tab/window" works; otherwise navigate in-app.
                   if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                   e.preventDefault();
                   setNavTick((t) => t + 1);
                   go(n.key, null);
                 }}>
                <NavIcon name={n.icon} /><span>{n.label}</span>
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
            <button className="btn ghost sm" title="Toggle light / dark"
              onClick={() => setTheme((t) => (t === "light" ? "dark" : "light"))}>
              {theme === "light" ? "☾ Dark" : "☀ Light"}</button>
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
          {isSuper && <HealthBanner />}
          {view === null && <Dashboard role={role} edition={edition}
            onOpen={(section, opts) => go(section, opts || null)} />}
          {view === "updates" && <Updates role={role} />}
          {!isAuditor && view === "schedules" && <Schedules />}
          {!isAuditor && view === "alerts" && <Alerts />}
          {view === "host" && <HostDetail hostId={target?.id} label={target?.label} canAct={!isAuditor}
            onBack={() => (history.length > 0 ? goBack() : go(null))}
            onOpen={(section, opts) => go(section, opts || null)} />}
          {isSuper && view === "hosts" && <HostEnrollment />}
          {isSuper && view === "migrate" && <Migrate />}
          {isSuper && view === "settings" && <Settings initialTab={target?.tab} sso={sso} />}
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
