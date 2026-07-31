import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import SuppressMenu from "../components/SuppressMenu.jsx";
import { findSupp, describeSupp } from "../suppress.js";
import { hypervisorBadge, hypervisorActionWarning, isHypervisor } from "../hypervisor.js";

// Map a posture row key to the dashboard suppression signal key where one
// exists, so suppressing a finding here stays consistent with the compliance
// strip. Rows without a mapping suppress under their own posture key.
const SUPP_KEY = {
  "ssh.permit_root_login": "ssh_root_login",
  "fw.active": "firewall_disabled",
  "mac.selinux": "mac_not_enforcing", "mac.apparmor": "mac_not_enforcing",
  "reboot.required": "reboot_required",
  "time.synced": "time_unsynced",
  "users.uid0_count": "risky_accounts", "users.empty_pw_count": "risky_accounts",
  "cert.expiring_30d": "cert_expiring", "cert.nearest_days": "cert_expiring",
  "svc.failed_count": "failed_units",
  "fs.disk_pct": "disk_critical",
};

// Per-host posture / compliance drill-down. Read-only: renders the full
// category breakdown from /api/host-posture/{id} (see cmd_posture_snapshot),
// with pass/warn/fail coloring on the signals that have a clear good/bad state.

const C = { good: "#4ec07a", warn: "#e0a83a", bad: "#e06c6c", none: "#7a7a7a", supp: "#6c7fa8" };

