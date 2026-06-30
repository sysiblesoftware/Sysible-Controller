import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Per-host posture / compliance drill-down. Read-only: renders the full
// category breakdown from /api/host-posture/{id} (see cmd_posture_snapshot),
// with pass/warn/fail coloring on the signals that have a clear good/bad state.

const C = { good: "#4ec07a", warn: "#e0a83a", bad: "#e06c6c", none: "#7a7a7a" };

// Display sections: each pulls one or more gather categories together under a
// friendly title. `labels` renames known keys; anything else is humanized so
// no gathered field is silently dropped.
const SECTIONS = [
  { title: "Operating System", cats: ["os", "sub", "reboot"], labels: {
    "os.distro": "Distribution ID", "os.name": "Release", "os.version": "Version",
    "os.kernel": "Kernel", "os.arch": "Architecture", "os.uptime_s": "Uptime",
    "os.boot_epoch": "Booted", "sub.rhsm": "RHEL subscription", "sub.ubuntu_pro": "Ubuntu Pro",
    "reboot.required": "Reboot required" } },
  { title: "Security & Hardening", cats: ["mac", "sec", "fw"], labels: {
    "mac.selinux": "SELinux", "mac.apparmor": "AppArmor", "sec.fips": "FIPS mode",
    "sec.aslr": "ASLR (randomize_va_space)", "sec.secureboot": "Secure Boot",
    "sec.auditd": "auditd", "sec.fail2ban": "fail2ban", "sec.usb_storage": "USB storage",
    "fw.backend": "Firewall backend", "fw.active": "Firewall active" } },
  { title: "Users & Accounts", cats: ["users"], labels: {
    "users.uid0": "UID-0 accounts", "users.uid0_count": "UID-0 count",
    "users.empty_pw": "Empty-password accounts", "users.empty_pw_count": "Empty-password count",
    "users.dup_uid": "Duplicate UIDs", "users.dup_gid": "Duplicate GIDs",
    "users.admins": "Admin group members", "users.pw_max_days": "Password max age (days)",
    "users.pw_min_len": "Password min length", "users.locked_count": "Locked accounts",
    "users.svc_login_shells": "Service accounts with login shell", "users.pw_complexity": "Password complexity" } },
  { title: "SSH", cats: ["ssh"], labels: {
    "ssh.permit_root_login": "PermitRootLogin", "ssh.password_auth": "Password auth",
    "ssh.pubkey_auth": "Pubkey auth", "ssh.max_auth_tries": "MaxAuthTries",
    "ssh.idle_timeout": "ClientAliveInterval", "ssh.banner": "Banner",
    "ssh.allow_users": "AllowUsers", "ssh.allow_groups": "AllowGroups",
    "ssh.x11_forwarding": "X11 forwarding", "ssh.weak_ciphers": "Weak ciphers",
    "ssh.weak_macs": "Weak MACs", "ssh.weak_kex": "Weak key exchange", "ssh.version": "Version" } },
  { title: "Filesystem", cats: ["fs", "mount"], labels: {
    "fs.disk_pct": "Disk usage (worst)", "fs.inode_pct": "Inode usage (worst)",
    "fs.suid_sgid_count": "SUID/SGID files", "fs.world_writable_count": "World-writable files",
    "fs.unowned_count": "Unowned files" } },
  { title: "Time Synchronization", cats: ["time"], labels: {
    "time.synced": "Clock synchronized", "time.ntp_service": "NTP service",
    "time.timezone": "Timezone", "time.source": "Time source", "time.offset": "Last offset" } },
  { title: "Logging", cats: ["log"], labels: {
    "log.rsyslog": "rsyslog", "log.journald": "journald", "log.remote_forward": "Remote forwarding",
    "log.logrotate": "logrotate", "log.var_log_mb": "/var/log size (MB)" } },
  { title: "Networking", cats: ["net"], labels: {
    "net.listen_count": "Listening sockets", "net.listen_ports": "Listening ports",
    "net.dns": "DNS servers", "net.gateway": "Default gateway", "net.ip_forward": "IP forwarding",
    "net.ipv6_disabled": "IPv6 disabled", "net.hostname": "Hostname" } },
  { title: "TLS Certificates", cats: ["cert"], labels: {
    "cert.count": "Certificates found", "cert.expiring_30d": "Expiring < 30 days",
    "cert.self_signed": "Self-signed", "cert.nearest_days": "Nearest expiry (days)" } },
  { title: "Services", cats: ["svc"], labels: {
    "svc.failed_count": "Failed units", "svc.failed": "Failed unit names", "svc.zombies": "Zombie processes" } },
  { title: "Hardware", cats: ["hw"], labels: {
    "hw.mem_pct": "Memory usage", "hw.swap_pct": "Swap usage", "hw.cores": "CPU cores",
    "hw.raid": "RAID arrays", "hw.smart": "SMART health" } },
  { title: "Virtualization & Containers", cats: ["virt", "cont"], labels: {
    "virt.type": "Platform", "virt.guest_agent": "Guest agent", "cont.docker": "Docker version",
    "cont.docker_running": "Docker running", "cont.docker_privileged": "Privileged containers",
    "cont.podman": "Podman version", "cont.podman_running": "Podman running" } },
  { title: "Identity & Directory", cats: ["ad"], labels: {
    "ad.domain": "AD domain", "ad.sssd": "sssd", "ad.kerberos": "Kerberos" } },
  { title: "Performance & Misc", cats: ["perf", "misc"], labels: {
    "perf.load1": "Load (1m)", "perf.oom": "OOM events", "misc.last_login": "Last login",
    "misc.cron_jobs": "Cron jobs (/etc/cron.d)", "misc.timers": "systemd timers" } },
];

