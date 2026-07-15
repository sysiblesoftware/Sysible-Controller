"""ufw (Uncomplicated Firewall) command builders.

ufw is the Debian/Ubuntu firewall front-end; before this it had only an
"Install ufw" button — no status, on/off, or rule management. These tests cover
the new builders and, crucially, that operator-supplied values (port, source
CIDR, rule number) are whitelisted/validated so nothing hostile reaches the shell.
"""
import subprocess

from client import _api_firewall as fw


def _bash_ok(script: str) -> bool:
    r = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    return r.returncode == 0


class TestUfwStatus:
    def test_status_distinguishes_not_installed(self):
        s = fw.cmd_ufw_status()
        # The not-installed case is a distinct, actionable message — not the same
        # thing as "inactive". This is the gap the operator reported.
        assert "not installed" in s
        assert "command -v ufw" in s
        # inactive vs active is surfaced by `ufw status verbose` (Status: active/inactive)
        assert "ufw status verbose" in s
        # and boot-persistence is shown so "off now" != "off for good"
        assert "is-enabled ufw" in s
        assert _bash_ok(s)

    def test_enable_is_noninteractive_and_persists(self):
        s = fw.cmd_set_ufw_enabled(True)
        assert "ufw --force enable" in s          # no interactive SSH-disruption prompt
        assert "systemctl enable ufw" in s        # survives reboot
        assert _bash_ok(s)

    def test_disable_keeps_rules(self):
        s = fw.cmd_set_ufw_enabled(False)
        assert "ufw disable" in s
        assert "systemctl disable ufw" in s
        assert _bash_ok(s)


class TestUfwRules:
    def test_simple_allow(self):
        s = fw.cmd_ufw_add_rule("allow", "22", "tcp", "")
        assert "ufw allow 22/tcp" in s
        assert _bash_ok(s)

    def test_scoped_rule_uses_from_syntax(self):
        s = fw.cmd_ufw_add_rule("allow", "22", "tcp", "10.0.0.0/24")
        assert "from 10.0.0.0/24 to any port 22 proto tcp" in s
        assert _bash_ok(s)

    def test_port_range_requires_protocol(self):
        # ufw itself rejects a bare range with no protocol — fail fast in Python.
        try:
            fw.cmd_ufw_add_rule("allow", "6000:6010", "", "")
            raise AssertionError("range without protocol should raise")
        except ValueError:
            pass
        s = fw.cmd_ufw_add_rule("allow", "6000:6010", "tcp", "")
        assert "6000:6010/tcp" in s
        assert _bash_ok(s)

    def test_bad_action_rejected(self):
        for bad in ("drop", "accept", "; rm -rf /", ""):
            try:
                fw.cmd_ufw_add_rule(bad, "22", "tcp", "")
                raise AssertionError(f"action {bad!r} should raise")
            except ValueError:
                pass

    def test_bad_port_rejected(self):
        for bad in ("abc", "0", "70000", "22; reboot", "$(id)"):
            try:
                fw.cmd_ufw_add_rule("allow", bad, "tcp", "")
                raise AssertionError(f"port {bad!r} should raise")
            except ValueError:
                pass

    def test_bad_source_rejected(self):
        for bad in ("not-a-cidr", "10.0.0.0/24; rm -rf /", "$(hostname)", "10.0.0.0/99"):
            try:
                fw.cmd_ufw_add_rule("allow", "22", "tcp", bad)
                raise AssertionError(f"source {bad!r} should raise")
            except ValueError:
                pass

    def test_delete_by_number_only(self):
        s = fw.cmd_ufw_delete_rule("3")
        assert "ufw --force delete 3" in s
        assert _bash_ok(s)
        for bad in ("0", "-1", "two", "1; reboot", ""):
            try:
                fw.cmd_ufw_delete_rule(bad)
                raise AssertionError(f"rule number {bad!r} should raise")
            except ValueError:
                pass


class TestUfwDefaultsAndReset:
    def test_default_policy_whitelisted(self):
        s = fw.cmd_ufw_set_default("deny", "incoming")
        assert "ufw default deny incoming" in s
        assert _bash_ok(s)
        for pol, dirn in (("burn", "incoming"), ("deny", "sideways"), ("$(id)", "incoming")):
            try:
                fw.cmd_ufw_set_default(pol, dirn)
                raise AssertionError(f"{pol}/{dirn} should raise")
            except ValueError:
                pass

    def test_reset_is_forced(self):
        s = fw.cmd_ufw_reset()
        assert "ufw --force reset" in s
        assert _bash_ok(s)
