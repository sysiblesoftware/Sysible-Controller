"""sysible_ctl speaks ONE syntax, and never widens a target you named.

The CLI grew four overlapping ad-hoc forms with different command sets, and the
seams between them produced two classes of bug:

  * `update all` worked but `update controller` was rejected as "unknown or
    ambiguous" — as were `logs/backup/destroy <product>`. Half the verbs could
    only be written one way round, and the help advertised only that way.

  * far worse, `stop controller` SILENTLY STOPPED EVERY PRODUCT. main() looked
    for the literal `all` in slot 2, then fell through to a bare-command rule
    that ignored slot 2 entirely — so naming one product became a fleet-wide
    action with nothing said. Same for start/restart/status/up/build, and
    `destroy all -v` dropped the -v and kept every data volume.

The grammar is now two slots — a TARGET (product id/alias, or `all`) and a
COMMAND — writable in either order, with defaults when one is omitted. These
tests drive the REAL main() with every p_* action stubbed, so what they assert
is the routing decision itself: which action, against which target, with which
arguments.
"""
import os
import re
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysible_ctl")

PRODUCTS = ["controller", "slep", "connect", "slop"]
# Every command an operator can type, with the action it must resolve to.
# build/install are documented aliases of up.
COMMANDS = {"update": "update", "up": "up", "build": "up", "install": "up",
            "status": "status", "restart": "restart", "start": "start",
            "stop": "stop", "backup": "backup", "destroy": "destroy"}


@pytest.fixture(scope="module")
def ctl_lib(tmp_path_factory):
    """The script with its trailing `main "$@"` removed, so a test can source the
    real functions and call main() itself."""
    src = open(CTL, encoding="utf-8").read()
    lib = tmp_path_factory.mktemp("ctl") / "ctl.lib.sh"
    lib.write_text(re.sub(r'^main "\$@"\s*$', "", src, flags=re.M))
    return str(lib)


# Stub every action so nothing touches docker, and print the routing decision in
# a form the test can parse: "<action> <target> [args]".
_STUBS = r"""
_need_docker() { :; }
_route() { local action="$1" target="$2"; shift 2; echo "ROUTE $action $target $*"; }
p_up()      { _route up      "$@"; }
p_update()  { _route update  "$@"; }
p_status()  { _route status  "$@"; }
p_logs()    { _route logs    "$@"; }
p_restart() { _route restart "$@"; }
p_start()   { _route start   "$@"; }
p_stop()    { _route stop    "$@"; }
p_backup()  { _route backup  "$@"; }
p_destroy() { _route destroy "$@"; }
p_all()         { local c="$1"; shift; echo "ROUTE $c ALL $*"; }
cmd_install()   { echo "ROUTE link-cli"; }
cmd_uninstall() { echo "ROUTE unlink-cli"; }
usage()         { echo "ROUTE usage"; }
"""


def route(ctl_lib, *argv):
    """Run `sysible_ctl <argv…>` and return its routing decision, or ERROR:<msg>."""
    script = f'. "{ctl_lib}"\n' + textwrap.dedent(_STUBS) + "\nmain " + " ".join(
        f"'{a}'" for a in argv)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HOME": os.environ.get("HOME", "/root")}, timeout=60)
    # A refusal exits non-zero. Check that FIRST: the refusal path also prints the
    # help, so scanning for a routing line would report the stubbed usage() and
    # make every rejected command look like it had been accepted.
    if r.returncode != 0:
        return "ERROR:" + (r.stderr.strip() or r.stdout.strip())
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        if line.startswith("ROUTE "):
            return line[len("ROUTE "):].strip()
    return "ERROR:" + (r.stderr.strip() or r.stdout.strip())


@pytest.mark.parametrize("typed,action", sorted(COMMANDS.items()))
@pytest.mark.parametrize("product", PRODUCTS)
class TestEitherOrder:
    """The heart of it: for every command and every product, the two orders are
    the same command. `update controller` used to be "unknown or ambiguous"."""

    # Compared on (action, target): `destroy` also carries its wipe flag, which
    # TestArgumentsSurviveBothOrders pins separately.
    def test_target_first(self, ctl_lib, product, typed, action):
        assert route(ctl_lib, product, typed).split()[:2] == [action, product]

    def test_command_first(self, ctl_lib, product, typed, action):
        assert route(ctl_lib, typed, product).split()[:2] == [action, product]

    def test_the_two_orders_agree(self, ctl_lib, product, typed, action):
        assert route(ctl_lib, product, typed) == route(ctl_lib, typed, product)


@pytest.mark.parametrize("typed,action", sorted(COMMANDS.items()))
class TestFleetWide:
    def test_all_after_the_command(self, ctl_lib, typed, action):
        assert route(ctl_lib, typed, "all").split()[:2] == [action, "ALL"]

    def test_all_before_the_command(self, ctl_lib, typed, action):
        assert route(ctl_lib, "all", typed).split()[:2] == [action, "ALL"]

    def test_no_target_means_every_product(self, ctl_lib, typed, action):
        """`update` alone was rejected while `status` alone worked."""
        if typed == "install":
            pytest.skip("bare `install` is the documented exception — it links the CLI")
        assert route(ctl_lib, typed).split()[:2] == [action, "ALL"]