// Display sections: each pulls one or more gather categories together under a
// friendly title. `labels` renames known keys; anything else is humanized so
// no gathered field is silently dropped.
const SECTIONS = [
  { title: "Operating System", cats: ["os", "sub", "reboot"], labels: {
    "os.distro": "Distribution ID", "os.name": "Release", "os.version": "Version",
    "os.kernel": "Kernel", "os.arch": "Architecture", "os.uptime_s": "Uptime",
    "os.boot_epoch": "Booted", "sub.rhsm": "RHEL subscription", "sub.ubuntu_pro": "Ubuntu Pro",
    "os.eol": "End-of-life / unsupported OS",
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
    "net.ipv4": "IPv4 addresses", "net.ipv6": "IPv6 addresses",
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
    case "os.eol": return s === "yes" ? "bad" : s === "no" ? "good" : "none";
    case "fw.active": return s === "1" ? "good" : "bad";
    case "mac.selinux": return s === "enforcing" ? "good" : s === "permissive" ? "warn" : s === "disabled" ? "warn" : "none";
    case "mac.apparmor": return s === "enabled" ? "good" : s === "disabled" ? "warn" : "none";
    case "sec.auditd": return s === "active" ? "good" : "none";
    case "sec.fail2ban": return s === "active" ? "good" : s === "absent" ? "none" : "warn";
    case "sec.fips": return s === "1" ? "good" : "none";
    case "sec.usb_storage": return s === "blocked" ? "good" : "none";
    case "ssh.permit_root_login": return s === "yes" ? "bad" : (s === "no" ? "good" : (s ? "warn" : "none"));
    case "ssh.password_auth": return s === "yes" ? "warn" : s === "no" ? "good" : "none";
    case "ssh.weak_ciphers": case "ssh.weak_macs": case "ssh.weak_kex": return s ? "warn" : "good";
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
  if (fullKey === "os.eol") return v === "yes" ? "EOL / unsupported" : v === "no" ? "Supported" : v;
  if (fullKey === "os.uptime_s") return fmtUptime(v);
  if (fullKey === "os.boot_epoch") { const d = new Date(parseInt(v, 10) * 1000); return isNaN(d) ? v : d.toLocaleString(); }
  if (fullKey === "fw.active" || fullKey === "reboot.required" || fullKey === "net.ip_forward" ||
      fullKey === "log.remote_forward" || fullKey === "net.ipv6_disabled") {
    return v === "1" ? "yes" : v === "0" ? "no" : v;
  }
  if (["fs.disk_pct", "fs.inode_pct", "hw.mem_pct", "hw.swap_pct"].includes(fullKey)) return `${v}%`;
  return v;
}

// Remediation per finding. Two kinds:
//   kind "run"  — a safe fix we can execute right here (reboot).
//   kind "tool" — jump to the System Administration tool that fixes it, so the
//                 operator lands on the exact screen (TLS cert → Certificate
//                 Management, weak SSH → Security Administration, etc.).
// `tool` names MUST match the tiles in ToolRunner.jsx exactly. Only shown when
// the row is actually in a warn/bad state — no action on a healthy signal.
const ACTIONS = {
  "reboot.required": { kind: "run", label: "Reboot host", confirm: "Reboot this host now?",
                       vmDisruptive: true,   // warn first if this host is a VM host
                       refreshAfter: true, run: (hostId) => api.fleet("reboot", [hostId]) },
  // No in-place fix for an EOL OS — send the operator to the release-upgrade tool
  // with this host pre-selected so they can assess + upgrade it.
  "os.eol": { kind: "tool", tool: "OS Release Upgrade",
    prefill: (posture, hostId) => ({ host: hostId }) },
  "cert.expiring_30d": { kind: "tool", tool: "Certificate Management" },
  "cert.nearest_days": { kind: "tool", tool: "Certificate Management" },
  "fw.active": { kind: "tool", tool: "Firewall Administration" },
  "mac.selinux": { kind: "tool", tool: "Security Administration" },
  "mac.apparmor": { kind: "tool", tool: "Security Administration" },
  "sec.auditd": { kind: "tool", tool: "Security Administration" },
  "sec.fail2ban": { kind: "tool", tool: "Security Administration" },
  "ssh.permit_root_login": { kind: "tool", tool: "Security Administration" },
  "ssh.password_auth": { kind: "tool", tool: "Security Administration" },
  "ssh.weak_ciphers": { kind: "tool", tool: "Security Administration" },
  "ssh.weak_macs": { kind: "tool", tool: "Security Administration" },
  "ssh.weak_kex": { kind: "tool", tool: "Security Administration" },
  "time.synced": { kind: "tool", tool: "Time Synchronization" },
  "svc.failed_count": { kind: "tool", tool: "Quick System Actions",
    // Carry the failed unit's name + this host over to Quick System Actions so
    // its "Service (by name)" field and target are already filled in.
    prefill: (posture, hostId) => {
      const names = String((posture.svc || {}).failed || "").split(/[\s,]+/).filter(Boolean);
      return { name: names[0] || "", host: hostId };
    } },
  "fs.disk_pct": { kind: "tool", tool: "Storage Administration" },
  "fs.inode_pct": { kind: "tool", tool: "Storage Administration" },
  "users.empty_pw_count": { kind: "tool", tool: "User & Group Administration" },
  "users.uid0_count": { kind: "tool", tool: "User & Group Administration" },
  "users.pw_complexity": { kind: "tool", tool: "Environmental Policies",
    // Land on Environmental Policies with THIS host already checked as the push
    // target, so the operator can adjust password quality and push in one go.
    prefill: (posture, hostId) => ({ host: hostId }) },
};

// Plain-language "why it matters + how to fix" for each finding that can flag,
// so the alert is self-explanatory ON THE ROW — the operator shouldn't have to
// already know what "Password auth: yes" implies or open another screen to learn
// the remediation. Keep each to one or two short sentences and name the concrete
// directive/command, so it's actionable even without opening the linked tool.
const EXPLAIN = {
  "reboot.required": {
    why: "A kernel or core library was updated but the system is still running the old version — those security fixes aren't active until a reboot.",
    fix: "Reboot this host during a maintenance window (the “Reboot host” button here, or `systemctl reboot`)." },
  "os.eol": {
    why: "This OS release no longer gets security updates, so newly found vulnerabilities will never be patched on it.",
    fix: "Plan an in-place release upgrade or migrate to a supported release — “Fix” opens the OS Release Upgrade tool with this host selected." },
  "fw.active": {
    why: "No host firewall is active, so every listening service is reachable from anywhere the network permits.",
    fix: "Enable ufw/firewalld and allow only the ports this host needs — open Firewall Administration." },
  "mac.selinux": {
    why: "SELinux isn't enforcing, so its mandatory-access-control confinement of a compromised service is switched off.",
    fix: "Once you've confirmed no policy denials break the workload, set enforcing: `setenforce 1` and SELINUX=enforcing in /etc/selinux/config." },
  "mac.apparmor": {
    why: "AppArmor is disabled, so its per-program confinement isn't limiting what a compromised service can reach.",
    fix: "Enable it and load the distro profiles: `systemctl enable --now apparmor`." },
  "sec.fail2ban": {
    why: "fail2ban isn't running, so repeated failed logins (e.g. SSH brute-force) aren't being blocked.",
    fix: "Install fail2ban with an sshd jail and start it: `systemctl enable --now fail2ban`." },
  "ssh.permit_root_login": {
    why: "SSH allows direct root login: one guessed or leaked root password is a full takeover, and root logins can't be tied to a person.",
    fix: "Set `PermitRootLogin no` in /etc/ssh/sshd_config and reload sshd; log in as a normal user and use sudo." },
  "ssh.password_auth": {
    why: "SSH accepts password logins, which can be brute-forced or phished — key-based logins can't.",
    fix: "Confirm your admins can log in with SSH keys, then set `PasswordAuthentication no` in /etc/ssh/sshd_config and reload sshd." },
  "ssh.weak_ciphers": {
    why: "sshd offers encryption ciphers now considered weak, lowering the bar for a capable network attacker.",
    fix: "Pin strong ciphers in /etc/ssh/sshd_config, e.g. `Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com`, then reload sshd." },
  "ssh.weak_macs": {
    why: "sshd offers MAC (integrity) algorithms now considered weak, e.g. hmac-sha1, weakening tamper protection of the session.",
    fix: "Pin strong MACs in /etc/ssh/sshd_config, e.g. `MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com`, then reload sshd." },
  "ssh.weak_kex": {
    why: "sshd offers key-exchange algorithms now considered weak.",
    fix: "Pin strong KEX in /etc/ssh/sshd_config, e.g. `KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org`, then reload sshd." },
  "users.uid0_count": {
    why: "More than one account has UID 0 — each is effectively root, a common backdoor that also destroys accountability.",
    fix: "Give each admin their own non-zero UID account with sudo, and remove the extra UID-0 entries from /etc/passwd." },
  "users.empty_pw_count": {
    why: "One or more accounts have an empty password, so anyone can log in as them with no credential at all.",
    fix: "Set a password or lock each account (`passwd -l <user>`) — open User & Group Administration." },
  "users.dup_uid": {
    why: "Two usernames share a UID, so the system can't tell them apart — file ownership and audit trails become ambiguous.",
    fix: "Assign each account a unique UID and re-chown its files." },
  "users.dup_gid": {
    why: "Two groups share a GID, so group-based permissions can't be distinguished.",
    fix: "Assign each group a unique GID." },
  "users.pw_complexity": {
    why: "No password-quality policy is configured, so users can set trivially weak passwords.",
    fix: "Configure pam_pwquality (min length/complexity) — “Fix” opens Environmental Policies to push a policy to this host." },
  "fs.disk_pct": {
    why: "A filesystem is nearly full; at 100% anything writing to it will fail or crash.",
    fix: "Free space or grow the volume — open Storage Administration to see the worst mount." },
  "fs.inode_pct": {
    why: "A filesystem is nearly out of inodes; it will refuse new files even with free space remaining.",
    fix: "Delete large numbers of small/stale files, or recreate the FS with more inodes — open Storage Administration." },
  "time.synced": {
    why: "The clock isn't synchronized; skew breaks TLS validation, Kerberos, token expiry and log correlation.",
    fix: "Enable an NTP client (`systemctl enable --now chronyd`, or systemd-timesyncd) — open Time Synchronization." },
  "cert.expiring_30d": {
    why: "A TLS certificate expires within 30 days; once it lapses, clients reject the service.",
    fix: "Renew or re-issue the certificate before the date — open Certificate Management." },
  "cert.nearest_days": {
    why: "The soonest-expiring TLS certificate is close to lapsing.",
    fix: "Renew it ahead of the expiry date — open Certificate Management." },
  "svc.failed_count": {
    why: "One or more systemd units are in the failed state, so something that should be running isn't.",
    fix: "Inspect with `systemctl --failed` and `journalctl -u <unit>`, then restart/repair — “Fix” opens Quick System Actions with the unit filled in." },
  "svc.zombies": {
    why: "Zombie processes mean a parent isn't reaping its children — usually a buggy service.",
    fix: "Find the parent (`ps -el | grep Z`) and restart it." },
  "hw.smart": {
    why: "A disk reports SMART health as failing — it may fail soon and take data with it.",
    fix: "Back up now and replace the drive; confirm with `smartctl -a /dev/<disk>`." },
  "perf.oom": {
    why: "The kernel OOM-killer has fired, meaning the host ran out of memory and killed processes.",
    fix: "Add RAM/swap or cut the top consumers' memory use; review with `journalctl -k | grep -i oom`." },
  "cont.docker_privileged": {
    why: "One or more containers run with --privileged, which effectively grants them root on the host.",
    fix: "Re-run those containers without --privileged, granting only the specific capabilities they actually need." },
};

function RowAction({ action, hostId, posture, hyp, onOpen, onRefreshSoon }) {
  const [state, setState] = useState("idle"); // idle | busy | ok | err
  const [msg, setMsg] = useState("");
  if (action.kind === "tool") {
    return (
      <button className="btn ghost sm" style={{ whiteSpace: "nowrap" }}
              onClick={() => onOpen && onOpen("sysadmin", {
                tool: action.tool, tab: action.tab,
                prefill: action.prefill ? action.prefill(posture || {}, hostId) : undefined })}
              title={`Open ${action.tool}`}>
        Fix in {action.tool} →
      </button>
    );
  }
  const doRun = () => {
    if (state === "busy") return;
    // A VM-disruptive action (reboot) on a hypervisor host takes every guest down
    // with it — surface that in the confirm before anything happens.
    const vmWarn = action.vmDisruptive ? hypervisorActionWarning(hyp) : "";
    const confirmMsg = vmWarn ? `${vmWarn}\n\n${action.confirm || "Continue?"}` : action.confirm;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setState("busy"); setMsg("");
    action.run(hostId)
      .then((r) => {
        const res = (r && r.results && r.results[0]) || {};
        const ok = res.ok || res.code === 0 || !!(r && r.action);
        setState(ok ? "ok" : "err");
        setMsg(ok ? "" : (res.error || res.stderr || "failed"));
        if (ok && action.refreshAfter && onRefreshSoon) onRefreshSoon();
      })
      .catch((e) => { setState("err"); setMsg(String(e.message || e)); });
  };
  const color = state === "ok" ? C.good : state === "err" ? C.bad : C.warn;
  return (
    <button className="btn sm" style={{ whiteSpace: "nowrap", borderColor: color, color }}
            onClick={doRun} disabled={state === "busy" || !hostId} title={msg || action.label}>
      {state === "busy" ? "…" : state === "ok" ? "✓ requested" : state === "err" ? "✕ failed" : action.label}
    </button>
  );
}

function Row({ fullKey, label, value, hostId, posture, hyp, host, env, boot, supps, onOpen, canAct, onRefreshSoon, onSuppressed, onChanged }) {
  const st = evalStatus(fullKey, value);
  const flagged = st === "bad" || st === "warn";
  const suppKey = SUPP_KEY[fullKey] || fullKey;
  // Is this (still-alerting) finding suppressed? Then show it muted/gray rather
  // than amber/red — it's flagging but the operator has accepted/silenced it.
  const supp = flagged ? findSupp(supps, { key: suppKey, host, env, bootEpoch: boot }) : null;
  const action = (flagged && canAct && !supp) ? ACTIONS[fullKey] : null;
  const dotColor = supp ? C.supp : C[st];
  const valColor = supp ? C.supp : (st === "bad" ? C.bad : st === "warn" ? C.warn : "var(--text)");
  // An unsuppressed bad/warn finding gets a tinted band with a colored left
  // accent so it's noticeable at a glance instead of blending into the list.
  // Non-flagged rows keep a transparent accent of the same width to stay aligned.
  const accent = (flagged && !supp) ? (st === "bad" ? C.bad : C.warn) : null;
  const rowBg = st === "bad" ? "rgba(224,108,108,0.12)" : "rgba(224,168,58,0.10)";
  return (
    <div style={{
      padding: "5px 8px",
      borderBottom: "1px solid var(--border)",
      borderLeft: `3px solid ${accent || "transparent"}`,
      background: accent ? rowBg : "transparent",
      borderRadius: accent ? "0 4px 4px 0" : 0,
    }}>
      <div className="spread" style={{ gap: 12, alignItems: "baseline" }}>
        <span className="faint" style={{ fontSize: 13, whiteSpace: "nowrap" }}>{label}</span>
        <span style={{ fontSize: 13, textAlign: "right", minWidth: 0, overflowWrap: "anywhere",
                       display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
          {st !== "none" && <span className="dot" style={{ background: dotColor, flex: "0 0 auto" }} />}
          <span style={{ color: valColor, fontWeight: st === "bad" && !supp ? 600 : 400 }}>
            {fmtValue(fullKey, value)}
          </span>
        </span>
      </div>
      {flagged && !supp && EXPLAIN[fullKey] && (
        <div style={{ marginTop: 5, fontSize: 12, lineHeight: 1.45, color: "var(--text)", opacity: 0.85 }}>
          <div>
            <span style={{ color: st === "bad" ? C.bad : C.warn, fontWeight: 600 }}>Why it matters: </span>
            {EXPLAIN[fullKey].why}
          </div>
          <div style={{ marginTop: 2 }}>
            <span style={{ color: C.good, fontWeight: 600 }}>How to fix: </span>
            {EXPLAIN[fullKey].fix}
          </div>
        </div>
      )}
      {flagged && supp && (
        <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8, marginTop: 5 }}>
          <span style={{ fontSize: 11, color: C.supp, border: `1px solid ${C.supp}`, borderRadius: 10, padding: "0 8px" }}
                title={describeSupp(supp)}>⊘ suppressed · {describeSupp(supp)}</span>
          {canAct && <button className="btn ghost sm" title="Remove suppression"
                  onClick={async () => { try { await api.removeSuppression(supp.id); onChanged && onChanged(); } catch { /* ignore */ } }}>
            Un-suppress</button>}
        </div>
      )}
      {flagged && canAct && !supp && (
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 5 }}>
          {action && <RowAction action={action} hostId={hostId} posture={posture} hyp={hyp} onOpen={onOpen} onRefreshSoon={onRefreshSoon} />}
          <SuppressMenu ctx={{ key: suppKey, host, env, bootEpoch: boot }} onDone={onSuppressed} />
        </div>
      )}
    </div>
  );
}

