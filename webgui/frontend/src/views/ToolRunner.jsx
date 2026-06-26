import React, { useEffect, useState, useCallback, useMemo } from "react";
import { api } from "../api.js";
import ToolPage from "./ToolPage.jsx";
import UserGroupPage from "./UserGroupPage.jsx";
import ServiceManagementPage from "./ServiceManagementPage.jsx";
import HostSoftwarePage from "./HostSoftwarePage.jsx";
import EnvironmentalPolicies from "./EnvironmentalPolicies.jsx";
import ToolIcon from "../components/ToolIcons.jsx";

// Tools with a bespoke page instead of the generic three-pane runner.
const CUSTOM_PAGES = {
  "User & Group Administration": UserGroupPage,
  "Service Management": ServiceManagementPage,
  "Host Software Management": HostSoftwarePage,
};

// The System Administration tile grid — mirrors the desktop page exactly:
// same order, icons, colors, and descriptions (client/system_administration_page.py).
// [tool name, description, icon, color-class, special-view?]
const TOOLS = [
  ["User & Group Administration", "Create, lock, and manage user accounts, passwords, sudo access, and groups across agent and SSH hosts.", "users", "ico-slate"],
  ["System Health, Logs & Recovery", "Disk usage, memory/CPU, failed services, logs, and process tools, plus boot/GRUB and kernel recovery — across agent and SSH hosts.", "heartbeat", "ico-green"],
  ["Service Management", "Start, stop, restart, enable/disable, and troubleshoot systemd services, or create and configure new ones.", "cogs", "ico-purple"],
  ["Environmental Policies", "Set the baseline password, lockout, sudo, and umask policy for accounts on managed hosts, and push it out.", "shield-alt", "ico-coral", "env"],
  ["Cron & Systemd Timers", "View, add, and remove cron jobs, and view, create, start/stop, enable/disable, and delete systemd timers.", "clock", "ico-amber"],
  ["Host Software Management", "Detect each host's package manager, then install, remove, update, query, verify, and clean packages across dnf/yum, zypper, and apt hosts alike.", "box", "ico-teal"],
  ["Repository Management", "List, add, enable, disable, and remove software repositories across dnf/yum, zypper, and apt hosts.", "code-branch", "ico-rose"],
  ["Network Management", "Diagnose connectivity and DNS, inspect ports and capture packets, and configure IP/DHCP/DNS/gateway/routing/hostname/bonding/teaming/VLANs/bridges/MTU across managed hosts.", "network-wired", "ico-sky"],
  ["File System Management", "Create/remove directories, copy/move/rename files, manage ownership/permissions/ACLs and links, mount/unmount/resize/repair filesystems, configure /etc/fstab and quotas, and archive/compress files across managed hosts.", "hdd", "ico-indigo"],
  ["Storage Administration", "Partition, format, and monitor disks, manage LVM physical volumes/volume groups/logical volumes, configure RAID and replace failed disks, and set up swap space across managed hosts.", "database", "ico-copper"],
  ["Firewall Administration", "Configure firewalld zones, ports, and rich rules, and manage the underlying nftables and iptables rule sets across managed hosts.", "fire", "ico-crimson"],
  ["Security Administration", "Configure and troubleshoot SELinux, harden SSH access and rotate keys, review audit logs and failed logins, install security updates, set password policy, harden systems, and run vulnerability scans across managed hosts.", "lock", "ico-graphite"],
  ["Backup & Recovery", "Back up and restore files, verify backup integrity, schedule backups, create and restore LVM snapshots, guide deleted-file recovery, and run disaster-recovery drills.", "save", "ico-teal"],
  ["Time Synchronization", "Configure NTP/chrony, verify synchronization, troubleshoot clock drift, and set the system time zone across managed hosts.", "clock", "ico-sky"],
  ["Certificate Management", "Generate CSRs, install/renew/replace certificates, verify certificate chains, and troubleshoot TLS endpoints across managed hosts.", "certificate", "ico-rose"],
  ["Containers & VMs", "List and start/stop/restart Docker or Podman containers, view container logs and images, and manage libvirt virtual machines across managed hosts.", "cube", "ico-indigo"],
  ["Directory Services (Active Directory / LDAP)", "Join hosts to Active Directory (realmd/SSSD), manage realm status and login permits, enable home-dir creation, and configure/test LDAP and LDAPS.", "users-cog", "ico-sky"],
  ["Distro Subscription & Licensing", "Register and manage commercial-distro subscriptions: Red Hat (subscription-manager), Ubuntu Pro, and SUSE (SUSEConnect) — status, attach/enable, and repositories.", "id-card", "ico-amber"],
];

export default function ToolRunner({ openTool, openTab, onConsumed }) {
  const [catalog, setCatalog] = useState(null);
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(null);   // {name, special?, tab?}
  const [q, setQ] = useState("");

  const loadHosts = useCallback(() => {
    api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    api.tools().then((d) => setCatalog(d.tools || [])).catch((e) => setErr(e.message));
    loadHosts();
  }, [loadHosts]);

  // Task search asked to jump straight to a specific tool (+ optional tab).
  useEffect(() => {
    if (!openTool) return;
    const t = TOOLS.find(([name]) => name === openTool);
    if (t) setOpen({ name: t[0], special: t[4], tab: openTab });
    onConsumed && onConsumed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openTool, openTab]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return TOOLS;
    return TOOLS.filter(([t, d]) => t.toLowerCase().includes(s) || d.toLowerCase().includes(s));
  }, [q]);

  if (err) return <div className="error-box">{err}</div>;
  if (catalog === null) return <div className="empty"><span className="spin" /></div>;

  if (open) {
    const Custom = CUSTOM_PAGES[open.name];
    const tool = catalog.find((t) => t.tool === open.name);
    return (
      <div style={{ height: "calc(100vh - 150px)", display: "flex", flexDirection: "column" }}>
        <div className="row" style={{ marginBottom: 12 }}>
          <button className="btn ghost sm" onClick={() => setOpen(null)}>← All tools</button>
          <strong>{open.name}</strong>
        </div>
        {open.special === "env"
          ? <EnvironmentalPolicies hosts={hosts} onRefreshHosts={loadHosts} />
          : Custom
            ? <Custom initialTab={open.tab} hosts={hosts} onRefreshHosts={loadHosts} />
            : tool
              ? <ToolPage tool={tool} hosts={hosts} onRefreshHosts={loadHosts} />
              : <div className="empty">This tool isn't available.</div>}
      </div>
    );
  }

  return (
    <div>
      <input className="search-bar" placeholder='Search tools… e.g. "users", "firewall", "raid", "certificates"'
             value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="tool-grid">
        {filtered.map(([name, desc, icon, cls, special]) => (
          <button key={name} className="tile" onClick={() => setOpen({ name, special })}>
            <div className={"tile-ico " + cls}><ToolIcon name={icon} /></div>
            <h3>{name}</h3>
            <p>{desc}</p>
          </button>
        ))}
      </div>
    </div>
  );
}
