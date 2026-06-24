"""Directory Services command builders (dual-host): join/leave Active
Directory via realmd/SSSD, realm status and login permits, home-dir
creation, LDAPS connectivity testing, and a generic SSSD LDAP(S) client
config.

Plain POSIX sh, shlex.quote() on interpolated values, explicit messages
for missing tooling, and real exit codes for the result banner.
"""
import re
import shlex

_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9 ._@\-]+$")
_KRB_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9._/\-]+(@[A-Za-z0-9.\-]+)?$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


_INSTALL_REALM = (
    "if command -v apt-get >/dev/null 2>&1; then export DEBIAN_FRONTEND=noninteractive; apt-get update; "
    "apt-get install -y realmd sssd sssd-tools libnss-sss libpam-sss adcli samba-common-bin "
    "oddjob oddjob-mkhomedir packagekit krb5-user; "
    "elif command -v dnf >/dev/null 2>&1; then dnf install -y realmd sssd oddjob oddjob-mkhomedir adcli "
    "samba-common samba-common-tools krb5-workstation; "
    "elif command -v yum >/dev/null 2>&1; then yum install -y realmd sssd oddjob oddjob-mkhomedir adcli "
    "samba-common samba-common-tools krb5-workstation; "
    "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install realmd sssd sssd-ad adcli "
    "samba-client krb5-client; "
    "else echo 'No supported package manager found.' >&2; exit 1; fi"
)


def cmd_install_ad_dependencies() -> str:
    """Install everything needed to join a host to Active Directory, without
    joining yet (realmd, SSSD, adcli, Kerberos, samba tools, oddjob)."""
    return (
        _INSTALL_REALM +
        " && echo 'Active Directory client dependencies installed "
        "(realmd, SSSD, adcli, Kerberos, samba tools). You can now Join Domain.'"
    )


def cmd_prepare_ad_join(domain: str = "") -> str:
    """One-click 'prepare host for AD domain join': installs the client
    packages AND turns on the prerequisites that installing alone leaves
    off - time synchronization (Kerberos rejects a skewed clock), the
    message bus + oddjobd, and automatic home-dir creation on first login -
    then prints a readiness check. SSSD itself is intentionally left
    stopped: it has no config until `realm join` writes one and starts it.
    If a domain is given, also runs `realm discover` as a DNS/reachability
    pre-flight."""
    domain = (domain or "").strip()
    if domain and not _DOMAIN_RE.match(domain):
        raise ValueError("Enter a valid AD domain (e.g. corp.example.com).")

    discover = ""
    if domain:
        dq = shlex.quote(domain)
        discover = (
            "echo; echo '== Domain discovery (realm discover) =='; "
            f"realm discover {dq} 2>&1 || echo 'Could not discover {domain} - point this "
            "host'\\''s DNS at the AD domain controllers and confirm the domain is reachable.'; "
        )

    return (
        "set +e; "
        "echo '== 1/5 Installing AD client packages =='; "
        + _INSTALL_REALM + " || { echo 'Package install failed.' >&2; exit 1; }; "
        "echo 'Packages installed.'; "
        "echo; echo '== 2/5 Enabling time synchronization (Kerberos needs an accurate clock) =='; "
        "if systemctl enable --now chronyd 2>/dev/null || systemctl enable --now chrony 2>/dev/null; then "
        "echo 'chrony enabled and started.'; "
        "else timedatectl set-ntp true 2>/dev/null; "
        "systemctl enable --now systemd-timesyncd 2>/dev/null && echo 'systemd-timesyncd enabled.' "
        "|| echo 'Could not enable a time-sync service - make sure this host'\\''s clock is accurate.'; fi; "
        "echo; echo '== 3/5 Starting message bus + oddjob =='; "
        "systemctl enable --now dbus 2>/dev/null || systemctl enable --now messagebus 2>/dev/null || true; "
        "systemctl enable --now oddjobd 2>/dev/null && echo 'oddjobd enabled and started.' "
        "|| echo 'oddjobd not started (install/start it if home-dir creation is needed).'; "
        "echo; echo '== 4/5 Enabling automatic home-dir creation on first login =='; "
        "if command -v authselect >/dev/null 2>&1; then "
        "authselect enable-feature with-mkhomedir 2>&1 && echo 'mkhomedir enabled (authselect).'; "
        "elif command -v pam-auth-update >/dev/null 2>&1; then "
        "pam-auth-update --enable mkhomedir 2>/dev/null && echo 'mkhomedir enabled (pam-auth-update).' "
        "|| echo 'Enable \"Create home directory on login\" via pam-auth-update.'; "
        "else echo 'oddjobd started; enable pam_mkhomedir in your PAM stack if home dirs are needed.'; fi; "
        "echo; echo '== 5/5 Readiness check =='; "
        "for t in realm adcli klist; do if command -v \"$t\" >/dev/null 2>&1; then echo \"  $t: present\"; "
        "else echo \"  $t: MISSING\"; fi; done; "
        "if [ -x /usr/sbin/sssd ] || [ -x /sbin/sssd ] || command -v sssd >/dev/null 2>&1; then "
        "echo '  sssd: installed (will start on join)'; else echo '  sssd: MISSING'; fi; "
        "echo '  time sync:'; timedatectl 2>/dev/null | grep -iE 'synchronized|NTP service' | sed 's/^/   /' || true; "
        + discover +
        "echo; echo 'Host prepared. Next: Join AD Domain with your domain and an account permitted to join computers.'"
    )


