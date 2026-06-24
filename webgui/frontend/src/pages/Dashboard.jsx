import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// The full desktop tool set, so the dashboard reflects the target
// (full-parity) surface. `tool` must match the `tool` string an action
// is registered under in webgui/actions.py. Tiles whose tool has no
// registered actions yet render as "coming soon" and are disabled - as
// actions are added server-side they light up automatically.
// Each tile carries an accent `color`; the glyph is rendered in that color
// on a faintly tinted chip (see the inline style below), matching the
// desktop's colored tiles. Glyphs are chosen to be monochrome and a text
// variation selector (︎) is appended to any with an emoji form so the
// browser keeps them monochrome and our color actually applies (otherwise
// e.g. a lock or gear renders as a fixed-color emoji).
const TILES = [
  { tool: "__terminal__", label: "Sysible Connect", icon: ">_", color: "#2ea0c9", always: true,
    desc: "Open a live SSH terminal to a host, in your browser." },
  { tool: "__files__", label: "File Transfer", icon: "⇅", color: "#13a89e", always: true,
    desc: "Upload files to a host or download files from it." },
  { tool: "Run Command", icon: "❯", color: "#7c5cff", desc: "Run an arbitrary shell command across selected hosts." },
  { tool: "User & Group Administration", icon: "◍", color: "#8b93a7", desc: "Create, lock, and manage users, shells, and groups." },
  { tool: "Service Management", icon: "⚙︎", color: "#7c5cff", desc: "Start, stop, restart, and inspect systemd services." },
  { tool: "System Health, Logs & Recovery", icon: "✚", color: "#d9544d", desc: "Health checks, logs, boot & recovery." },
  { tool: "Cron & Systemd Timers", icon: "◷", color: "#f5a623", desc: "View and manage cron jobs and systemd timers." },
  { tool: "Host Software Management", icon: "▤", color: "#2ea043", desc: "Install, update, and remove packages." },
  { tool: "Repository Management", icon: "▦", color: "#3b82f6", desc: "Manage package repositories." },
  { tool: "Network Management", icon: "≋", color: "#0ea5e9", desc: "Interfaces, routes, DNS, diagnostics." },
  { tool: "File System Management", icon: "▣", color: "#0891b2", desc: "Files, permissions, mounts, archives." },
  { tool: "Storage Administration", icon: "⛃", color: "#c08457", desc: "Disks, partitions, LVM, RAID, SMART." },
  { tool: "Firewall Administration", icon: "⬣", color: "#e0563f", desc: "firewalld / nftables / iptables." },
  { tool: "Security Administration", icon: "⚿", color: "#9aa3b2", desc: "SELinux, auditing, hardening." },
  { tool: "Backup & Recovery", icon: "⤓", color: "#2ea043", desc: "Back up and restore host data." },
  { tool: "Time Synchronization", icon: "◔", color: "#0ea5e9", desc: "NTP/chrony and time zone." },
  { tool: "Certificate Management", icon: "❖", color: "#e0568b", desc: "TLS certificates on managed hosts." },
  { tool: "Containers & VMs", icon: "◰", color: "#6366f1", desc: "Podman/Docker containers and libvirt VMs." },
  { tool: "Directory Services (Active Directory / LDAP)", icon: "⧉", color: "#0ea5e9", desc: "Join AD / LDAP." },
  { tool: "Distro Subscription & Licensing", icon: "✦", color: "#f5a623", desc: "RHSM, Ubuntu Pro, SUSE SCC." },
];

export default function Dashboard({ onOpenTool }) {
  const [available, setAvailable] = useState(null); // set of tool names with actions

  useEffect(() => {
    api
      .tools()
      .then((d) => setAvailable(new Set(d.tools.map((t) => t.tool))))
      .catch(() => setAvailable(new Set()));
  }, []);

  return (
    <div>
      <h2 className="page-title">Tools</h2>
      <p className="muted page-sub">
        Select a tool, choose target hosts, and run actions across your fleet.
      </p>
      <div className="tile-grid">
        {TILES.map((t) => {
          const ready = t.always || (available && available.has(t.tool));
          return (
            <button
              key={t.tool}
              className={`tile ${ready ? "" : "tile-disabled"}`}
              onClick={() => ready && onOpenTool(t.tool)}
              disabled={!ready}
            >
              <span
                className="tile-icon"
                style={ready ? { color: t.color, background: t.color + "22" } : undefined}
              >
                {t.icon}
              </span>
              <span className="tile-body">
                <span className="tile-title">{t.label || t.tool}</span>
                <span className="tile-desc">{t.desc}</span>
              </span>
              {!ready && <span className="tile-soon">soon</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
