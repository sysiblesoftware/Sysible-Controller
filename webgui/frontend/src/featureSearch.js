// Task search index — maps plain-language tasks to a destination, mirroring
// the desktop client/feature_search.py registry. Each entry: a section to open
// (hosts/settings/connect/portal/sysadmin/live) and, for System Administration,
// the tool name (and optional User & Group tab).
export const REGISTRY = [
  { title: "Host Enrollment", section: "hosts",
    keywords: ["enroll host", "add host", "new host", "download agent", "agent bundle", "managed hosts", "fleet", "host enrollment", "curl", "enrollment token", "disenroll"] },
  { title: "Sysible Controller Settings", section: "settings",
    keywords: ["settings", "administrators", "add administrator", "admin password policy", "audit log", "controller address", "controller port", "change my password", "controller settings", "version", "license"] },
  { title: "Sysible Connect", section: "connect",
    keywords: ["ssh", "ssh terminal", "remote terminal", "console", "connect to host", "environment tag", "sysible connect", "upload to host", "download from host", "file transfer", "terminal", "run command", "run script", "run a script", "ad hoc", "ad-hoc", "fleet command", "run on all hosts", "shell command", "reboot all", "power off", "restart agent", "check in", "ping"] },
  { title: "Webserver Portal Configuration", section: "portal",
    keywords: ["portal", "reset portal password", "portal credentials", "portal port", "login history", "portal sessions", "host operator login"] },
  // User & Group Administration tabs
  { title: "Create User", section: "sysadmin", tool: "User & Group Administration", tab: "create",
    keywords: ["create a user", "create user", "add user", "new user", "new account", "create account", "add a user"] },
  { title: "Reset / Set Password", section: "sysadmin", tool: "User & Group Administration", tab: "password",
    keywords: ["reset password", "change password", "set password", "generate password", "force password reset", "password aging"] },
  { title: "Account Status / Lock / Sudo", section: "sysadmin", tool: "User & Group Administration", tab: "account",
    keywords: ["lock account", "unlock account", "disable account", "account expiration", "account status", "kill sessions", "terminate user", "grant sudo", "remove sudo", "set shell", "delete user"] },
  { title: "Group Membership", section: "sysadmin", tool: "User & Group Administration", tab: "groups",
    keywords: ["add to group", "group membership", "manage groups", "sudo group", "add user to group", "create group", "delete group"] },
  { title: "User & Group Reports", section: "sysadmin", tool: "User & Group Administration", tab: "reports",
    keywords: ["user report", "group report", "sudoers report", "privileged users audit"] },
  { title: "System Health, Logs & Recovery", section: "sysadmin", tool: "System Health, Logs & Recovery",
    keywords: ["disk usage", "memory usage", "cpu usage", "view logs", "search logs", "failed services", "large files", "uptime", "process list", "system health", "boot failure", "analyze boot", "grub", "rebuild grub", "rescue mode", "initramfs", "kernel parameters", "manage kernels", "crashes"] },
  { title: "Service Management", section: "sysadmin", tool: "Service Management",
    keywords: ["restart service", "start service", "stop service", "enable service", "disable service", "create service", "systemd service", "troubleshoot service", "service logs"] },
  { title: "Environmental Policies", section: "sysadmin", tool: "Environmental Policies",
    keywords: ["password policy", "lockout policy", "sudo policy", "umask policy", "environmental policy", "baseline policy"] },
  { title: "Cron & Systemd Timers", section: "sysadmin", tool: "Cron & Systemd Timers",
    keywords: ["cron job", "add cron job", "systemd timer", "scheduled task", "schedule a task"] },
  { title: "Host Software Management", section: "sysadmin", tool: "Host Software Management",
    keywords: ["install package", "remove package", "update package", "query package", "verify package", "clean package cache", "upgrade packages", "search package"] },
  { title: "Repository Management", section: "sysadmin", tool: "Repository Management",
    keywords: ["add repository", "add a repo", "enable repository", "disable repository", "remove repository", "repo to all hosts"] },
  { title: "Network Management", section: "sysadmin", tool: "Network Management",
    keywords: ["network", "ip address", "dns", "gateway", "route", "ping", "traceroute", "vlan", "bond", "team", "bridge", "mtu", "dhcp", "static ip", "tcpdump", "capture packets"] },
  { title: "File System Management", section: "sysadmin", tool: "File System Management",
    keywords: ["file system", "directory", "permissions", "chmod", "chown", "acl", "mount", "unmount", "nfs", "cifs", "smb", "fstab", "quota", "archive", "compress", "symlink", "copy file", "move file"] },
  { title: "Storage Administration", section: "sysadmin", tool: "Storage Administration",
    keywords: ["disk", "partition", "format", "lvm", "volume group", "logical volume", "physical volume", "raid", "swap", "mount disk", "smart", "resize volume"] },
  { title: "Firewall Administration", section: "sysadmin", tool: "Firewall Administration",
    keywords: ["firewall", "firewalld", "open port", "close port", "zone", "rich rule", "nftables", "iptables", "ufw", "default zone"] },
  { title: "Security Administration", section: "sysadmin", tool: "Security Administration",
    keywords: ["selinux", "ssh hardening", "harden ssh", "audit", "failed logins", "security updates", "harden", "vulnerability scan", "lynis", "rkhunter", "rotate keys", "authorized keys", "pubkey", "sysctl"] },
  { title: "Backup & Recovery", section: "sysadmin", tool: "Backup & Recovery",
    keywords: ["backup", "back up files", "restore files", "verify backup", "backup schedule", "schedule backup", "snapshot", "create snapshot", "restore snapshot", "recover deleted files", "disaster recovery", "dr test"] },
  { title: "Time Synchronization", section: "sysadmin", tool: "Time Synchronization",
    keywords: ["ntp", "configure ntp", "chrony", "configure chrony", "time sync", "verify synchronization", "clock drift", "time zone", "timezone", "set time zone"] },
  { title: "Certificate Management", section: "sysadmin", tool: "Certificate Management",
    keywords: ["certificate", "csr", "generate csr", "install certificate", "ssl certificate", "renew certificate", "replace expired certificate", "certificate chain", "tls", "troubleshoot tls", "openssl"] },
  { title: "Containers & VMs", section: "sysadmin", tool: "Containers & VMs",
    keywords: ["container", "docker", "podman", "container logs", "list containers", "images", "virtual machine", "vm", "libvirt", "virsh", "kvm", "prune"] },
  { title: "Directory Services (Active Directory / LDAP)", section: "sysadmin", tool: "Directory Services (Active Directory / LDAP)",
    keywords: ["active directory", "join domain", "join active directory", "realm", "realmd", "sssd", "ldap", "ldaps", "kerberos", "domain join", "leave domain", "directory", "winbind"] },
  { title: "Distro Subscription & Licensing", section: "sysadmin", tool: "Distro Subscription & Licensing",
    keywords: ["subscription", "license", "licensing", "register", "registration", "subscription-manager", "rhsm", "red hat subscription", "activation key", "ubuntu pro", "pro attach", "suseconnect", "scc", "suse register", "entitlement"] },
];

// Mirror of the desktop search() scoring: exact 100, prefix 80, substring 60,
// all-words 40. Empty query -> no results (the box jumps to a task, not browse).
export function searchTasks(query, limit = 8) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return [];
  const words = q.split(/\s+/);
  const scored = [];
  for (const entry of REGISTRY) {
    let best = 0;
    const hay = [entry.title.toLowerCase(), ...entry.keywords.map((k) => k.toLowerCase())];
    for (const text of hay) {
      if (q === text) best = Math.max(best, 100);
      else if (text.startsWith(q)) best = Math.max(best, 80);
      else if (text.includes(q)) best = Math.max(best, 60);
      else if (words.every((w) => text.includes(w))) best = Math.max(best, 40);
    }
    if (best) scored.push([best, entry]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.slice(0, limit).map((s) => s[1]);
}