def cmd_install_ldap_dependencies() -> str:
    """Install everything needed for LDAP/LDAPS client auth via SSSD."""
    return (
        "if command -v apt-get >/dev/null 2>&1; then export DEBIAN_FRONTEND=noninteractive; apt-get update; "
        "apt-get install -y sssd sssd-tools libnss-sss libpam-sss ldap-utils oddjob oddjob-mkhomedir; "
        "elif command -v dnf >/dev/null 2>&1; then dnf install -y sssd sssd-ldap openldap-clients oddjob oddjob-mkhomedir; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y sssd sssd-ldap openldap-clients oddjob oddjob-mkhomedir; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install sssd sssd-ldap openldap2-client; "
        "else echo 'No supported package manager found.' >&2; exit 1; fi; "
        "echo 'LDAP client dependencies installed (SSSD, LDAP utils, PAM/NSS modules).'"
    )


def cmd_kerberos_status() -> str:
    return (
        "echo '== Cached tickets (klist) =='; klist 2>&1 || echo '(no credential cache, or klist not installed)'; echo; "
        "echo '== Default realm =='; grep -i 'default_realm' /etc/krb5.conf 2>/dev/null || echo '(none configured)'; echo; "
        "echo '== Host keytab (klist -k) =='; klist -k 2>/dev/null | head -n 20 || echo '(no /etc/krb5.keytab)'"
    )


def cmd_kerberos_destroy() -> str:
    return (
        "if command -v kdestroy >/dev/null 2>&1; then kdestroy && echo 'Destroyed cached Kerberos tickets.'; "
        "else echo 'kdestroy is not installed (install the Kerberos client).' >&2; exit 1; fi"
    )


def cmd_kerberos_kinit(principal: str, password: str) -> str:
    """Get a TGT for `principal` to verify Kerberos auth works. The password
    is fed via a transient root-only file on stdin, never the command line."""
    principal = (principal or "").strip()
    if not principal or not _KRB_PRINCIPAL_RE.match(principal):
        raise ValueError("Enter a principal, e.g. jdoe or jdoe@CORP.EXAMPLE.COM.")
    if not password:
        raise ValueError("Password is required to obtain a ticket.")
    q_p, q_pass = shlex.quote(principal), shlex.quote(password)
    return (
        "if ! command -v kinit >/dev/null 2>&1; then echo 'kinit is not installed (install the Kerberos client).' >&2; exit 1; fi; "
        "cred=$(mktemp) && chmod 600 \"$cred\"; "
        f"printf '%s' {q_pass} > \"$cred\"; "
        f"kinit {q_p} < \"$cred\"; rc=$?; rm -f \"$cred\"; "
        f"if [ \"$rc\" -eq 0 ]; then echo 'Obtained a ticket for {principal}:'; klist; "
        "else echo 'kinit failed - check the principal, password, realm, and that DNS/clock are in sync with the KDC.' >&2; exit \"$rc\"; fi"
    )


def cmd_kerberos_config(realm: str, kdc: str, admin_server: str = "") -> str:
    """Write a basic /etc/krb5.conf pointing at a realm and KDC."""
    realm = (realm or "").strip().upper()
    kdc = (kdc or "").strip()
    admin_server = (admin_server or "").strip() or kdc
    if not _DOMAIN_RE.match(realm) or "." not in realm:
        raise ValueError("Realm is required (e.g. CORP.EXAMPLE.COM).")
    if not _HOST_RE.match(kdc):
        raise ValueError("KDC must be a hostname or IP.")
    if not _HOST_RE.match(admin_server):
        raise ValueError("Admin server must be a hostname or IP.")
    conf = (
        "[libdefaults]\n"
        f"    default_realm = {realm}\n"
        "    dns_lookup_realm = false\n"
        "    dns_lookup_kdc = true\n"
        "    rdns = false\n\n"
        "[realms]\n"
        f"    {realm} = {{\n"
        f"        kdc = {kdc}\n"
        f"        admin_server = {admin_server}\n"
        "    }\n"
    )
    return (
        "[ -f /etc/krb5.conf ] && cp /etc/krb5.conf /etc/krb5.conf.sysible.bak; "
        "cat > /etc/krb5.conf <<'SYS_KRB5'\n" + conf + "SYS_KRB5\n"
        f"echo 'Wrote /etc/krb5.conf for realm {realm} (KDC {kdc}); a backup of any prior config is at /etc/krb5.conf.sysible.bak.'"
    )