function SectionCard({ section, posture, hyp, hostId, host, env, boot, supps, onOpen, canAct, onRefreshSoon, onSuppressed, onChanged }) {
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
      {rows.map((r) => <Row key={r.fullKey} fullKey={r.fullKey} label={r.label} value={r.value}
                            hostId={hostId} posture={posture} hyp={hyp} host={host} env={env} boot={boot} supps={supps}
                            onOpen={onOpen} canAct={canAct} onRefreshSoon={onRefreshSoon}
                            onSuppressed={onSuppressed} onChanged={onChanged} />)}
    </div>
  );
}

const CRIT_COLOR = { low: "#7a7a7a", normal: "#7a7a7a", high: "#e0a83a", critical: "#e06c6c" };

// Operator metadata for one host: criticality / owner / tags / notes, keyed by
// host name (applies to agent + SSH hosts alike). View + inline edit.
function HostMetaPanel({ name, canAct }) {
  const [meta, setMeta] = useState(null);
  const [crits, setCrits] = useState(["low", "normal", "high", "critical"]);
  const [edit, setEdit] = useState(false);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!name) return;
    const fallback = { tags: [], owner: "", notes: "", criticality: "normal" };
    api.hostMeta()
      .then((d) => { setCrits(d.criticality || crits); setMeta((d.meta || {})[name] || fallback); })
      .catch(() => setMeta(fallback));
  }, [name]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!meta) return null;
  const crit = meta.criticality || "normal";
  const startEdit = () => { setDraft({ ...meta, tagsText: (meta.tags || []).join(", ") }); setErr(""); setEdit(true); };
  async function save() {
    setBusy(true); setErr("");
    const body = {
      tags: draft.tagsText.split(",").map((t) => t.trim()).filter(Boolean),
      owner: draft.owner, notes: draft.notes, criticality: draft.criticality,
    };
    try { setMeta(await api.setHostMeta(name, body)); setEdit(false); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="spread" style={{ marginBottom: edit ? 8 : 0 }}>
        <strong>Host info</strong>
        {canAct && !edit && <button className="btn ghost sm" onClick={startEdit}>Edit</button>}
      </div>
      {!edit ? (
        <div className="row" style={{ gap: 16, flexWrap: "wrap", fontSize: 13, alignItems: "center" }}>
          <span><span className="dot" style={{ background: CRIT_COLOR[crit] }} /> {crit}</span>
          <span className="faint">owner: {meta.owner || "—"}</span>
          <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {(meta.tags || []).length
              ? meta.tags.map((t) => <span key={t} className="badge" style={{ fontSize: 11 }}>{t}</span>)
              : <span className="faint">no tags</span>}
          </span>
          {meta.notes && <span className="faint" style={{ fontSize: 12, flexBasis: "100%" }}>{meta.notes}</span>}
        </div>
      ) : (
        <div style={{ display: "grid", gap: 8, maxWidth: 520 }}>
          <label className="field"><span>Criticality</span>
            <select value={draft.criticality} onChange={(e) => setDraft({ ...draft, criticality: e.target.value })}>
              {crits.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></label>
          <label className="field"><span>Owner</span>
            <input value={draft.owner} onChange={(e) => setDraft({ ...draft, owner: e.target.value })}
                   placeholder="e.g. platform-team or alice" /></label>
          <label className="field"><span>Tags <span className="faint">(comma-separated)</span></span>
            <input value={draft.tagsText} onChange={(e) => setDraft({ ...draft, tagsText: e.target.value })}
                   placeholder="e.g. web, edge, pci" /></label>
          <label className="field"><span>Notes</span>
            <textarea value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                      rows={2} style={{ width: "100%" }} placeholder="Anything worth remembering about this host" /></label>
          {err && <div className="error-box">{err}</div>}
          <div className="row" style={{ gap: 8 }}>
            <button className="btn sm" onClick={save} disabled={busy}>{busy ? <span className="spin" /> : "Save"}</button>
            <button className="btn ghost sm" onClick={() => setEdit(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

// Module-level posture cache (survives unmount): navigating to a "Fix in…" tool
// and back re-mounts this component, and a fresh gather is a live host round-trip.
// We seed instantly from the last result for this host, then revalidate in the
// background (stale-while-revalidate) so "back" is seamless, not a spinner.
const _POSTURE_CACHE = new Map();   // hostId -> { data, ts }

export default function HostDetail({ hostId, label, onBack, onOpen, canAct = true }) {
  const cached = hostId ? _POSTURE_CACHE.get(hostId) : null;
  const [data, setData] = useState(cached ? cached.data : null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [suppMsg, setSuppMsg] = useState(false);
  const [supps, setSupps] = useState([]);
  const loadSupps = useCallback(() => {
    api.suppressions().then((d) => setSupps(d.suppressions || [])).catch(() => {});
  }, []);
  useEffect(() => { loadSupps(); }, [loadSupps]);

  const [rebooting, setRebooting] = useState(false);
  const pollRef = useRef(null);
  const dataRef = useRef(null);
  useEffect(() => { dataRef.current = data; }, [data]);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const store = useCallback((d) => {
    if (hostId && d && d.posture) _POSTURE_CACHE.set(hostId, { data: d, ts: Date.now() });
    setData(d);
  }, [hostId]);

  const load = useCallback(() => {
    if (!hostId) { setErr("No host selected."); return; }
    // Only show the full-page spinner when we have nothing cached to show.
    if (!_POSTURE_CACHE.get(hostId)) setLoading(true);
    setErr("");
    api.hostPosture(hostId)
      .then((d) => store(d))
      .catch((e) => { if (!_POSTURE_CACHE.get(hostId)) setErr(e.message); })
      .finally(() => setLoading(false));
  }, [hostId, store]);
  // Seed from cache immediately on host change, then revalidate.
  useEffect(() => {
    const c = hostId ? _POSTURE_CACHE.get(hostId) : null;
    setData(c ? c.data : null);
    load();
  }, [hostId, load]);

  // After a reboot the host goes down and comes back. Poll posture and ALWAYS
  // swap in the latest successful gather (so the box never sits on stale uptime),
  // and stop the "rebooting" banner once we've confirmed the reboot — either we
  // saw the host go offline and return, its boot time changed, or its uptime is
  // now tiny. Runs up to 5 min.
  const refreshAfterReboot = useCallback(() => {
    if (!hostId) return;
    const baseBoot = (((dataRef.current || {}).posture || {}).os || {}).boot_epoch;
    setRebooting(true);
    if (pollRef.current) clearInterval(pollRef.current);
    const t0 = Date.now();
    let sawDown = false;
    const tick = async () => {
      if (Date.now() - t0 > 300000) { clearInterval(pollRef.current); setRebooting(false); return; }
      let d = null;
      try { d = await api.hostPosture(hostId); } catch { d = null; }
      const reachable = !!(d && d.posture);
      if (!reachable) { sawDown = true; return; }   // down mid-reboot — keep polling
      setData(d);                                    // reachable → show current, never stale
      const os = d.posture.os || {};
      const rebootReq = (d.posture.reboot || {}).required;
      const up = os.uptime_s != null ? Number(os.uptime_s) : null;
      const rebooted = sawDown ||
        (baseBoot && os.boot_epoch && os.boot_epoch !== baseBoot) ||
        rebootReq === "0" ||
        (up != null && isFinite(up) && up < 900);
      if (rebooted) { clearInterval(pollRef.current); setRebooting(false); }
    };
    pollRef.current = setInterval(tick, 8000);
    setTimeout(tick, 4000);   // first check soon, not after a full interval
  }, [hostId]);

  const posture = data && data.posture;
  // Hypervisor context (role + running-guest count) attached to the posture
  // response from the heartbeat health reading; drives the badge below and the
  // reboot confirm warning.
  const hyp = data ? { hypervisor: data.hypervisor, vms: data.vms, label } : null;

  return (
    <div>
      <div className="spread" style={{ marginBottom: 14 }}>
        <button className="btn ghost sm" onClick={onBack}>← Back to dashboard</button>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          {isHypervisor(hyp) && (
            <span title="This host runs virtual machines — rebooting or powering it off takes its guests down."
                  style={{ fontSize: 11.5, fontWeight: 600, color: "#e0a83a",
                           border: "1px solid #e0a83a", borderRadius: 10, padding: "1px 9px" }}>
              🖥 {hypervisorBadge(hyp)}
            </span>
          )}
          {data && data.environment && <span className="faint" style={{ fontSize: 12 }}>{data.environment}</span>}
          <button className="btn ghost sm" onClick={load} disabled={loading}>
            {loading ? <span className="spin" /> : "Refresh posture"}
          </button>
        </div>
      </div>

      {err && <div className="error-box">{err}</div>}
      {suppMsg && (
        <div className="ok-text" style={{ fontSize: 12.5, marginBottom: 10, display: "flex", gap: 8, alignItems: "center" }}>
          ✓ Finding suppressed — it’ll drop from the dashboard donut and “needs attention”.
          <button className="btn ghost sm" onClick={() => setSuppMsg(false)}>Dismiss</button>
        </div>
      )}

      {label && <HostMetaPanel name={label} canAct={canAct} />}

      {rebooting && (
        <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "9px 12px",
                      marginBottom: 12, fontSize: 12.5, display: "flex", alignItems: "center", gap: 8 }}>
          <span className="spin" /> Reboot requested — waiting for {label || "the host"} to check back in;
          this will refresh automatically once it's back.
        </div>
      )}

      {posture && (posture.meta || {}).privileged === "0" && (
        <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "9px 12px",
                      marginBottom: 12, fontSize: 12.5, color: "var(--text-dim)" }}>
          ⚠ Posture for this host was gathered <strong>without root</strong> (e.g. an SSH host whose
          login user isn't root). Root-only checks — empty passwords, effective SSH config, SUID
          scans — may read blank, so an absence of findings here is <strong>not</strong> a clean bill.
        </div>
      )}

      {!posture ? (
        <div className="empty" style={{ padding: 24 }}>
          {loading ? `Gathering posture for ${label || "host"}…`
                   : (data && data.error) ? `Could not gather posture: ${data.error}`
                   : "No posture data."}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
                      gap: 12, alignItems: "start" }}>
          {SECTIONS.map((s) => <SectionCard key={s.title} section={s} posture={posture} hyp={hyp}
                                            hostId={hostId} host={label} env={data && data.environment}
                                            boot={(posture.os || {}).boot_epoch} supps={supps}
                                            onOpen={onOpen} canAct={canAct} onRefreshSoon={refreshAfterReboot}
                                            onSuppressed={() => { setSuppMsg(true); loadSupps(); }}
                                            onChanged={loadSupps} />)}
        </div>
      )}
    </div>
  );
}
