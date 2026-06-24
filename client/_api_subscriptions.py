"""Subscription / licensing command builders for the commercial Linux
distributions (dual-host, agent + SSH):

  * Red Hat family  — subscription-manager (RHSM)
  * Canonical/Ubuntu — the Ubuntu Pro client (`pro`)
  * SUSE / SLES      — SUSEConnect (SCC)

Each builder guards on the relevant tool being present and emits a clear
"this isn't that kind of host" message otherwise, so running a Red Hat
action against an Ubuntu box fails loudly instead of silently. Values are
shlex.quote()'d. Registration secrets (RHSM password, Ubuntu Pro token,
SUSE reg-code) are passed to the vendor tool the only way it accepts them
- on its own argv - so they can briefly appear in `ps` on the target host
while the command runs; prefer the non-password paths (RHSM activation
keys) where possible.

Imported via `from client._api_subscriptions import *` at the bottom of
client/api.py.
"""
import shlex


# ===========================================================
# Overview / detection (distro-agnostic)
# ===========================================================
def cmd_subscription_detect() -> str:
    """Identify the distro and which subscription tooling is present, so an
    operator knows which tab applies before registering anything."""
    return (
        '. /etc/os-release 2>/dev/null; '
        'echo "Distro:  ${NAME:-unknown} ${VERSION_ID:-}"; '
        'echo "ID:      ${ID:-?}    ID_LIKE: ${ID_LIKE:-}"; '
        'echo; echo "Subscription tooling on this host:"; '
        'for t in subscription-manager pro SUSEConnect; do '
        '  if command -v "$t" >/dev/null 2>&1; then echo "  $t: present"; '
        '  else echo "  $t: not installed"; fi; done'
    )


def cmd_subscription_register_all(org: str = "", activationkey: str = "",
                                  username: str = "", password: str = "",
                                  auto_attach: bool = True, pro_token: str = "",
                                  suse_regcode: str = "", suse_email: str = "") -> str:
    """One 'register' that works across a whole fleet at once: each host
    detects its own subscription tooling and registers with the matching
    vendor using whichever credentials were supplied - RHSM (org +
    activation key, or username + password) on Red Hat-family hosts,
    Ubuntu Pro (token) on Ubuntu, SUSEConnect (reg-code) on SLES.

    Dispatched across every selected host like any other action, so
    checking five RHEL boxes and clicking once registers all five. Hosts
    of a vendor you didn't provide credentials for report a clear 'skipping'
    message instead of failing silently, which also makes it safe to fire
    across a mixed-distro fleet. Same secret-on-argv caveat as the
    individual register commands applies (prefer RHSM activation keys)."""
    org = (org or "").strip()
    ak = (activationkey or "").strip()
    username = (username or "").strip()
    pro_token = (pro_token or "").strip()
    suse_regcode = (suse_regcode or "").strip()
    suse_email = (suse_email or "").strip()
    auto = " --auto-attach" if auto_attach else ""

    if not (ak or username or pro_token or suse_regcode):
        raise ValueError(
            "Provide credentials for at least one vendor: RHSM (org + activation key, "
            "or username + password), an Ubuntu Pro token, or a SUSE registration code."
        )

    # Red Hat branch
    if ak:
        if not org:
            raise ValueError("An Organization (org ID) is required when using an RHSM activation key.")
        rhsm = ("echo '== Red Hat (RHSM) =='; subscription-manager register "
                f"--org {shlex.quote(org)} --activationkey {shlex.quote(ak)}{auto} 2>&1")
    elif username:
        if not password:
            raise ValueError("A password is required when registering RHSM with a username.")
        org_arg = f" --org {shlex.quote(org)}" if org else ""
        rhsm = ("echo '== Red Hat (RHSM) =='; subscription-manager register "
                f"--username {shlex.quote(username)} --password {shlex.quote(password)}"
                f"{org_arg}{auto} 2>&1")
    else:
        rhsm = ("echo 'Red Hat-family host detected, but no RHSM credentials were provided "
                "(org + activation key, or username + password). Skipping.' >&2; exit 1")

    # Ubuntu branch
    if pro_token:
        pro = f"echo '== Ubuntu Pro =='; pro attach {shlex.quote(pro_token)} 2>&1"
    else:
        pro = ("echo 'Ubuntu host detected, but no Ubuntu Pro token was provided. "
               "Skipping.' >&2; exit 1")

    # SUSE branch
    if suse_regcode:
        email_arg = f" -e {shlex.quote(suse_email)}" if suse_email else ""
        suse = f"echo '== SUSE (SCC) =='; SUSEConnect -r {shlex.quote(suse_regcode)}{email_arg} 2>&1"
    else:
        suse = ("echo 'SUSE / SLES host detected, but no SUSE registration code was provided. "
                "Skipping.' >&2; exit 1")

    return (
        "if command -v subscription-manager >/dev/null 2>&1; then " + rhsm + "; "
        "elif command -v pro >/dev/null 2>&1; then " + pro + "; "
        "elif command -v SUSEConnect >/dev/null 2>&1; then " + suse + "; "
        "else echo 'No supported subscription tool (subscription-manager / pro / "
        "SUSEConnect) found on this host.' >&2; exit 1; fi"
    )


