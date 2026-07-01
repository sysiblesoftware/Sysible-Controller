"""Certificate Management command builders (dual-host).

CSR generation, installing/renewing/replacing certificates, certificate
chain handling, and TLS troubleshooting. Plain POSIX sh, shlex.quote() on
interpolated values, explicit messages for missing tools, real exit codes.
"""
import re
import shlex

_HOSTPORT_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
# Subject CN / O etc: letters, digits, spaces, and a few punctuation marks.
_SUBJECT_RE = re.compile(r"^[\w .,'\-]+$")


def _need_openssl() -> str:
    return ("if ! command -v openssl >/dev/null 2>&1; then "
            "echo 'openssl is not installed on this host.' >&2; exit 1; fi; ")


def cmd_generate_csr(common_name: str, org: str = "", out_dir: str = "/etc/ssl/sysible") -> str:
    """Generate a 2048-bit key + CSR for `common_name` under `out_dir`."""
    common_name = (common_name or "").strip()
    org = (org or "").strip()
    out_dir = (out_dir or "/etc/ssl/sysible").strip()
    if not common_name or not _HOSTPORT_RE.match(common_name):
        raise ValueError("Common Name must be a hostname/FQDN (e.g. www.example.com).")
    if org and not _SUBJECT_RE.match(org):
        raise ValueError("Organization contains unexpected characters.")
    subj = f"/CN={common_name}" + (f"/O={org}" if org else "")
    qsubj = shlex.quote(subj)
    qdir = shlex.quote(out_dir)
    return (
        _need_openssl() +
        f"d={qdir}; mkdir -p \"$d\" && chmod 700 \"$d\"; "
        f"key=\"$d/{common_name}.key\"; csr=\"$d/{common_name}.csr\"; "
        f"openssl req -new -newkey rsa:2048 -nodes -keyout \"$key\" -out \"$csr\" -subj {qsubj} && "
        "chmod 600 \"$key\" && "
        f"echo \"Generated key and CSR for {common_name}:\" && echo \"  $key\" && echo \"  $csr\" && "
        "echo && echo '== CSR ==' && cat \"$csr\""
    )


def cmd_install_certificate(cert_src: str, key_src: str, dest_dir: str = "/etc/ssl/sysible") -> str:
    """Install an existing cert (+ optional key) into `dest_dir` with safe
    permissions."""
    cert_src = (cert_src or "").strip()
    key_src = (key_src or "").strip()
    dest_dir = (dest_dir or "/etc/ssl/sysible").strip()
    if not cert_src:
        raise ValueError("Certificate file path is required.")
    qcert = shlex.quote(cert_src)
    qdir = shlex.quote(dest_dir)
    out = (
        _need_openssl() +
        f"cert={qcert}; d={qdir}; "
        'if [ ! -f "$cert" ]; then echo "Certificate not found: $cert" >&2; exit 1; fi; '
        'mkdir -p "$d"; '
        'openssl x509 -in "$cert" -noout -subject -enddate || { echo "Not a valid certificate." >&2; exit 1; }; '
        'install -m 644 "$cert" "$d/" && echo "Installed certificate into $d."; '
    )
    if key_src:
        qkey = shlex.quote(key_src)
        out += (
            f"key={qkey}; if [ -f \"$key\" ]; then install -m 600 \"$key\" \"$d/\" && "
            "echo \"Installed private key (mode 600) into $d.\"; "
            "else echo \"Key not found: $key\" >&2; exit 1; fi"
        )
    return out


def cmd_check_certificate(cert_path: str) -> str:
    """Show a certificate's subject, issuer, validity window, and whether
    it has expired."""
    cert_path = (cert_path or "").strip()
    if not cert_path:
        raise ValueError("Certificate file path is required.")
    qc = shlex.quote(cert_path)
    return (
        _need_openssl() +
        f"c={qc}; if [ ! -f \"$c\" ]; then echo \"Certificate not found: $c\" >&2; exit 1; fi; "
        "openssl x509 -in \"$c\" -noout -subject -issuer -dates 2>&1; echo; "
        "if openssl x509 -in \"$c\" -noout -checkend 0 >/dev/null 2>&1; then "
        "echo 'Status: VALID (not expired).'; "
        "if ! openssl x509 -in \"$c\" -noout -checkend 2592000 >/dev/null 2>&1; then "
        "echo 'Note: expires within 30 days - plan to renew.'; fi; "
        "else echo 'Status: EXPIRED - replace this certificate.' >&2; exit 1; fi"
    )


