import React, { useMemo, useState } from "react";

// Inline SVG icons (stroke uses currentColor so the tint classes color them).
const I = {
  server: <><rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><line x1="7" y1="7" x2="7" y2="7"/><line x1="7" y1="17" x2="7" y2="17"/></>,
  cog: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></>,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></>,
  globe: <><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/></>,
  grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  stream: <><path d="M4 6h16M4 12h16M4 18h10"/></>,
};

function Icon({ name }) {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {I[name]}
    </svg>
  );
}

// (title, desc, icon, color-class, section-id-or-null, superuserOnly)
const TILES = [
  ["Sysible Controller Host Enrollment", "Download the agent bundle and manage the enrolled host fleet.", "server", "ico-teal", "hosts", true],
  ["Sysible Controller Settings", "Manage dashboard administrators, password policy, the controller's address/port, and the audit log.", "cog", "ico-slate", "settings", true],
  ["Sysible Connect", "Pop-out SSH/agent terminals with file upload & download, search, and saved output, plus one-click SSH enrollment.", "terminal", "ico-purple", "connect", false],
  ["Webserver Portal Configuration", "Run the host-facing portal for agent downloads and file transfers.", "globe", "ico-coral", "portal", true],
  ["System Administration", "All host-management tools — users, services, storage, firewall, network, and more — across agent and SSH hosts.", "grid", "ico-amber", "sysadmin", false],
  ["Live Activity & Logs", "Live, attributed feed of who did what across the fleet, plus the controller's own log.", "stream", "ico-sky", "live", true],
];

export default function Dashboard({ role, onOpen }) {
  const [q, setQ] = useState("");
  const isSuper = role === "superuser";

  const tiles = useMemo(
    () => TILES.filter(([,,,,, su]) => !su || isSuper),
    [isSuper]
  );
  const filtered = tiles.filter(([t, d]) => {
    const s = q.trim().toLowerCase();
    return !s || t.toLowerCase().includes(s) || d.toLowerCase().includes(s);
  });

  const BUILT = new Set(["hosts", "sysadmin", "connect"]);

  return (
    <div>
      <input
        className="search-bar"
        placeholder='Search for a task, e.g. "create a user" or "add a repository"…'
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="tiles">
        {filtered.map(([title, desc, icon, cls, section]) => (
          <button
            key={section}
            className="tile"
            onClick={() => BUILT.has(section) ? onOpen(section) : null}
            style={!BUILT.has(section) ? { opacity: 0.6, cursor: "default" } : undefined}
          >
            <div className={"tile-ico " + cls}><Icon name={icon} /></div>
            <h3>{title}</h3>
            <p>{desc}{!BUILT.has(section) ? "  (coming soon to the web console)" : ""}</p>
          </button>
        ))}
      </div>
    </div>
  );
}