# ===========================================================
# Red Hat family - subscription-manager (RHSM)
# ===========================================================
_RHSM_MISSING = (
    "if ! command -v subscription-manager >/dev/null 2>&1; then "
    "echo 'subscription-manager is not installed - this does not look like a Red Hat-family "
    "subscription host (RHEL / CentOS Stream / Fedora).' >&2; exit 1; fi; "
)


def cmd_rhsm_status() -> str:
    return _RHSM_MISSING + "subscription-manager status 2>&1; echo; subscription-manager identity 2>&1 || true"


def cmd_rhsm_register(org: str = "", activationkey: str = "",
                      username: str = "", password: str = "",
                      auto_attach: bool = True) -> str:
    """Register the host with RHSM. Preferred path is Organization + one or
    more Activation Keys (no password); a Username + Password is also
    supported. `auto_attach` runs entitlement auto-attach on register."""
    org = (org or "").strip()
    ak = (activationkey or "").strip()
    username = (username or "").strip()
    auto = " --auto-attach" if auto_attach else ""

    if ak:
        if not org:
            raise ValueError("An Organization (org ID) is required when using an activation key.")
        return _RHSM_MISSING + (
            f"subscription-manager register --org {shlex.quote(org)} "
            f"--activationkey {shlex.quote(ak)}{auto} 2>&1"
        )
    if username:
        if not password:
            raise ValueError("A password is required when registering with a username.")
        org_arg = f" --org {shlex.quote(org)}" if org else ""
        return _RHSM_MISSING + (
            f"subscription-manager register --username {shlex.quote(username)} "
            f"--password {shlex.quote(password)}{org_arg}{auto} 2>&1"
        )
    raise ValueError("Provide either an Organization + Activation Key, or a Username + Password.")


def cmd_rhsm_auto_attach() -> str:
    return _RHSM_MISSING + "subscription-manager attach --auto 2>&1"


def cmd_rhsm_refresh() -> str:
    return _RHSM_MISSING + "subscription-manager refresh 2>&1 && echo 'Refreshed.'"


def cmd_rhsm_list_consumed() -> str:
    return _RHSM_MISSING + "subscription-manager list --consumed 2>&1"


def cmd_rhsm_list_available() -> str:
    return _RHSM_MISSING + "subscription-manager list --available 2>&1"


def cmd_rhsm_repos() -> str:
    return _RHSM_MISSING + "subscription-manager repos --list 2>&1"


def cmd_rhsm_unregister() -> str:
    return _RHSM_MISSING + "subscription-manager unregister 2>&1 && subscription-manager clean 2>&1 && echo 'Unregistered and cleaned.'"


# ===========================================================
# Canonical / Ubuntu - the Ubuntu Pro client (`pro`)
# ===========================================================
_PRO_MISSING = (
    "if ! command -v pro >/dev/null 2>&1; then "
    "echo 'The Ubuntu Pro client (pro / ubuntu-advantage-tools) is not installed - this is an "
    "Ubuntu-only tool.' >&2; exit 1; fi; "
)

# Common Ubuntu Pro services an admin enables/disables.
PRO_SERVICES = [
    "esm-infra", "esm-apps", "livepatch", "fips", "fips-updates",
    "usg", "cis", "realtime-kernel", "landscape",
]


def cmd_pro_status() -> str:
    return _PRO_MISSING + "pro status --all 2>&1"


def cmd_pro_attach(token: str) -> str:
    token = (token or "").strip()
    if not token:
        raise ValueError("An Ubuntu Pro token is required (from ubuntu.com/pro/dashboard).")
    return _PRO_MISSING + f"pro attach {shlex.quote(token)} 2>&1"


def cmd_pro_detach() -> str:
    return _PRO_MISSING + "pro detach --assume-yes 2>&1"


def cmd_pro_enable(service: str) -> str:
    service = (service or "").strip()
    if not service:
        raise ValueError("Choose a Pro service to enable.")
    return _PRO_MISSING + f"pro enable {shlex.quote(service)} --assume-yes 2>&1"


def cmd_pro_disable(service: str) -> str:
    service = (service or "").strip()
    if not service:
        raise ValueError("Choose a Pro service to disable.")
    return _PRO_MISSING + f"pro disable {shlex.quote(service)} --assume-yes 2>&1"


def cmd_pro_refresh() -> str:
    return _PRO_MISSING + "pro refresh 2>&1 && echo 'Refreshed.'"


# ===========================================================
# SUSE / SLES - SUSEConnect (SCC)
# ===========================================================
_SUSE_MISSING = (
    "if ! command -v SUSEConnect >/dev/null 2>&1; then "
    "echo 'SUSEConnect is not installed - this is a SUSE / SLES registration tool.' >&2; exit 1; fi; "
)


def cmd_suse_status() -> str:
    return _SUSE_MISSING + "SUSEConnect --status-text 2>&1"


def cmd_suse_register(regcode: str, email: str = "") -> str:
    regcode = (regcode or "").strip()
    email = (email or "").strip()
    if not regcode:
        raise ValueError("A SUSE registration code is required (from scc.suse.com).")
    email_arg = f" -e {shlex.quote(email)}" if email else ""
    return _SUSE_MISSING + f"SUSEConnect -r {shlex.quote(regcode)}{email_arg} 2>&1"


def cmd_suse_list_extensions() -> str:
    return _SUSE_MISSING + "SUSEConnect --list-extensions 2>&1"


def cmd_suse_deregister() -> str:
    return _SUSE_MISSING + "SUSEConnect -d 2>&1 && echo 'Deregistered.'"