class TestANamedProductIsNeverWidened:
    """The outage-shaped bug: `stop controller` stopped SLEP, Connect and the SLOP
    gateway too, and said nothing about it."""

    @pytest.mark.parametrize("typed", ["stop", "start", "restart", "status", "up", "build"])
    @pytest.mark.parametrize("product", PRODUCTS)
    def test_it_acts_on_that_product_alone(self, ctl_lib, typed, product):
        assert route(ctl_lib, typed, product).split()[1] == product
        assert "ALL" not in route(ctl_lib, typed, product)


class TestDefaults:
    @pytest.mark.parametrize("product", PRODUCTS + ["ce"])
    def test_a_bare_product_reports_its_status(self, ctl_lib, product):
        expected = "controller" if product == "ce" else product
        assert route(ctl_lib, product) == f"status {expected}"

    def test_the_ce_alias_resolves_in_both_orders(self, ctl_lib):
        assert route(ctl_lib, "ce", "update") == "update controller"
        assert route(ctl_lib, "update", "ce") == "update controller"

    def test_no_arguments_at_all_prints_the_help(self, ctl_lib):
        assert route(ctl_lib) == "usage"

    def test_bare_install_still_links_the_cli(self, ctl_lib):
        """Documented, and install.sh/docs depend on it — the ONE exception."""
        assert route(ctl_lib, "install") == "link-cli"

    def test_install_with_a_target_builds_it(self, ctl_lib):
        assert route(ctl_lib, "install", "slep") == "up slep"
        assert route(ctl_lib, "slep", "install") == "up slep"
        assert route(ctl_lib, "install", "all") == "up ALL"


class TestArgumentsSurviveBothOrders:
    def test_the_destroy_volume_flag_means_the_same_everywhere(self, ctl_lib):
        """`destroy all -v` used to drop the flag and keep every data volume —
        the operator asked for a wipe and silently got the opposite."""
        for argv in (("controller", "destroy", "-v"), ("destroy", "controller", "-v")):
            assert route(ctl_lib, *argv) == "destroy controller 1"
        for argv in (("destroy", "all", "-v"), ("all", "destroy", "-v"), ("destroy", "-v")):
            assert route(ctl_lib, *argv) == "destroy ALL 1"

    def test_destroy_without_the_flag_keeps_the_volume(self, ctl_lib):
        assert route(ctl_lib, "controller", "destroy") == "destroy controller 0"
        assert route(ctl_lib, "destroy", "all") == "destroy ALL 0"

    def test_the_controller_address_reaches_up_either_way(self, ctl_lib):
        assert route(ctl_lib, "controller", "up", "192.168.1.50") == "up controller 192.168.1.50"
        assert route(ctl_lib, "up", "controller", "192.168.1.50") == "up controller 192.168.1.50"

    def test_the_log_tail_reaches_logs_either_way(self, ctl_lib):
        assert route(ctl_lib, "controller", "logs", "1000") == "logs controller 1000"
        assert route(ctl_lib, "logs", "controller", "1000") == "logs controller 1000"


class TestRefusalsInsteadOfGuesses:
    def test_a_mistyped_product_never_becomes_the_whole_fleet(self, ctl_lib):
        """`update slpe` must not quietly update every product."""
        out = route(ctl_lib, "update", "slpe")
        assert out.startswith("ERROR:"), out
        assert "slpe" in out and "not a product" in out

    def test_a_mistyped_command_is_named(self, ctl_lib):
        out = route(ctl_lib, "controller", "frobnicate")
        assert out.startswith("ERROR:") and "frobnicate" in out

    def test_an_unknown_first_word_is_refused(self, ctl_lib):
        assert route(ctl_lib, "frobnicate").startswith("ERROR:")
        assert route(ctl_lib, "frobnicate", "controller").startswith("ERROR:")

    def test_following_the_logs_of_every_product_is_refused(self, ctl_lib):
        """`logs all` tailed the FIRST product forever and never reached the
        others, which reads as 'the rest have no logs'."""
        for argv in (("logs", "all"), ("all", "logs"), ("logs",)):
            out = route(ctl_lib, *argv)
            assert out.startswith("ERROR:"), f"{argv} -> {out}"
            assert "one product" in out


class TestTheHelpDescribesWhatTheParserDoes:
    """The old help advertised `<product> <command>` and `<command> all` only, so
    the forms it omitted looked unsupported even where they worked."""

    @pytest.fixture(scope="class")
    def help_text(self, ctl_lib):
        r = subprocess.run(["bash", "-c", f'. "{ctl_lib}"\nmain help'],
                           capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                "HOME": os.environ.get("HOME", "/root")}, timeout=60)
        return r.stdout

    def test_both_orders_are_shown(self, help_text):
        assert "update controller" in help_text
        assert "controller update" in help_text

    def test_every_command_in_known_cmds_is_documented(self, ctl_lib, help_text):
        r = subprocess.run(["bash", "-c", f'. "{ctl_lib}"\nprintf %s "$KNOWN_CMDS"'],
                           capture_output=True, text=True, timeout=60)
        for cmd in r.stdout.split():
            assert cmd in help_text, f"'{cmd}' is a real command but the help never names it"
