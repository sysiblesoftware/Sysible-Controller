"""
Registry + matching logic for the dashboard's feature search bar
(client/home.py). Maps plain-language task descriptions ("create a
user", "add a repository", "view disk usage") to the dashboard tile -
and, for tasks nested under System Administration, the specific
sub-tile and (where that page has named tabs) the specific tab - that
performs that task. Lets an admin jump straight to a feature without
having to already know which tile, or which of System
Administration's seven sub-tiles, it lives under.

Entries are matched by simple substring/keyword scoring (see
search() below) rather than anything fancier - the registry is small
and hand-curated, so a real search index would be overkill.

Each entry:
    title    - shown in the results list and used as a match target
    keywords - phrases an admin might actually type; matched as
               substrings/word-sets against the query
    open     - name of an "open_xxx" method on HomeWindow (client/home.py)
    sub_open - name of an "open_xxx" method on the window `open`
               produces, if the feature is one level deeper (e.g.
               anything under System Administration)
    tab      - tab text to select on the final window, if that window
               exposes a `.tabs` QTabWidget (currently only User &
               Group Administration's Create User/Account/Password/
               Groups/Reports tabs do)
"""

REGISTRY = [
    # ---- Host Enrollment ----
    {"title": "Host Enrollment", "open": "open_hosts",
     "keywords": ["enroll host", "add host", "new host", "download agent",
                  "agent bundle", "managed hosts", "fleet", "host enrollment"]},

    # ---- Sysible Controller Settings ----
    {"title": "Sysible Controller Settings", "open": "open_admin_config",
     "keywords": ["settings", "administrators", "add administrator",
                  "admin password policy", "audit log", "controller address",
                  "controller port", "change my password", "controller settings"]},

    # ---- Remote Host Administration ----
    {"title": "Sysible Connect", "open": "open_remote",
     "keywords": ["ssh", "ssh terminal", "remote terminal", "console",
                  "connect to host", "environment tag", "remote administration",
                  "sysible connect", "upload to host", "download from host",
                  "file transfer", "terminal"]},

    # ---- Webserver Portal Configuration ----
    {"title": "Webserver Portal Configuration", "open": "open_portal",
     "keywords": ["portal", "file transfer", "upload file", "download file",
                  "reset portal password", "portal credentials"]},

    # ---- User & Group Administration (nested under System Administration) ----
    {"title": "Create User", "open": "open_system_admin", "sub_open": "open_user_group_admin",
     "tab": "Create User",
     "keywords": ["create a user", "create user", "add user", "new user",
                  "new account", "create account", "add a user"]},
    {"title": "Reset / Set Password", "open": "open_system_admin", "sub_open": "open_user_group_admin",
     "tab": "Password",
     "keywords": ["reset password", "change password", "set password",
                  "generate password", "force password reset", "password aging"]},
    {"title": "Account Status / Lock / Expiration", "open": "open_system_admin", "sub_open": "open_user_group_admin",
     "tab": "Account",
     "keywords": ["lock account", "unlock account", "disable account",
                  "account expiration", "account status", "kill sessions",
                  "terminate user"]},
    {"title": "Group Membership", "open": "open_system_admin", "sub_open": "open_user_group_admin",
     "tab": "Groups",
     "keywords": ["add to group", "group membership", "manage groups",
                  "sudo group", "add user to group"]},
    {"title": "User & Group Reports", "open": "open_system_admin", "sub_open": "open_user_group_admin",
     "tab": "Reports",
     "keywords": ["user report", "group report", "sudoers report"]},

    # ---- System Health & Logs ----
    {"title": "System Health & Logs", "open": "open_system_admin", "sub_open": "open_health_logs",
     "keywords": ["disk usage", "memory usage", "cpu usage", "view logs",
                  "search logs", "failed services", "large files", "uptime",
                  "process list", "system health"]},

    # ---- Service Management ----
    {"title": "Service Management", "open": "open_system_admin", "sub_open": "open_service_management",
     "keywords": ["restart service", "start service", "stop service",
                  "enable service", "disable service", "create service",
                  "systemd service"]},

    # ---- Environmental Policies ----
    {"title": "Environmental Policies", "open": "open_system_admin", "sub_open": "open_environmental_policies",
     "keywords": ["password policy", "lockout policy", "sudo policy",
                  "umask policy", "environmental policy"]},

    # ---- Cron & Systemd Timers ----
    {"title": "Cron & Systemd Timers", "open": "open_system_admin", "sub_open": "open_cron_timers",
     "keywords": ["cron job", "add cron job", "systemd timer",
                  "scheduled task", "schedule a task"]},

    # ---- Host Software Management ----
    {"title": "Host Software Management", "open": "open_system_admin", "sub_open": "open_software_mgmt",
     "keywords": ["install package", "remove package", "update package",
                  "query package", "verify package", "clean package cache",
                  "upgrade packages"]},

    # ---- Repository Management ----
    {"title": "Repository Management", "open": "open_system_admin", "sub_open": "open_repo_mgmt",
     "keywords": ["add repository", "add a repo", "enable repository",
                  "disable repository", "remove repository",
                  "repository to all hosts", "add repo to all hosts"]},

    # ---- Backup & Recovery ----
    {"title": "Backup & Recovery", "open": "open_system_admin", "sub_open": "open_backup_recovery",
     "keywords": ["backup", "back up files", "restore files", "verify backup",
                  "backup schedule", "schedule backup", "snapshot", "create snapshot",
                  "restore snapshot", "recover deleted files", "disaster recovery",
                  "dr test"]},

    # ---- System Boot & Recovery ----
    {"title": "System Boot & Recovery", "open": "open_system_admin", "sub_open": "open_boot_recovery",
     "keywords": ["boot failure", "analyze boot", "grub", "change grub",
                  "rebuild grub", "rescue mode", "emergency mode", "boot target",
                  "initramfs", "regenerate initramfs", "kernel parameters",
                  "kernel cmdline", "manage kernels", "remove old kernels"]},

    # ---- Time Synchronization ----
    {"title": "Time Synchronization", "open": "open_system_admin", "sub_open": "open_timesync",
     "keywords": ["ntp", "configure ntp", "chrony", "configure chrony",
                  "time sync", "verify synchronization", "clock drift",
                  "time zone", "timezone", "set time zone"]},

    # ---- Certificate Management ----
    {"title": "Certificate Management", "open": "open_system_admin", "sub_open": "open_cert_mgmt",
     "keywords": ["certificate", "csr", "generate csr", "install certificate",
                  "ssl certificate", "renew certificate", "replace expired certificate",
                  "certificate chain", "tls", "troubleshoot tls", "openssl"]},

    # ---- Containers & VMs ----
    {"title": "Containers & VMs", "open": "open_system_admin", "sub_open": "open_containers_vms",
     "keywords": ["container", "docker", "podman", "container logs", "list containers",
                  "images", "virtual machine", "vm", "libvirt", "virsh", "kvm"]},

    # ---- Run A Script Across All Hosts ----
    {"title": "Run A Script Across All Hosts", "open": "open_system_admin", "sub_open": "open_automation",
     "keywords": ["run command", "run script", "run a script", "run a command",
                  "ad hoc", "ad-hoc", "automate", "automation", "execute command",
                  "fleet command", "run on all hosts", "across all hosts",
                  "script on hosts", "repetitive task", "shell command"]},

    # ---- Directory Services (AD / LDAP) - a tab within Security Administration ----
    {"title": "Directory Services (AD / LDAP) - in Security Administration", "open": "open_system_admin", "sub_open": "open_security_admin",
     "keywords": ["active directory", "ad", "join domain", "join active directory",
                  "realm", "realmd", "sssd", "ldap", "ldaps", "kerberos", "domain join",
                  "leave domain", "directory", "winbind", "add server to active directory"]},

    # ---- License & Version (now a section of Sysible Controller
    # Settings, not its own tile - see client/admin_configuration_page.py) ----
    {"title": "License & Version", "open": "open_admin_config",
     "keywords": ["version", "license", "licensing", "what version",
                  "license key"]},
]


def search(query, limit=8):
    """Return up to `limit` REGISTRY entries that match `query`, best
    match first. Empty/whitespace-only query returns no results - the
    search box is for jumping straight to a specific task, not for
    browsing the whole registry."""
    query = (query or "").strip().lower()
    if not query:
        return []

    scored = []
    for entry in REGISTRY:
        best = 0
        haystacks = [entry["title"].lower()] + [k.lower() for k in entry["keywords"]]
        for text in haystacks:
            if query == text:
                best = max(best, 100)
            elif text.startswith(query):
                best = max(best, 80)
            elif query in text:
                best = max(best, 60)
            elif all(word in text for word in query.split()):
                best = max(best, 40)
        if best:
            scored.append((best, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]