def cmd_realm_status() -> str:
    return (
        "if command -v realm >/dev/null 2>&1; then echo '== realm list =='; realm list || echo '(not joined to any domain)'; "
        "else echo 'realmd is not installed - use \"Join Active Directory\" (it installs the tooling).'; fi; "
        "echo; echo '== SSSD service =='; systemctl is-active sssd 2>/dev/null || echo 'sssd not running'; "
        "echo; echo '== Kerberos default realm =='; grep -i '^\\s*default_realm' /etc/krb5.conf 2>/dev/null || echo '(none configured)'"
    )


def cmd_join_ad(domain: str, admin_user: str, password: str, computer_ou: str = "") -> str:
    """Install the AD client tooling and join `domain` with `admin_user`'s
    credentials. The password is written to a transient root-only file fed
    to realm on stdin, never placed on the realm command line."""
    domain = (domain or "").strip()
    admin_user = (admin_user or "").strip()
    computer_ou = (computer_ou or "").strip()
    if not _DOMAIN_RE.match(domain) or "." not in domain:
        raise ValueError("Enter the AD domain (e.g. corp.example.com).")
    if not admin_user or " " in admin_user:
        raise ValueError("Enter the joining account (e.g. Administrator).")
    if not password:
        raise ValueError("The joining account's password is required.")
    q_dom, q_user, q_pass = shlex.quote(domain), shlex.quote(admin_user), shlex.quote(password)
    ou = f"--computer-ou={shlex.quote(computer_ou)} " if computer_ou else ""
    # A discovery pre-check turns realmd's opaque "No such realm found" into
    # an actionable diagnosis (which DNS servers the host uses, the domain
    # SRV lookup, and the .local/mDNS trap) before we ever try to join.
    return (
        f"dom={q_dom}; "
        + _INSTALL_REALM + "; "
        'echo "== Discovering $dom =="; '
        'if ! realm discover "$dom" 2>&1; then '
        '  echo "" >&2; '
        '  echo "Realm discovery failed: this host cannot locate $dom via DNS, so a join cannot proceed." >&2; '
        '  echo "DNS servers this host is using:" >&2; '
        '  { resolvectl status 2>/dev/null | grep -i "DNS Server" || grep -i "^nameserver" /etc/resolv.conf; } >&2; '
        '  echo "AD SRV record lookup (_ldap._tcp.$dom):" >&2; '
        '  { host -t SRV _ldap._tcp."$dom" 2>&1 || nslookup -type=SRV _ldap._tcp."$dom" 2>&1 || echo "(no host/nslookup tool installed)"; } >&2; '
        '  case "$dom" in *.local) echo "NOTE: a .local domain is normally resolved by mDNS/Avahi, NOT your AD DNS - this by itself causes the No such realm found error. Point this host DNS at the AD domain controller, and/or stop mDNS handling .local, then retry." >&2;; esac; '
        '  echo "Fix: set this host DNS to the AD domain controller(s), confirm the clock is in sync with the DC, then retry." >&2; '
        '  exit 1; '
        'fi; '
        'cred=$(mktemp) && chmod 600 "$cred"; '
        f"printf '%s' {q_pass} > \"$cred\"; "
        f'realm join {ou}-U {q_user} "$dom" < "$cred"; rc=$?; rm -f "$cred"; '
        'systemctl enable --now oddjobd 2>/dev/null || true; '
        'if [ "$rc" -eq 0 ]; then echo "Joined $dom."; realm list; '
        'else echo "Join failed (the domain was discoverable, so check the join account and password, the Computer OU, and that the host clock is in sync with the DC)." >&2; exit "$rc"; fi'
    )


def cmd_leave_ad(domain: str) -> str:
    domain = (domain or "").strip()
    if not _DOMAIN_RE.match(domain) or "." not in domain:
        raise ValueError("Enter the AD domain to leave (e.g. corp.example.com).")
    q = shlex.quote(domain)
    return (
        "if ! command -v realm >/dev/null 2>&1; then echo 'realmd is not installed.' >&2; exit 1; fi; "
        f"realm leave {q} && echo 'Left {domain}.'"
    )


