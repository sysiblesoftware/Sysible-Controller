"""FIREWALL ADMINISTRATION dual-host command builders - split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_firewall import *` at the bottom of client/api.py.

Covers firewalld (zones, ports, rich rules) plus the two lower-level
packet-filtering backends it normally sits on top of - nftables and
iptables - for hosts/scenarios where an admin wants to manage those
directly instead. Same rules as the rest of this split: plain POSIX
sh, shlex.quote() (or explicit validation) on anything interpolated,
a clear "X is not installed" message instead of a bare
command-not-found, and explicit guardrails before anything
destructive (flushes, deletes).
"""
import shlex


from client._validators import validate_nonempty_line as _validate_nonempty_line


def _validate_zone_name(name: str, label: str = "Zone name") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not all(c.isalnum() or c in "_-" for c in name):
        raise ValueError(f"{label} may only contain letters, numbers, dashes, and underscores.")
    return name


def _validate_port_spec(value: str, label: str = "Port") -> str:
    """Accepts a single port (1-65535) or a hyphenated range
    ("8000-9000"), as firewall-cmd's --add-port/--remove-port expect."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    parts = value.split("-")
    if len(parts) not in (1, 2):
        raise ValueError(f"{label} must be a single port or a range like 8000-9000.")
    nums = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            raise ValueError(f"{label} must be numeric.")
        if not (1 <= n <= 65535):
            raise ValueError(f"{label} must be between 1 and 65535.")
        nums.append(n)
    if len(nums) == 2 and nums[0] >= nums[1]:
        raise ValueError(f"{label} range must have a lower start than end.")
    return value


_VALID_PROTOCOLS = {"tcp", "udp"}


def _validate_protocol(value: str, label: str = "Protocol") -> str:
    value = (value or "").strip().lower()
    if value not in _VALID_PROTOCOLS:
        raise ValueError(f"{label} must be one of: {', '.join(sorted(_VALID_PROTOCOLS))}")
    return value



def _resplit_quote(value: str, label: str) -> str:
    """For free-text rule specs handed to nft/iptables (e.g. '-p tcp
    --dport 22 -j ACCEPT'). Both tools treat their trailing arguments
    as a single space-separated token stream, the same way a person
    typing the command at a shell would - so this re-tokenizes with
    shlex.split() (rejecting unbalanced quotes) and re-quotes each
    token individually, which preserves that structure while keeping
    every token shell-safe."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    try:
        tokens = shlex.split(value)
    except ValueError as e:
        raise ValueError(f"{label} could not be parsed ({e}). Check for unbalanced quotes.")
    if not tokens:
        raise ValueError(f"{label} is required.")
    return " ".join(shlex.quote(t) for t in tokens)


_FIREWALLD_MISSING = (
    "if ! command -v firewall-cmd >/dev/null 2>&1; then "
    "echo 'firewalld is not installed on this host (package: firewalld).' >&2; exit 1; fi; "
    # firewall-cmd is a Python program that imports the GObject bindings ('gi')
    # at startup. On minimal openSUSE/SUSE installs python3-gobject is absent, so
    # every call dies with 'ModuleNotFoundError: No module named gi'. Detect THAT
    # case specifically and turn the traceback into an actionable message - but
    # ONLY that case: a non-zero exit for any other reason must not be mis-reported
    # as a missing binding (which would wrongly block an otherwise-working host).
    # If the probe fails for something else, fall through and let the real command
    # run and surface its own error.
    "_fwver=$(firewall-cmd --version 2>&1); _fwrc=$?; "
    "if [ \"$_fwrc\" -ne 0 ] && printf '%s' \"$_fwver\" | grep -qiE \"gi\\.repository|no module named '?gi\"; then "
    "echo \"firewall-cmd is installed but its Python GObject bindings ('gi') are missing.\" >&2; "
    "echo 'Install them - openSUSE/SUSE: sudo zypper install python3-gobject; "
    "Fedora/RHEL: sudo dnf install python3-gobject; Debian/Ubuntu: sudo apt install python3-gi' >&2; exit 1; fi; "
)
_NFT_MISSING = (
    "if ! command -v nft >/dev/null 2>&1; then "
    "echo 'nftables is not installed on this host (package: nftables).' >&2; exit 1; fi; "
)
_IPTABLES_MISSING = (
    "if ! command -v iptables >/dev/null 2>&1; then "
    "echo 'iptables is not installed on this host (package: iptables).' >&2; exit 1; fi; "
)


# ---------------------------------------------------------
# Configure firewalld
# ---------------------------------------------------------
def cmd_firewalld_status() -> str:
    return (
        _FIREWALLD_MISSING +
        "echo '-- State --' && firewall-cmd --state 2>&1; "
        "echo; echo '-- Default zone --' && firewall-cmd --get-default-zone 2>&1; "
        "echo; echo '-- Active zones --' && firewall-cmd --get-active-zones 2>&1; "
        "echo; echo '-- systemctl status firewalld --' && systemctl status firewalld --no-pager 2>&1"
    )


def cmd_set_firewalld_enabled(enabled: bool) -> str:
    """Starts/enables or stops/disables the firewalld service (both
    the running state and whether it comes up at boot)."""
    if enabled:
        # Gate on systemctl's REAL exit code before the is-active diagnostic.
        # Previously this was `systemctl enable ...; <is-active diag>`, so when the
        # enable was refused by polkit (non-root RBAC path: "Interactive
        # authentication required" / "Access denied") but firewalld happened to be
        # running already, the is-active check passed and the whole command exited
        # 0 - masking the failure, reporting false success, AND stopping the agent
        # from escalating to sudo (it only escalates on a NON-zero exit that also
        # looks like a privilege error). Propagating rc lets the agent retry the
        # command under sudo, so the enable actually succeeds.
        return (
            "systemctl enable --now firewalld 2>&1; rc=$?; "
            "if [ \"$rc\" -ne 0 ]; then "
            "echo 'firewalld could not be enabled. An \"authentication required\" or "
            "\"access denied\" message above means this action needs root - mark this "
            "host \"password sudo\" or grant the console user NOPASSWD sudo.' >&2; "
            "exit \"$rc\"; fi; "
            + _FW_START_DIAG
        )
    return (
        "systemctl disable --now firewalld 2>&1 "
        "&& echo 'firewalld stopped and disabled.'"
    )


def cmd_set_default_zone(zone: str) -> str:
    zone = _validate_zone_name(zone)
    q_zone = shlex.quote(zone)
    return (
        _FIREWALLD_MISSING +
        f"firewall-cmd --set-default-zone={q_zone} 2>&1"
    )


def cmd_reload_firewalld() -> str:
    # Reload doesn't need the firewall-cmd Python CLI: `systemctl reload
    # firewalld` signals the daemon to re-read its config directly. So prefer
    # firewall-cmd, but fall back to systemctl when firewall-cmd is broken (e.g.
    # openSUSE minimal, where its 'gi' GObject bindings are missing) — the reload
    # still succeeds instead of dying with a Python traceback.
    return (
        "if firewall-cmd --reload 2>/dev/null; then echo 'firewalld configuration reloaded.'; "
        "elif systemctl reload firewalld 2>&1; then "
        "echo 'firewalld reloaded (via systemctl; the firewall-cmd CLI was unavailable).'; "
        "else echo 'Could not reload firewalld. Check it is installed and running "
        "(firewall-cmd may also be missing its python3-gobject gi bindings).' >&2; exit 1; fi"
    )


# ---------------------------------------------------------
# Open / close ports
# ---------------------------------------------------------
def cmd_list_ports(zone: str = "") -> str:
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    return (
        _FIREWALLD_MISSING +
        f"firewall-cmd {zone_flag}--list-all 2>&1"
    )


def cmd_open_port(port: str, protocol: str, zone: str = "", permanent: bool = True) -> str:
    port = _validate_port_spec(port)
    protocol = _validate_protocol(protocol)
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    perm_flag = "--permanent " if permanent else ""
    spec = shlex.quote(f"{port}/{protocol}")
    cmd = _FIREWALLD_MISSING + f"firewall-cmd {zone_flag}{perm_flag}--add-port={spec} 2>&1"
    if permanent:
        cmd += " && firewall-cmd --reload 2>&1"
    zone_suffix = f" in zone {zone}" if zone else ""
    return cmd + f" && echo 'Opened port {port}/{protocol}{zone_suffix}.'"


def cmd_close_port(port: str, protocol: str, zone: str = "", permanent: bool = True) -> str:
    port = _validate_port_spec(port)
    protocol = _validate_protocol(protocol)
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    perm_flag = "--permanent " if permanent else ""
    spec = shlex.quote(f"{port}/{protocol}")
    cmd = _FIREWALLD_MISSING + f"firewall-cmd {zone_flag}{perm_flag}--remove-port={spec} 2>&1"
    if permanent:
        cmd += " && firewall-cmd --reload 2>&1"
    zone_suffix = f" in zone {zone}" if zone else ""
    return cmd + f" && echo 'Closed port {port}/{protocol}{zone_suffix}.'"


# ---------------------------------------------------------
# Zones
# ---------------------------------------------------------
def cmd_list_zones() -> str:
    return (
        _FIREWALLD_MISSING +
        "echo '-- Zones --' && firewall-cmd --get-zones 2>&1; "
        "echo; echo '-- Default zone --' && firewall-cmd --get-default-zone 2>&1; "
        "echo; echo '-- Active zones --' && firewall-cmd --get-active-zones 2>&1"
    )


def cmd_create_zone(zone_name: str) -> str:
    zone_name = _validate_zone_name(zone_name)
    q_zone = shlex.quote(zone_name)
    return (
        _FIREWALLD_MISSING +
        f"firewall-cmd --permanent --new-zone={q_zone} 2>&1 "
        f"&& firewall-cmd --reload 2>&1 "
        f"&& echo 'Created zone {zone_name}.'"
    )


def cmd_delete_zone(zone_name: str) -> str:
    zone_name = _validate_zone_name(zone_name)
    q_zone = shlex.quote(zone_name)
    return (
        _FIREWALLD_MISSING +
        f"firewall-cmd --permanent --delete-zone={q_zone} 2>&1 "
        f"&& firewall-cmd --reload 2>&1 "
        f"&& echo 'Deleted zone {zone_name}.'"
    )


# ---------------------------------------------------------
# Rich rules
# ---------------------------------------------------------
def cmd_list_rich_rules(zone: str = "") -> str:
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    return (
        _FIREWALLD_MISSING +
        f"firewall-cmd {zone_flag}--list-rich-rules 2>&1"
    )


def cmd_add_rich_rule(rule: str, zone: str = "", permanent: bool = True) -> str:
    """`rule` is a full firewalld rich-rule expression, e.g.
    'rule family="ipv4" source address="192.168.0.0/24" service name="ssh" accept'."""
    rule = _validate_nonempty_line(rule, "Rich rule")
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    perm_flag = "--permanent " if permanent else ""
    q_rule = shlex.quote(rule)
    cmd = _FIREWALLD_MISSING + f"firewall-cmd {zone_flag}{perm_flag}--add-rich-rule={q_rule} 2>&1"
    if permanent:
        cmd += " && firewall-cmd --reload 2>&1"
    return cmd + " && echo 'Rich rule added.'"


def cmd_remove_rich_rule(rule: str, zone: str = "", permanent: bool = True) -> str:
    rule = _validate_nonempty_line(rule, "Rich rule")
    zone = (zone or "").strip()
    zone_flag = f"--zone={shlex.quote(zone)} " if zone else ""
    perm_flag = "--permanent " if permanent else ""
    q_rule = shlex.quote(rule)
    cmd = _FIREWALLD_MISSING + f"firewall-cmd {zone_flag}{perm_flag}--remove-rich-rule={q_rule} 2>&1"
    if permanent:
        cmd += " && firewall-cmd --reload 2>&1"
    return cmd + " && echo 'Rich rule removed.'"


# ---------------------------------------------------------
# nftables
# ---------------------------------------------------------
_VALID_NFT_FAMILIES = {"ip", "ip6", "inet", "arp", "bridge", "netdev"}
_VALID_NFT_HOOKS = {"prerouting", "input", "forward", "output", "postrouting"}


def _validate_nft_identifier(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError(f"{label} may only contain letters, numbers, dashes, and underscores.")
    return value


def _validate_nft_family(value: str) -> str:
    value = (value or "ip").strip().lower()
    if value not in _VALID_NFT_FAMILIES:
        raise ValueError(f"Family must be one of: {', '.join(sorted(_VALID_NFT_FAMILIES))}")
    return value


def cmd_nft_list_ruleset() -> str:
    return _NFT_MISSING + "nft -a list ruleset 2>&1"


def cmd_nft_add_table(family: str, table: str) -> str:
    family = _validate_nft_family(family)
    table = _validate_nft_identifier(table, "Table name")
    return (
        _NFT_MISSING +
        f"nft add table {family} {shlex.quote(table)} 2>&1 "
        f"&& echo 'Table {table} ({family}) created (or already existed).'"
    )


def cmd_nft_add_chain(family: str, table: str, chain: str, hook: str = "", priority: str = "0", policy: str = "accept") -> str:
    """Leave `hook` blank for a plain (non-base) chain used only as a
    jump target. Set it (input/output/forward/prerouting/postrouting)
    to create a base chain wired into the netfilter hook of that name."""
    family = _validate_nft_family(family)
    table = _validate_nft_identifier(table, "Table name")
    chain = _validate_nft_identifier(chain, "Chain name")
    hook = (hook or "").strip().lower()
    q_family = family
    q_table = shlex.quote(table)
    q_chain = shlex.quote(chain)

    if hook:
        if hook not in _VALID_NFT_HOOKS:
            raise ValueError(f"Hook must be one of: {', '.join(sorted(_VALID_NFT_HOOKS))} (or blank).")
        try:
            priority_n = int(str(priority).strip())
        except (TypeError, ValueError):
            raise ValueError("Priority must be a whole number.")
        policy = (policy or "accept").strip().lower()
        if policy not in {"accept", "drop"}:
            raise ValueError("Policy must be 'accept' or 'drop'.")
        spec = shlex.quote(f"{{ type filter hook {hook} priority {priority_n}; policy {policy}; }}")
        return (
            _NFT_MISSING +
            f"nft add chain {q_family} {q_table} {q_chain} {spec} 2>&1 "
            f"&& echo 'Base chain {chain} created on {table} ({family}), hook={hook}, policy={policy}.'"
        )

    return (
        _NFT_MISSING +
        f"nft add chain {q_family} {q_table} {q_chain} 2>&1 "
        f"&& echo 'Chain {chain} created on {table} ({family}).'"
    )


def cmd_nft_add_rule(family: str, table: str, chain: str, rule_spec: str) -> str:
    """`rule_spec` is the rest of an `nft add rule` line as you'd type
    it yourself, e.g. 'tcp dport 22 accept' or 'ip saddr 10.0.0.0/24 drop'."""
    family = _validate_nft_family(family)
    table = _validate_nft_identifier(table, "Table name")
    chain = _validate_nft_identifier(chain, "Chain name")
    q_rule = _resplit_quote(rule_spec, "Rule")
    return (
        _NFT_MISSING +
        f"nft add rule {family} {shlex.quote(table)} {shlex.quote(chain)} {q_rule} 2>&1 "
        f"&& echo 'Rule added.'"
    )


def cmd_nft_delete_rule(family: str, table: str, chain: str, handle) -> str:
    """`handle` is the rule handle number shown by `nft -a list ruleset`
    (List Ruleset above)."""
    family = _validate_nft_family(family)
    table = _validate_nft_identifier(table, "Table name")
    chain = _validate_nft_identifier(chain, "Chain name")
    try:
        handle_n = int(str(handle).strip())
    except (TypeError, ValueError):
        raise ValueError("Handle must be a whole number (see List Ruleset's -a output).")
    return (
        _NFT_MISSING +
        f"nft delete rule {family} {shlex.quote(table)} {shlex.quote(chain)} handle {handle_n} 2>&1 "
        f"&& echo 'Rule (handle {handle_n}) deleted.'"
    )


def cmd_nft_flush_ruleset() -> str:
    """Wipes every table/chain/rule in the live ruleset. Irreversible -
    confirm with the admin before dispatching this."""
    return _NFT_MISSING + "nft flush ruleset 2>&1 && echo 'nftables ruleset flushed.'"


def cmd_nft_save_persist() -> str:
    """Persist the live nftables ruleset so it survives a reboot. nft rules added at
    runtime are otherwise lost — this writes them to the distro's boot-loaded file
    (/etc/nftables.conf on most; /etc/sysconfig/nftables.conf on RHEL/SUSE) and enables
    nftables.service. nftables is the default backend on Arch and modern distros, so
    without this an operator's rules silently vanish on reboot."""
    return _NFT_MISSING + r"""
if [ -f /etc/sysconfig/nftables.conf ] || [ -d /etc/sysconfig ]; then _f=/etc/sysconfig/nftables.conf; else _f=/etc/nftables.conf; fi
{ echo '#!/usr/sbin/nft -f'; echo 'flush ruleset'; nft list ruleset; } > "$_f" 2>&1 \
  && chmod 0600 "$_f" \
  && (systemctl enable --now nftables.service 2>/dev/null || true) \
  && echo "Saved live ruleset to $_f and enabled nftables.service (loads on boot)."
""".strip()


# ---------------------------------------------------------
# iptables
# ---------------------------------------------------------
_VALID_IPTABLES_TABLES = {"filter", "nat", "mangle", "raw", "security"}


def _validate_iptables_table(value: str) -> str:
    value = (value or "filter").strip().lower()
    if value not in _VALID_IPTABLES_TABLES:
        raise ValueError(f"Table must be one of: {', '.join(sorted(_VALID_IPTABLES_TABLES))}")
    return value


def _validate_chain_name(value: str, label: str = "Chain") -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError(f"{label} may only contain letters, numbers, dashes, and underscores.")
    return value


def cmd_iptables_list(table: str = "filter") -> str:
    table = _validate_iptables_table(table)
    return _IPTABLES_MISSING + f"iptables -t {table} -L -n -v --line-numbers 2>&1"


def cmd_iptables_add_rule(table: str, chain: str, rule_spec: str, append: bool = True) -> str:
    """`rule_spec` is the rest of an iptables rule as you'd type it
    yourself, e.g. '-p tcp --dport 22 -j ACCEPT'. Appended to the end
    of the chain by default, or inserted at the top if `append` is
    False."""
    table = _validate_iptables_table(table)
    chain = _validate_chain_name(chain)
    q_rule = _resplit_quote(rule_spec, "Rule")
    flag = "-A" if append else "-I"
    verb = "appended to" if append else "inserted into"
    return (
        _IPTABLES_MISSING +
        f"iptables -t {table} {flag} {shlex.quote(chain)} {q_rule} 2>&1 "
        f"&& echo 'Rule {verb} {chain} ({table}).'"
    )


def cmd_iptables_delete_rule(table: str, chain: str, rule_spec_or_number: str) -> str:
    """`rule_spec_or_number` is either the exact rule spec to remove
    (e.g. '-p tcp --dport 22 -j ACCEPT') or a bare line number from
    List Rules (e.g. '3')."""
    table = _validate_iptables_table(table)
    chain = _validate_chain_name(chain)
    value = (rule_spec_or_number or "").strip()
    if not value:
        raise ValueError("Rule spec or line number is required.")
    if value.isdigit():
        target = value
    else:
        target = _resplit_quote(value, "Rule")
    return (
        _IPTABLES_MISSING +
        f"iptables -t {table} -D {shlex.quote(chain)} {target} 2>&1 "
        f"&& echo 'Rule removed from {chain} ({table}).'"
    )


def cmd_iptables_flush(table: str = "filter", chain: str = "") -> str:
    """Flushes every rule in `chain` (or the whole table if `chain` is
    left blank). Irreversible - confirm with the admin first."""
    table = _validate_iptables_table(table)
    chain = (chain or "").strip()
    if chain:
        chain = _validate_chain_name(chain)
        return (
            _IPTABLES_MISSING +
            f"iptables -t {table} -F {shlex.quote(chain)} 2>&1 "
            f"&& echo 'Flushed chain {chain} ({table}).'"
        )
    return _IPTABLES_MISSING + f"iptables -t {table} -F 2>&1 && echo 'Flushed table {table}.'"


def cmd_iptables_save_persist() -> str:
    """Persists the live ruleset so it survives a reboot, using
    whichever mechanism the host has available (Debian/Ubuntu's
    netfilter-persistent, or RHEL/CentOS's iptables-services)."""
    return r"""
if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save 2>&1
elif command -v service >/dev/null 2>&1 && service iptables save >/dev/null 2>&1; then
    echo "Saved via 'service iptables save'."
elif [ -d /etc/sysconfig ]; then
    iptables-save > /etc/sysconfig/iptables 2>&1 && echo "Saved to /etc/sysconfig/iptables."
elif [ -d /etc/iptables ] || command -v pacman >/dev/null 2>&1; then
    # Arch: iptables.service loads /etc/iptables/iptables.rules on boot.
    mkdir -p /etc/iptables && iptables-save > /etc/iptables/iptables.rules 2>&1 \
      && { command -v ip6tables-save >/dev/null 2>&1 && ip6tables-save > /etc/iptables/ip6tables.rules 2>/dev/null; true; } \
      && echo "Saved to /etc/iptables/iptables.rules (enable iptables.service to load on boot)."
else
    echo "No known persistence mechanism found (tried netfilter-persistent, service iptables save, /etc/sysconfig/iptables, /etc/iptables) - install iptables-persistent or iptables-services." >&2
    exit 1
fi
""".strip()


# ---------------------------------------------------------
# Installing a firewall backend + a backend-agnostic
# "what's actually listening" view
# ---------------------------------------------------------
_PKG_DETECT = (
    "if command -v dnf >/dev/null 2>&1; then PM='dnf install -y'; "
    "elif command -v yum >/dev/null 2>&1; then PM='yum install -y'; "
    "elif command -v zypper >/dev/null 2>&1; then PM='zypper --non-interactive install'; "
    "elif command -v apt-get >/dev/null 2>&1; then apt-get update >/dev/null 2>&1; "
    "PM='apt-get install -y'; "
    # Arch: -Sy refreshes the sync db, --needed is idempotent, --noconfirm keeps it
    # non-interactive. firewalld and ufw are both installable (Arch ships no firewall
    # by default). The harmless DEBIAN_FRONTEND prefix callers add is ignored here.
    "elif command -v pacman >/dev/null 2>&1; then PM='pacman -Sy --needed --noconfirm'; "
    "else echo 'No supported package manager found (dnf/yum/zypper/apt/pacman).' >&2; exit 1; fi; "
)

# After a start attempt, `systemctl enable --now` only reports "status=1/FAILURE"
# when firewalld's daemon dies — the actual reason (a missing backend, a Python
# traceback) lives in the journal. Surface it so the operator sees the real cause
# instead of a bare failure, and exit non-zero so the console flags it.
_FW_START_DIAG = (
    "if systemctl is-active --quiet firewalld; then "
    "echo 'firewalld enabled and started.'; "
    "else "
    "echo 'firewalld did NOT start — usually a missing backend (nftables/iptables) "
    "or Python binding on this host. Detail:'; "
    "systemctl status firewalld --no-pager -l 2>&1 | tail -n 12; "
    "echo '--- journal (firewalld) ---'; "
    "journalctl -xeu firewalld --no-pager -n 40 2>&1 | tail -n 40; "
    "exit 1; fi"
)


def cmd_install_firewalld() -> str:
    """Install firewalld via the host's package manager, then enable+start it.

    On openSUSE/SLES (zypper) also install python3-gobject: firewall-cmd imports
    the GObject ('gi') bindings at startup, and minimal openSUSE images ship
    firewalld without them, so every firewall-cmd call otherwise dies with
    'ModuleNotFoundError: No module named gi'. Pulling them in here means the
    Install button leaves a working firewall-cmd, not just a running daemon."""
    return (
        _PKG_DETECT
        + "DEBIAN_FRONTEND=noninteractive $PM firewalld 2>&1 "
        "|| { echo 'Could not install the firewalld package.' >&2; exit 1; }; "
        # openSUSE/SLES: BOTH the firewalld daemon and firewall-cmd are Python
        # and need the GObject ('gi') bindings; the daemon also needs a working
        # backend (nftables by default, iptables as a fallback). Minimal openSUSE
        # images ship firewalld without these, so the daemon exits 1 at start
        # ("status=1/FAILURE"). Pull them in best-effort so the daemon actually
        # comes up — install each separately so one missing package name doesn't
        # abort the whole transaction.
        "if command -v zypper >/dev/null 2>&1; then "
        "  for _p in python3-gobject nftables iptables; do "
        "    rpm -q \"$_p\" >/dev/null 2>&1 || zypper --non-interactive install \"$_p\" 2>&1 || "
        "      echo \"Warning: could not install $_p; firewalld may not start until it is present.\" >&2; "
        "  done; "
        "fi; "
        "systemctl enable --now firewalld 2>&1; "
        + _FW_START_DIAG
    )


def cmd_install_ufw() -> str:
    """Install ufw (Uncomplicated Firewall). Left disabled - turning it on is
    an explicit, connectivity-affecting step the admin does deliberately."""
    return (
        _PKG_DETECT
        + "DEBIAN_FRONTEND=noninteractive $PM ufw 2>&1 && "
        "echo 'ufw installed (not enabled). Turn it on later with the \"Enable/disable ufw\" action.'"
    )


# ---------------------------------------------------------
# ufw (Uncomplicated Firewall) - the default firewall front-end on
# Debian/Ubuntu. firewalld covers RHEL/Fedora/SUSE; ufw is its
# Debian-world counterpart, so managing it needs the same status /
# on-off / rule verbs firewalld already has above.
# ---------------------------------------------------------
_UFW_MISSING = (
    "if ! command -v ufw >/dev/null 2>&1; then "
    "echo 'ufw is not installed on this host (package: ufw). Use \"Install ufw\" first.' >&2; "
    "exit 1; fi; "
)

# ufw port ranges use a colon (6000:6010), NOT the hyphen firewalld uses.
def _validate_ufw_port_spec(value: str, label: str = "Port") -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    parts = value.split(":")
    if len(parts) not in (1, 2):
        raise ValueError(f"{label} must be a single port or a range like 6000:6010.")
    nums = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            raise ValueError(f"{label} must be numeric.")
        if not (1 <= n <= 65535):
            raise ValueError(f"{label} must be between 1 and 65535.")
        nums.append(n)
    if len(nums) == 2 and nums[0] >= nums[1]:
        raise ValueError(f"{label} range must have a lower start than end.")
    return value


def _validate_cidr(value: str, label: str = "Source") -> str:
    """Optional IP or CIDR (e.g. 10.0.0.0/24). Blank means 'any'."""
    import ipaddress
    value = (value or "").strip()
    if not value:
        return ""
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError(f"{label} must be an IP or CIDR like 10.0.0.0/24 (leave blank for any).")
    return value


def cmd_ufw_status() -> str:
    """ufw state. Distinguishes the three cases an operator actually cares about:
    NOT INSTALLED (the guard above), installed-but-INACTIVE ('Status: inactive'),
    and ACTIVE (with the numbered rule list). Also shows whether it starts at
    boot, so 'off right now' vs 'off and won't come back' are distinguishable."""
    return (
        _UFW_MISSING +
        "echo '-- ufw status --'; ufw status verbose 2>&1; "
        "echo; echo '-- numbered rules --'; ufw status numbered 2>&1; "
        "echo; printf 'Starts at boot: '; systemctl is-enabled ufw 2>&1"
    )


def cmd_set_ufw_enabled(enabled: bool) -> str:
    """Turn ufw on or off. Enabling uses `ufw --force enable` so it doesn't hang
    on the interactive 'this may disrupt existing ssh connections' prompt, and
    also enables the service so it survives a reboot; disabling stops it and
    keeps the configured rules (re-enabling re-applies them)."""
    if enabled:
        # Gate on ufw's exit code before the trailing `ufw status` (which always
        # exits 0 and would otherwise mask a failed enable and block the agent's
        # sudo escalation - see cmd_set_firewalld_enabled).
        return (
            _UFW_MISSING +
            "ufw --force enable 2>&1; rc=$?; "
            "if [ \"$rc\" -ne 0 ]; then "
            "echo 'ufw could not be enabled. If this needs root, mark this host "
            "\"password sudo\" or grant the console user NOPASSWD sudo.' >&2; exit \"$rc\"; fi; "
            "systemctl enable ufw >/dev/null 2>&1; "
            "echo; ufw status verbose 2>&1"
        )
    return (
        _UFW_MISSING +
        "ufw disable 2>&1; rc=$?; "
        "if [ \"$rc\" -ne 0 ]; then echo 'ufw could not be disabled (needs root).' >&2; exit \"$rc\"; fi; "
        "systemctl disable ufw >/dev/null 2>&1; "
        "echo 'ufw disabled (rules kept; re-enable to re-apply them).'"
    )


_UFW_RULE_ACTIONS = {"allow", "deny", "reject", "limit"}


def cmd_ufw_add_rule(action: str, port: str, protocol: str = "", source: str = "") -> str:
    """Add a ufw rule. `action` is allow/deny/reject/limit, `port` a single
    port or N:M range, `protocol` tcp/udp (blank = both), `source` an optional
    IP/CIDR to scope the rule to (blank = from anywhere)."""
    action = (action or "").strip().lower()
    if action not in _UFW_RULE_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(sorted(_UFW_RULE_ACTIONS))}.")
    port = _validate_ufw_port_spec(port)
    protocol = (protocol or "").strip().lower()
    if protocol and protocol not in _VALID_PROTOCOLS:
        raise ValueError(f"Protocol must be tcp, udp, or blank for both.")
    source = _validate_cidr(source)
    # ufw's own syntax differs for scoped vs unscoped rules. A port range REQUIRES
    # a protocol (ufw rejects '6000:6010' without one).
    if ":" in port and not protocol:
        raise ValueError("A port range needs a protocol (tcp or udp).")
    spec = port + (f"/{protocol}" if protocol else "")
    if source:
        # `ufw allow from 10.0.0.0/24 to any port 22 proto tcp`
        proto_clause = f" proto {protocol}" if protocol else ""
        body = f"from {shlex.quote(source)} to any port {shlex.quote(port)}{proto_clause}"
    else:
        # `ufw allow 22/tcp`
        body = shlex.quote(spec)
    return (
        _UFW_MISSING +
        f"ufw {action} {body} 2>&1 && echo 'Rule added: {action} {spec}"
        + (f' from {source}' if source else '') + "'"
    )


def cmd_ufw_delete_rule(number: str) -> str:
    """Delete a ufw rule by its NUMBER (the [n] shown by ufw status numbered /
    the Status action). `ufw --force delete` skips the y/n confirmation prompt."""
    try:
        n = int(str(number).strip())
    except (TypeError, ValueError):
        raise ValueError("Rule number must be a whole number (see the numbered list in Status).")
    if n < 1:
        raise ValueError("Rule number must be 1 or greater.")
    return (
        _UFW_MISSING +
        f"ufw --force delete {n} 2>&1"
    )


_UFW_DEFAULT_POLICIES = {"allow", "deny", "reject"}
_UFW_DEFAULT_DIRECTIONS = {"incoming", "outgoing", "routed"}


def cmd_ufw_set_default(policy: str, direction: str = "incoming") -> str:
    """Set ufw's default policy for a direction (deny incoming / allow outgoing
    is the usual hardened baseline)."""
    policy = (policy or "").strip().lower()
    if policy not in _UFW_DEFAULT_POLICIES:
        raise ValueError(f"Policy must be one of: {', '.join(sorted(_UFW_DEFAULT_POLICIES))}.")
    direction = (direction or "incoming").strip().lower()
    if direction not in _UFW_DEFAULT_DIRECTIONS:
        raise ValueError(f"Direction must be one of: {', '.join(sorted(_UFW_DEFAULT_DIRECTIONS))}.")
    return (
        _UFW_MISSING +
        f"ufw default {policy} {direction} 2>&1 "
        f"&& echo 'Default {direction} policy set to {policy}.'"
    )


def cmd_ufw_reset() -> str:
    """Wipe ALL ufw rules back to installed defaults and disable it. Irreversible
    - confirm with the admin before dispatching."""
    return (
        _UFW_MISSING +
        "ufw --force reset 2>&1 && echo 'ufw reset to defaults and disabled.'"
    )


def cmd_list_listening_ports() -> str:
    """Every actually-listening TCP/UDP socket on the host, with the owning
    process - independent of which firewall is in use. Answers 'what ports are
    open on this box right now?' rather than 'what does the firewall allow?'.

    The raw `ss -tulpn` output is hard to read, so it's reformatted into an
    aligned PROTO / PORT / LISTEN ADDRESS / PROCESS table sorted by port.
    (Process names only show when run with enough privilege; otherwise '-'.)"""
    awk = (
        "awk '{proto=$1; la=$5; nc=split(la,a,\":\"); port=a[nc]; "
        "addr=substr(la,1,length(la)-length(port)-1); if(addr==\"*\")addr=\"0.0.0.0\"; "
        "proc=\"-\"; pid=\"\"; "
        "if(match($0,/\"[^\"]+\"/)){proc=substr($0,RSTART+1,RLENGTH-2)} "
        "if(match($0,/pid=[0-9]+/)){pid=substr($0,RSTART+4,RLENGTH-4)} "
        "ps=proc; if(pid!=\"\")ps=ps\" (pid \"pid\")\"; "
        "printf \"%-5s %-7s %-26s %s\\n\", toupper(proto), port, addr, ps}'"
    )
    return (
        "if command -v ss >/dev/null 2>&1; then "
        "printf \"%-5s %-7s %-26s %s\\n\" PROTO PORT \"LISTEN ADDRESS\" PROCESS; "
        "printf \"%-5s %-7s %-26s %s\\n\" ----- ---- \"--------------\" -------; "
        "ss -H -tulpn 2>/dev/null | " + awk + " | sort -k2 -n; "
        "elif command -v netstat >/dev/null 2>&1; then "
        "echo '(ss unavailable; raw netstat output)'; netstat -tulpn 2>&1; "
        "else echo 'Neither ss nor netstat is available on this host.' >&2; exit 1; fi"
    )
