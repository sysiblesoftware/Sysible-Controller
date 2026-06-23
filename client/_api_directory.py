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
    return (
        _INSTALL_REALM + "; "
        "cred=$(mktemp) && chmod 600 \"$cred\"; "
        f"printf '%s' {q_pass} > \"$cred\"; "
        f"realm join {ou}-U {q_user} {q_dom} < \"$cred\"; rc=$?; rm -f \"$cred\"; "
        "systemctl enable --now oddjobd 2>/dev/null || true; "
        f"if [ \"$rc\" -eq 0 ]; then echo 'Joined {domain}.'; realm list; "
        "else echo 'Join failed - check the domain name, credentials, DNS resolution of the domain, and that the host clock is in sync with the DC.' >&2; exit \"$rc\"; fi"
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
