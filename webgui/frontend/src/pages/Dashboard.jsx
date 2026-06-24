import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// The full desktop tool set, so the dashboard reflects the target
// (full-parity) surface. `tool` must match the `tool` string an action
// is registered under in webgui/actions.py. Tiles whose tool has no
// registered actions yet render as "coming soon" and are disabled - as
// actions are added server-side they light up automatically.
const TILES = [
  { tool: "__terminal__", label: "Sysible Connect", icon: "▮", always: true,
    desc: "Open a live SSH terminal to a host, in your browser." },
  { tool: "Run Command", icon: "»_", desc: "Run an arbitrary shell command across selected hosts." },
  { tool: "User & Group Administration", icon: "◴", desc: "Create, lock, and manage users, shells, and groups." },
  { tool: "Service Management", icon: "⚙", desc: "Start, stop, restart, and inspect systemd services." },
  { tool: "System Health, Logs & Recovery", icon: "❤", desc: "Health checks, logs, boot & recovery." },
  { tool: "Cron & Systemd Timers", icon: "◷", desc: "View and manage cron jobs and systemd timers." },
  { tool: "Host Software Management", icon: "▤", desc: "Install, update, and remove packages." },
  { tool: "Repository Management", icon: "▦", desc: "Manage package repositories." },
  { tool: "Network Management", icon: "≋", desc: "Interfaces, routes, DNS, diagnostics." },
  { tool: "File System Management", icon: "▣", desc: "Files, permissions, mounts, archives." },
  { tool: "Storage Administration", icon: "⛁", desc: "Disks, partitions, LVM, RAID, SMART." },
  { tool: "Firewall Administration", icon: "🜂", desc: "firewalld / nftables / iptables." },
  { tool: "Security Administration", icon: "🔒", desc: "SELinux, auditing, hardening." },
  { tool: "Backup & Recovery", icon: "🖫", desc: "Back up and restore host data." },
  { tool: "Time Synchronization", icon: "◷", desc: "NTP/chrony and time zone." },
  { tool: "Certificate Management", icon: "🎖", desc: "TLS certificates on managed hosts." },
  { tool: "Containers & VMs", icon: "◰", desc: "Podman/Docker containers and libvirt VMs." },
  { tool: "Directory Services (Active Directory / LDAP)", icon: "⛓", desc: "Join AD / LDAP." },
  { tool: "Distro Subscription & Licensing", icon: "🎫", desc: "RHSM, Ubuntu Pro, SUSE SCC." },
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
              <span className="tile-icon">{t.icon}</span>
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