function humanize(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtUptime(s) {
  const n = parseInt(s, 10);
  if (!isFinite(n)) return s;
  const d = Math.floor(n / 86400), h = Math.floor((n % 86400) / 3600), m = Math.floor((n % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function pctStatus(v) {
  const n = parseInt(v, 10);
  if (!isFinite(n)) return "none";
  return n >= 90 ? "bad" : n >= 75 ? "warn" : "good";
}

// Status for a key/value, or "none" if there's no meaningful good/bad sense.
function evalStatus(fullKey, v) {
  const s = (v == null ? "" : String(v)).trim().toLowerCase();
  switch (fullKey) {
    case "reboot.required": return s === "1" ? "bad" : "good";
    case "fw.active": return s === "1" ? "good" : "bad";
    case "mac.selinux": return s === "enforcing" ? "good" : s === "permissive" ? "warn" : s === "disabled" ? "warn" : "none";
    case "mac.apparmor": return s === "enabled" ? "good" : s === "disabled" ? "warn" : "none";
    case "sec.auditd": return s === "active" ? "good" : "none";
    case "sec.fail2ban": return s === "active" ? "good" : s === "absent" ? "none" : "warn";
    case "sec.fips": return s === "1" ? "good" : "none";
    case "sec.usb_storage": return s === "blocked" ? "good" : "none";
    case "ssh.permit_root_login": return s === "yes" ? "bad" : (s === "no" ? "good" : (s ? "warn" : "none"));
    case "ssh.password_auth": return s === "yes" ? "warn" : s === "no" ? "good" : "none";
    case "ssh.weak_ciphers": case "ssh.weak_macs": case "ssh.weak_kex": return s ? "bad" : "good";
    case "users.uid0_count": return parseInt(s, 10) > 1 ? "bad" : "good";
    case "users.empty_pw_count": return parseInt(s, 10) > 0 ? "bad" : "good";
    case "users.dup_uid": case "users.dup_gid": return s ? "bad" : "good";
    case "users.pw_complexity": return s === "configured" ? "good" : "warn";
    case "fs.disk_pct": case "fs.inode_pct": case "hw.mem_pct": case "hw.swap_pct": return pctStatus(s);
    case "time.synced": return ["yes", "true", "1"].includes(s) ? "good" : ["no", "false", "0"].includes(s) ? "bad" : "none";
    case "cert.expiring_30d": return parseInt(s, 10) > 0 ? "warn" : "good";
    case "cert.nearest_days": { const n = parseInt(s, 10); return isFinite(n) ? (n < 30 ? "warn" : "good") : "none"; }
    case "svc.failed_count": return parseInt(s, 10) > 0 ? "warn" : "good";
    case "svc.zombies": return parseInt(s, 10) > 0 ? "warn" : "good";
    case "hw.smart": return s === "ok" ? "good" : s === "failing" ? "bad" : "none";
    case "perf.oom": return parseInt(s, 10) > 0 ? "warn" : "good";
    case "cont.docker_privileged": return parseInt(s, 10) > 0 ? "warn" : "good";
    default: return "none";
  }
}

function fmtValue(fullKey, v) {
  if (v === "" || v == null) return "—";
  if (fullKey === "os.uptime_s") return fmtUptime(v);
  if (fullKey === "os.boot_epoch") { const d = new Date(parseInt(v, 10) * 1000); return isNaN(d) ? v : d.toLocaleString(); }
  if (fullKey === "fw.active" || fullKey === "reboot.required" || fullKey === "net.ip_forward" ||
      fullKey === "log.remote_forward" || fullKey === "net.ipv6_disabled") {
    return v === "1" ? "yes" : v === "0" ? "no" : v;
  }
  if (["fs.disk_pct", "fs.inode_pct", "hw.mem_pct", "hw.swap_pct"].includes(fullKey)) return `${v}%`;
  return v;
}

function Row({ fullKey, label, value }) {
  const st = evalStatus(fullKey, value);
  return (
    <div className="spread" style={{ padding: "5px 0", borderBottom: "1px solid var(--border)", gap: 12 }}>
      <span className="faint" style={{ fontSize: 13, whiteSpace: "nowrap" }}>{label}</span>
      <span style={{ fontSize: 13, textAlign: "right", wordBreak: "break-word",
                     display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
        {st !== "none" && <span className="dot" style={{ background: C[st] }} />}
        <span style={{ color: st === "bad" ? C.bad : st === "warn" ? C.warn : "var(--text)", fontWeight: st === "bad" ? 600 : 400 }}>
          {fmtValue(fullKey, value)}
        </span>
      </span>
    </div>
  );
}

function SectionCard({ section, posture }) {
  // Collect every present key within this section's categories: known keys keep
  // their friendly label and stated order; unknown ones are appended humanized.
  const rows = [];
  const seen = new Set();
  for (const fk of Object.keys(section.labels)) {
    const [cat, key] = fk.split(".");
    const val = (posture[cat] || {})[key];
    if (val !== undefined) { rows.push({ fullKey: fk, label: section.labels[fk], value: val }); seen.add(fk); }
  }
  for (const cat of section.cats) {
    for (const [key, val] of Object.entries(posture[cat] || {})) {
      const fk = `${cat}.${key}`;
      if (!seen.has(fk)) rows.push({ fullKey: fk, label: humanize(key), value: val });
    }
  }
  if (rows.length === 0) return null;
  return (
    <div className="card" style={{ padding: "12px 14px" }}>
      <div className="section-title" style={{ marginBottom: 6 }}>{section.title}</div>
      {rows.map((r) => <Row key={r.fullKey} fullKey={r.fullKey} label={r.label} value={r.value} />)}
    </div>
  );
}

export default function HostDetail({ hostId, label, onBack }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (!hostId) { setErr("No host selected."); return; }
    setLoading(true); setErr("");
    api.hostPosture(hostId)
      .then((d) => setData(d))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [hostId]);
  useEffect(() => { load(); }, [load]);

  const posture = data && data.posture;

  return (
    <div>
      <div className="spread" style={{ marginBottom: 14 }}>
        <button className="btn ghost sm" onClick={onBack}>← Back to dashboard</button>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          {data && data.environment && <span className="faint" style={{ fontSize: 12 }}>{data.environment}</span>}
          <button className="btn ghost sm" onClick={load} disabled={loading}>
            {loading ? <span className="spin" /> : "Refresh posture"}
          </button>
        </div>
      </div>

      {err && <div className="error-box">{err}</div>}

      {!posture ? (
        <div className="empty" style={{ padding: 24 }}>
          {loading ? `Gathering posture for ${label || "host"}…`
                   : (data && data.error) ? `Could not gather posture: ${data.error}`
                   : "No posture data."}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
                      gap: 12, alignItems: "start" }}>
          {SECTIONS.map((s) => <SectionCard key={s.title} section={s} posture={posture} />)}
        </div>
      )}
    </div>
  );
}
