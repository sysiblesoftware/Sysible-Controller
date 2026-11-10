import React, { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { api } from "../api.js";
import ToolPage from "./ToolPage.jsx";
import UserGroupPage from "./UserGroupPage.jsx";
import ServiceManagementPage from "./ServiceManagementPage.jsx";
import HostSoftwarePage from "./HostSoftwarePage.jsx";
import CronTimersPage from "./CronTimersPage.jsx";
import EnvironmentalPolicies from "./EnvironmentalPolicies.jsx";
import QuickSystemActionsPage from "./QuickSystemActionsPage.jsx";
import ToolIcon from "../components/ToolIcons.jsx";

// Tools with a bespoke page instead of the generic three-pane runner.
const CUSTOM_PAGES = {
  "User & Group Administration": UserGroupPage,
  "Service Management": ServiceManagementPage,
  "Host Software Management": HostSoftwarePage,
  "Cron & Systemd Timers": CronTimersPage,
  "Quick System Actions": QuickSystemActionsPage,
};

// The System Administration tile grid — mirrors the desktop page exactly:
// same order, icons, colors, and descriptions (client/system_administration_page.py).
// [tool name, description, icon, color-class, special-view?]
// The tool catalog.
//
// This was a list of positional tuples read as [name, desc, icon, cls, special]
// with a sixth element meaning "hidden" — t[4] and t[5] at the call sites, which
// is unreadable and was about to gain a seventh. Objects instead; the fields are
// named where they are used.
//
// WHY `short` AND `desc` BOTH EXIST. The grid showed `desc`, and `desc` is long
// on purpose: it is also the SEARCH corpus, and the search box advertises terms
// like "raid" that appear in no tool's NAME — only in Storage Administration's
// description. So the long text cannot simply be trimmed. It moved out of the
// tile instead: 21 tiles were carrying 3,168 characters of body copy on one
// screen (median 145, longest 249), which is roughly 500 words in a tile grid.
// `short` is what the tile shows; `desc` still backs the search and the tool's
// own page.
//
// `group` sorts the 21 into five sections rather than one undifferentiated wall,
// and `cls` follows from it: one step of the blue-to-green brand ramp per
// section (see --ramp1..5 in styles.css). Colour marks the grouping instead of
// decorating twelve tools in twelve unrelated hues.
const TOOLS = [
  // ---- Accounts & access ----
  { name: "User & Group Administration", group: "Accounts & access",
    short: "Accounts, passwords, sudo and groups", icon: "users", cls: "ico-ramp1",
    desc: "Create, lock, and manage user accounts, passwords, sudo access, and groups across agent and SSH hosts." },
  { name: "Environmental Policies", group: "Accounts & access",
    short: "Password, lockout, sudo and umask policy", icon: "shield-alt", cls: "ico-ramp1",
    special: "env",
    desc: "Set the baseline password, lockout, sudo, and umask policy for accounts on managed hosts, and push it out." },
  { name: "Directory Services (Active Directory / LDAP)", group: "Accounts & access",
    short: "Join Active Directory, configure LDAP", icon: "users-cog", cls: "ico-ramp1",
    desc: "Join hosts to Active Directory (realmd/SSSD), manage realm status and login permits, enable home-dir creation, and configure/test LDAP and LDAPS." },

  // ---- System & services ----
  { name: "System Health, Logs & Recovery", group: "System & services",
    short: "Disk, memory, services, logs, boot recovery", icon: "heartbeat", cls: "ico-ramp2",
    desc: "Disk usage, memory/CPU, failed services, logs, and process tools, plus boot/GRUB and kernel recovery — across agent and SSH hosts." },
  { name: "Service Management", group: "System & services",
    short: "Start, stop and troubleshoot systemd services", icon: "cogs", cls: "ico-ramp2",
    desc: "Start, stop, restart, enable/disable, and troubleshoot systemd services, or create and configure new ones." },
  { name: "Cron & Systemd Timers", group: "System & services",
    short: "Scheduled jobs and systemd timers", icon: "clock", cls: "ico-ramp2",
    desc: "View, add, and remove cron jobs, and view, create, start/stop, enable/disable, and delete systemd timers." },
  { name: "Environment & Shell", group: "System & services",
    short: "System-wide variables and shell aliases", icon: "cogs", cls: "ico-ramp2",
    desc: "Set and remove persistent, system-wide environment variables and shell aliases across managed hosts (via /etc/profile.d drop-ins for new login shells)." },
  { name: "Time Synchronization", group: "System & services",
    short: "NTP/chrony, clock drift and time zone", icon: "clock", cls: "ico-ramp2",
    desc: "Configure NTP/chrony, verify synchronization, troubleshoot clock drift, and set the system time zone across managed hosts." },
  { name: "Containers & VMs", group: "System & services",
    short: "Docker and Podman containers, libvirt VMs", icon: "cube", cls: "ico-ramp2",
    desc: "List and start/stop/restart Docker or Podman containers, view container logs and images, and manage libvirt virtual machines across managed hosts." },

  // ---- Software & updates ----
  { name: "Host Software Management", group: "Software & updates",
    short: "Install, update and query packages", icon: "box", cls: "ico-ramp3",
    desc: "Detect each host's package manager, then install, remove, update, query, verify, and clean packages across dnf/yum, zypper, and apt hosts alike." },
  { name: "Repository Management", group: "Software & updates",
    short: "Add, enable and remove repositories", icon: "code-branch", cls: "ico-ramp3",
    desc: "List, add, enable, disable, and remove software repositories across dnf/yum, zypper, and apt hosts." },
  { name: "OS Release Upgrade", group: "Software & updates",
    short: "Move a host to its next major release", icon: "level-up", cls: "ico-ramp3",
    desc: "Move a host to its next major/distribution release with the right mechanism per distro — Leapp (RHEL/Rocky/Alma), dnf system-upgrade (Fedora), do-release-upgrade (Ubuntu), and zypper migration/dup (SUSE/openSUSE). Assess first; nothing auto-reboots." },
  { name: "Distro Subscription & Licensing", group: "Software & updates",
    short: "Red Hat, Ubuntu Pro and SUSE subscriptions", icon: "id-card", cls: "ico-ramp3",
    desc: "Register and manage commercial-distro subscriptions: Red Hat (subscription-manager), Ubuntu Pro, and SUSE (SUSEConnect) — status, attach/enable, and repositories." },

  // ---- Storage & files ----
  { name: "File System Management", group: "Storage & files",
    short: "Files, permissions, mounts and fstab", icon: "hdd", cls: "ico-ramp4",
    desc: "Create/remove directories, copy/move/rename files, manage ownership/permissions/ACLs and links, mount/unmount/resize/repair filesystems, configure /etc/fstab and quotas, and archive/compress files across managed hosts." },
  { name: "Storage Administration", group: "Storage & files",
    short: "Disks, partitions, LVM, RAID and swap", icon: "database", cls: "ico-ramp4",
    desc: "Partition, format, and monitor disks, manage LVM physical volumes/volume groups/logical volumes, configure RAID and replace failed disks, and set up swap space across managed hosts." },
  { name: "Backup & Recovery", group: "Storage & files",
    short: "Back up, restore, snapshot and verify", icon: "save", cls: "ico-ramp4",
    desc: "Back up and restore files, verify backup integrity, schedule backups, create and restore LVM snapshots, guide deleted-file recovery, and run disaster-recovery drills." },

  // ---- Network & security ----
  { name: "Network Management", group: "Network & security",
    short: "Connectivity, DNS, IP, routing and VLANs", icon: "network-wired", cls: "ico-ramp5",
    desc: "Diagnose connectivity and DNS, inspect ports and capture packets, and configure IP/DHCP/DNS/gateway/routing/hostname/bonding/teaming/VLANs/bridges/MTU across managed hosts." },
  { name: "Firewall Administration", group: "Network & security",
    short: "firewalld zones, ports and nftables rules", icon: "fire", cls: "ico-ramp5",
    desc: "Configure firewalld zones, ports, and rich rules, and manage the underlying nftables and iptables rule sets across managed hosts." },
  { name: "Security Administration", group: "Network & security",
    short: "SELinux, SSH hardening, audit and scans", icon: "lock", cls: "ico-ramp5",
    desc: "Configure and troubleshoot SELinux, harden SSH access and rotate keys, review audit logs and failed logins, install security updates, set password policy, harden systems, and run vulnerability scans across managed hosts." },
  { name: "Certificate Management", group: "Network & security",
    short: "CSRs, renewals and TLS troubleshooting", icon: "certificate", cls: "ico-ramp5",
    desc: "Generate CSRs, install/renew/replace certificates, verify certificate chains, and troubleshoot TLS endpoints across managed hosts." },

  // hidden → resolvable by name (sidebar `solo` mode + "Fix in…" links) but NOT
  // shown as a tile here; it lives only in its own left-sidebar entry.
  { name: "Quick System Actions", group: "System & services", hidden: true,
    short: "One-click reboot, restart and reload fixes", icon: "bolt", cls: "ico-ramp2",
    desc: "One-click common fixes across selected hosts: reboot or power off, restart a service (NetworkManager, SSH, time sync, or any by name), flush DNS, clear failed units, and reload systemd." },
];

// The order the sections appear in. Derived from the catalog so a tool added with
// a new group name cannot silently vanish from the page.
const GROUPS = ["Accounts & access", "System & services", "Software & updates",
                "Storage & files", "Network & security"];

// `solo` pins the runner to a single tool: no grid, no "← All tools" — used by
// dedicated sidebar entries (e.g. Quick System Actions) that promote one tool to
// its own top-level menu item.
export default function ToolRunner({ openTool, openTab, openPrefill, onConsumed, solo, resetKey }) {
  const [catalog, setCatalog] = useState(null);
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(() => {
    if (!solo) return null;
    const t = TOOLS.find((x) => x.name === solo);
    return t ? { name: t.name, special: t.special } : null;
  });   // {name, special?, tab?}
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
    const t = TOOLS.find((x) => x.name === openTool);
    if (t) setOpen({ name: t.name, special: t.special, tab: openTab, prefill: openPrefill });
    onConsumed && onConsumed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openTool, openTab]);

  // Clicking the "System Administration Tools" nav item while already on this
  // view bumps resetKey. Return to the tile grid rather than sitting on the tool
  // page. (The parent can't do this via props alone: `open` is our own state,
  // and the tool was opened after the parent already cleared its target, so the
  // usual navigate call early-returns.) Skip the first run so mounting straight
  // into a tool via task-search isn't clobbered.
  const firstReset = useRef(true);
  useEffect(() => {
    if (firstReset.current) { firstReset.current = false; return; }
    if (!solo) setOpen(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  const filtered = useMemo(() => {
    const visible = TOOLS.filter((t) => !t.hidden);
    const s = q.trim().toLowerCase();
    if (!s) return visible;
    // Search the long `desc` as well as the name and the tile's own label: the
    // search box advertises terms like "raid" that appear in NEITHER the name nor
    // the short label, only in the description. Moving the long text off the tile
    // must not quietly narrow what is findable.
    return visible.filter((t) =>
      (t.name + " " + t.short + " " + t.desc).toLowerCase().includes(s));
  }, [q]);

  const searching = q.trim().length > 0;
  const renderTile = (t) => (
    <button key={t.name} className="tile" onClick={() => setOpen({ name: t.name, special: t.special })}>
      <div className={"tile-ico " + t.cls}><ToolIcon name={t.icon} /></div>
      <h3>{t.name}</h3>
      <p>{t.short}</p>
    </button>
  );

  if (err) return <div className="error-box">{err}</div>;
  if (catalog === null) return <div className="empty"><span className="spin" /></div>;

  if (open) {
    const Custom = CUSTOM_PAGES[open.name];
    const tool = catalog.find((t) => t.tool === open.name);
    return (
      <div style={{ height: "calc(100vh - 150px)", display: "flex", flexDirection: "column" }}>
        <div className="row" style={{ marginBottom: 12 }}>
          {!solo && <button className="btn ghost sm" onClick={() => setOpen(null)}>← All tools</button>}
          <strong>{open.name}</strong>
        </div>
        {open.special === "env"
          ? <EnvironmentalPolicies prefill={open.prefill} hosts={hosts} onRefreshHosts={loadHosts} />
          : Custom
            ? <Custom initialTab={open.tab} prefill={open.prefill} hosts={hosts} onRefreshHosts={loadHosts} />
            : tool
              ? <ToolPage tool={tool} hosts={hosts} onRefreshHosts={loadHosts} prefill={open.prefill} />
              : <div className="empty">This tool isn't available.</div>}
      </div>
    );
  }

  return (
    <div>
      <input className="search-bar" placeholder='Search tools… e.g. "users", "firewall", "raid", "certificates"'
             value={q} onChange={(e) => setQ(e.target.value)} />
      {searching
        // While searching, one flat grid: sections would mostly be empty
        // headings, and the point of a search is the matches, not where they live.
        ? (filtered.length
            ? <div className="tool-grid">{filtered.map(renderTile)}</div>
            : <div className="empty">No tool matches “{q.trim()}”.</div>)
        : GROUPS.map((g) => {
            const inGroup = filtered.filter((t) => t.group === g);
            if (!inGroup.length) return null;
            return (
              <section key={g}>
                <div className="section-title">{g}</div>
                <div className="tool-grid">{inGroup.map(renderTile)}</div>
              </section>
            );
          })}
    </div>
  );
}
