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


def _validate_nonempty_line(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} cannot span multiple lines.")
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
        return (
            "systemctl enable --now firewalld 2>&1 "
            "&& echo 'firewalld enabled and started.'"
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
    return _FIREWALLD_MISSING + "firewall-cmd --reload 2>&1 && echo 'firewalld configuration reloaded.'"


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
else
    echo "No known persistence mechanism found (tried netfilter-persistent, service iptables save, /etc/sysconfig/iptables) - install iptables-persistent or iptables-services." >&2
    exit 1
fi
""".strip()