def cmd_install_certbot() -> str:
    """Install certbot (Let's Encrypt client) using whatever package manager the
    host has — cross-distro. RHEL/CentOS pull it from EPEL if enabled; snap is a
    last resort where a native package isn't available. No-op if already present."""
    return (
        "if command -v certbot >/dev/null 2>&1; then echo \"certbot already installed: $(certbot --version 2>&1)\"; exit 0; fi; "
        "if command -v dnf >/dev/null 2>&1; then dnf install -y certbot; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y certbot; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install certbot; "
        "elif command -v apt-get >/dev/null 2>&1; then export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y certbot; "
        "elif command -v snap >/dev/null 2>&1; then snap install --classic certbot && ln -sf /snap/bin/certbot /usr/bin/certbot; "
        "else echo 'No supported package manager (dnf/yum/zypper/apt/snap) found to install certbot.' >&2; exit 1; fi; "
        "if command -v certbot >/dev/null 2>&1; then echo \"Installed: $(certbot --version 2>&1)\"; "
        "else echo 'certbot install did not complete (a native package may be unavailable — on RHEL enable EPEL, or install snapd).' >&2; exit 1; fi"
    )


def cmd_renew_certbot(domain: str = "") -> str:
    """Renew Let's Encrypt certs via certbot (all, or one domain)."""
    domain = (domain or "").strip()
    if domain and not _HOSTPORT_RE.match(domain):
        raise ValueError("Domain must be a hostname/FQDN.")
    inner = "certbot renew" if not domain else f"certbot certonly --force-renewal -d {shlex.quote(domain)}"
    return (
        "if ! command -v certbot >/dev/null 2>&1; then "
        "echo 'certbot is not installed on this host. Use the \"Install certbot\" button above first.' >&2; exit 1; fi; "
        f"{inner} 2>&1"
    )


def cmd_verify_chain(cert_path: str, chain_path: str = "") -> str:
    """Verify a certificate against an (optional) intermediate chain and
    show the chain it presents."""
    cert_path = (cert_path or "").strip()
    chain_path = (chain_path or "").strip()
    if not cert_path:
        raise ValueError("Certificate file path is required.")
    qc = shlex.quote(cert_path)
    verify = f"openssl verify {qc} 2>&1"
    if chain_path:
        qchain = shlex.quote(chain_path)
        verify = f"openssl verify -untrusted {qchain} {qc} 2>&1"
    return (
        _need_openssl() +
        f"c={qc}; if [ ! -f \"$c\" ]; then echo \"Certificate not found: $c\" >&2; exit 1; fi; "
        "echo '== Verify =='; " + verify + "; echo; "
        "echo '== Certificate chain (issuers) =='; "
        "openssl crl2pkcs7 -nocrl -certfile \"$c\" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null "
        "| grep -E 'subject|issuer' || openssl x509 -in \"$c\" -noout -subject -issuer"
    )


def cmd_troubleshoot_tls(host: str, port: str = "443") -> str:
    """Connect to host:port with openssl s_client and report the presented
    certificate, chain, and verification result."""
    host = (host or "").strip()
    port = (port or "443").strip()
    if not host or not _HOSTPORT_RE.match(host):
        raise ValueError("Host must be a hostname or IP.")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError("Port must be 1-65535.")
    qh = shlex.quote(host)
    return (
        _need_openssl() +
        f"h={qh}; p={port}; echo \"Connecting to $h:$p ...\"; echo; "
        "out=$(echo | openssl s_client -connect \"$h:$p\" -servername \"$h\" 2>&1); "
        "echo \"$out\" | sed -n '/Certificate chain/,/---/p'; echo; "
        "echo \"$out\" | grep -E 'subject=|issuer=|Verify return code|Verification'; echo; "
        "echo '== Validity =='; echo | openssl s_client -connect \"$h:$p\" -servername \"$h\" 2>/dev/null "
        "| openssl x509 -noout -dates 2>/dev/null || echo '(could not parse certificate - is the port serving TLS?)'"
    )