def cmd_realm_permit(principal: str, is_group: bool = False) -> str:
    """Allow a specific AD user or group to log in (realm denies all by
    default after a join). `principal` like 'jdoe@corp.example.com' or a
    group name with is_group=True."""
    principal = (principal or "").strip()
    if not principal or not _PRINCIPAL_RE.match(principal):
        raise ValueError("Enter a user (e.g. jdoe@corp.example.com) or group name.")
    flag = "-g " if is_group else ""
    q = shlex.quote(principal)
    return (
        "if ! command -v realm >/dev/null 2>&1; then echo 'realmd is not installed.' >&2; exit 1; fi; "
        f"realm permit {flag}{q} && echo 'Permitted {principal} to log in.'"
    )


def cmd_enable_mkhomedir() -> str:
    """Make a home directory be created automatically on first login for
    directory users."""
    return (
        "systemctl enable --now oddjobd 2>/dev/null || true; "
        "if command -v authselect >/dev/null 2>&1; then "
        "authselect enable-feature with-mkhomedir 2>&1 && echo 'Enabled home-dir creation (authselect with-mkhomedir).'; "
        "elif command -v pam-auth-update >/dev/null 2>&1; then "
        "pam-auth-update --enable mkhomedir 2>/dev/null && echo 'Enabled home-dir creation (pam-auth-update mkhomedir).' "
        "|| echo 'Run pam-auth-update on the host and enable \"Create home directory on login\".'; "
        "else echo 'Could not auto-configure mkhomedir; oddjobd was started - enable pam_mkhomedir for your PAM stack.'; fi"
    )


def cmd_test_ldaps(server: str, port: str = "636", base_dn: str = "") -> str:
    server = (server or "").strip()
    port = (port or "636").strip()
    base_dn = (base_dn or "").strip()
    if not _HOST_RE.match(server):
        raise ValueError("LDAP server must be a hostname or IP.")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError("Port must be 1-65535 (LDAPS is usually 636).")
    q_srv = shlex.quote(server)
    out = (
        "if ! command -v openssl >/dev/null 2>&1; then echo 'openssl is not installed.' >&2; exit 1; fi; "
        f"echo '== TLS to {server}:{port} =='; "
        f"echo | openssl s_client -connect {q_srv}:{port} 2>&1 | grep -E 'subject=|issuer=|Verify return code' "
        "|| echo '(no TLS response - is this an LDAPS port?)'; echo; "
    )
    if base_dn:
        q_base = shlex.quote(base_dn)
        out += (
            "if command -v ldapsearch >/dev/null 2>&1; then echo '== Anonymous base search =='; "
            f"ldapsearch -H ldaps://{q_srv}:{port} -x -b {q_base} -s base 2>&1 | head -n 20; "
            "else echo '(install ldap-utils / openldap-clients for an LDAP search test)'; fi"
        )
    else:
        out += "echo 'Provide a base DN to also run an anonymous LDAP search.'"
    return out


def cmd_configure_ldap_client(server: str, base_dn: str, use_ldaps: bool = True) -> str:
    """Write a basic SSSD config for a generic (non-AD) LDAP/LDAPS identity
    source and restart SSSD. For AD, use Join Active Directory instead."""
    server = (server or "").strip()
    base_dn = (base_dn or "").strip()
    if not _HOST_RE.match(server):
        raise ValueError("LDAP server must be a hostname or IP.")
    if not base_dn:
        raise ValueError("Base DN is required (e.g. dc=example,dc=com).")
    scheme = "ldaps" if use_ldaps else "ldap"
    start_tls = "False" if use_ldaps else "True"
    conf = (
        "[sssd]\n"
        "services = nss, pam\n"
        "config_file_version = 2\n"
        "domains = LDAP\n\n"
        "[domain/LDAP]\n"
        "id_provider = ldap\n"
        "auth_provider = ldap\n"
        f"ldap_uri = {scheme}://{server}\n"
        f"ldap_search_base = {base_dn}\n"
        f"ldap_id_use_start_tls = {start_tls}\n"
        "ldap_tls_reqcert = demand\n"
        "cache_credentials = True\n"
    )
    return (
        "cat > /etc/sssd/sssd.conf <<'SYS_SSSD'\n" + conf + "SYS_SSSD\n"
        "chmod 600 /etc/sssd/sssd.conf && "
        "systemctl restart sssd 2>&1 && systemctl enable sssd 2>/dev/null; "
        f"echo 'Configured SSSD for {scheme}://{server} (base {base_dn}) and restarted SSSD.'"
    )
