"""A firewall rule spec may describe a rule. It may not reconfigure the tool.

`-p tcp --dport 22 -j ACCEPT` is the whole point of the rule-spec field, so the
tokens in it legitimately start with a dash and _resplit_quote cannot simply
refuse those the way _pkg_quote_list does for package names. It re-tokenises
with shlex and re-quotes each token, which makes the value shell-safe — and
leaves it free to be read as a GENERAL option by the tool itself.

Two of iptables' general options matter, and both were reachable. Verified
against iptables v1.8.10 on the build host:

  * `--modprobe=<command>` — the tool's own --help says "try to insert modules
    using this command", and iptables runs it as root the moment a rule needs
    a module (`-m conntrack`, a nat table, …). A sudoers entry granting only
    /usr/sbin/iptables became arbitrary root.
      iptables -t filter -A INPUT --modprobe=/tmp/x.sh -p tcp --dport 22 \
               -m conntrack --ctstate NEW -j ACCEPT      → accepted, rule added

  * `-t` / `--table` — iptables permutes its arguments, so a `-t` inside the
    rule spec overrides the table the builder validated:
      iptables -t filter -A INPUT -t nat -p tcp -j ACCEPT → the rule landed in
                                                            the NAT table

The codebase already names this exact class for package managers, in
_pkg_quote_list: "shlex.quote stops shell-metacharacter injection but NOT
*option* injection". It just was not applied here.

nft is different and the difference is load-bearing, so it is pinned below: its
parser refuses options that follow a command ("options must be specified before
commands"), so `nft add rule … -f /etc/shadow` is rejected by nft itself. The
guard is kept for it anyway, because that is a property of the parser rather
than of the command we build.
"""
import shutil
import subprocess

import pytest

from client import api

# Reached through the registry in the console; called directly here so the
# argument shapes are explicit.
_IPT_ADD = api.cmd_iptables_add_rule
_IPT_DEL = api.cmd_iptables_delete_rule
_NFT_ADD = api.cmd_nft_add_rule


def _rejected(fn, *a, **kw):
    with pytest.raises(ValueError) as ei:
        fn(*a, **kw)
    return str(ei.value)


# ---------------------------------------------------------------------------
# iptables
# ---------------------------------------------------------------------------
_RUNS_A_COMMAND = [
    "--modprobe=/tmp/evil.sh",
    "--modprobe /tmp/evil.sh",
    "-M/tmp/evil.sh",
    "-M /tmp/evil.sh",
]
_OVERRIDES_THE_TABLE = [
    "--table=nat",
    "--table nat",
    "-tnat",
    "-t nat",
]


@pytest.mark.parametrize("bad", _RUNS_A_COMMAND + _OVERRIDES_THE_TABLE)
def test_iptables_refuses_a_general_option_in_the_rule_spec(bad):
    spec = f"{bad} -p tcp --dport 22 -j ACCEPT"
    msg = _rejected(_IPT_ADD, "filter", "INPUT", spec)
    assert "option" in msg.lower(), msg
    # …wherever in the spec it appears. iptables permutes, so position is not a
    # defence.
    msg = _rejected(_IPT_ADD, "filter", "INPUT", f"-p tcp --dport 22 -j ACCEPT {bad}")
    assert "option" in msg.lower(), msg
    # …and on the delete path, which takes the same free-text spec.
    _rejected(_IPT_DEL, "filter", "INPUT", spec)


def test_iptables_refuses_a_bundled_short_option():
    """`-nM/tmp/x` is `-n -M /tmp/x`. Blocking only the exact `-M` form leaves
    this open, so a single-dash token may not bundle at all — nothing in a real
    rule spec does."""
    for bad in ("-nM/tmp/evil.sh", "-4M/tmp/evil.sh", "-fM/tmp/evil.sh", "-ntnat"):
        msg = _rejected(_IPT_ADD, "filter", "INPUT", f"{bad} -j ACCEPT")
        assert "option" in msg.lower(), (bad, msg)


_REAL_SPECS = [
    "-p tcp --dport 22 -j ACCEPT",
    "-p tcp -m tcp --dport 80 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT",
    "-s 10.0.0.0/8 -d 192.168.1.0/24 -j DROP",
    "-i eth0 -o eth1 -g MYCHAIN",
    "-p icmp --icmp-type echo-request -m limit --limit 1/s -j ACCEPT",
    "-4 -p udp --dport 53 -j ACCEPT",
    "-f -j DROP",                            # fragment, a no-argument short opt
    "-ptcp --dport 443 -jACCEPT",             # attached values
    "-p tcp -m multiport --dports 80,443 -j ACCEPT",
    "-m comment --comment 'web tier, t and M in the text' -j ACCEPT",
    "! -s 10.0.0.1 -j DROP",                  # negation
    "-j LOG --log-prefix 'DROP-TCP: '",
]


@pytest.mark.parametrize("spec", _REAL_SPECS)
def test_a_real_rule_spec_still_works(spec):
    """The guard is worth nothing if it breaks the field. Every one of these is
    an ordinary rule a sysadmin types, including ones whose VALUES contain the
    blocked letters."""
    out = _IPT_ADD("filter", "INPUT", spec)
    assert "iptables -t filter -A INPUT " in out
    _IPT_DEL("filter", "INPUT", spec)


def test_a_line_number_is_still_a_valid_delete_target():
    assert "iptables -t filter -D INPUT 3 " in _IPT_DEL("filter", "INPUT", "3")


def test_the_table_the_builder_validated_is_the_table_used():
    out = _IPT_ADD("nat", "PREROUTING", "-p tcp --dport 80 -j REDIRECT --to-port 8080")
    assert out.count("-t ") == 1 and "-t nat " in out


# ---------------------------------------------------------------------------
# nft
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["-f /etc/shadow", "--file /etc/shadow",
                                 "-I /tmp", "--includepath /tmp", "-i", "--interactive"])
def test_nft_refuses_the_options_that_read_a_file(bad):
    msg = _rejected(_NFT_ADD, "inet", "filter", "input", f"tcp dport 22 accept {bad}")
    assert "option" in msg.lower(), msg


@pytest.mark.parametrize("spec", ["tcp dport 22 accept",
                                  "ip saddr 10.0.0.0/24 drop",
                                  "ct state new,established accept",
                                  'meta l4proto tcp counter accept comment "web"'])
def test_a_real_nft_rule_still_works(spec):
    out = _NFT_ADD("inet", "filter", "input", spec)
    assert "nft add rule inet filter input " in out


@pytest.mark.skipif(not shutil.which("nft"), reason="nft not installed")
def test_nfts_own_parser_is_the_primary_control_for_nft():
    """Pins WHY the nft half is defence in depth rather than the fix: this nft
    rejects an option that follows a command. If a future nft permutes like
    iptables does, this fails and the guard above becomes load-bearing."""
    r = subprocess.run(["nft", "add", "rule", "inet", "t", "c", "-f", "/nonexistent/zzz"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "options must be specified before commands" in (r.stderr + r.stdout)
