"""Bulk enrollment from a hosts file.

Enrollment took one host at a time: a name, an address, a user, a password.
That is the right shape for the first host and the wrong shape for the
fortieth, and the list an operator already has is almost always /etc/hosts.

The design decision worth testing is what is NOT here: there is no bulk
enrollment endpoint. /remote/hosts-file/preview reads and enrolls nothing; the
browser posts each ticked candidate back through the ordinary /enroll-ssh
route. A second path onto a host would be a second place for the duplicate-IP
check, the host limit and the superuser gate to be forgotten.

Most of what makes a hosts-file parser worth having is everything in a real
/etc/hosts that is NOT a machine you want to enroll.

Run: pytest tests/
"""
import ipaddress

import pytest

from backend import hosts_file
from backend.models.remote_models import HOST_ADDR_RE, HOST_NAME_RE

DEBIAN = """\
127.0.0.1\tlocalhost
127.0.1.1\tsysible-host.localdomain sysible-host

# The following lines are desirable for IPv6 capable hosts
::1     ip6-localhost ip6-loopback
fe00::0 ip6-localnet
ff00::0 ip6-mcastprefix
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters

192.168.155.135  ubuntu-lab
10.0.0.5         db.internal db
10.0.0.6         web-01
"""


def _by_addr(rows, addr):
    return next(r for r in rows if r["address"] == addr)


def _names(rows):
    return [r["name"] for r in rows if r["enrollable"]]


# ---- a real /etc/hosts ----------------------------------------------------
def test_a_stock_debian_hosts_file_offers_only_the_real_machines():
    """Eight of the eleven lines in a stock file are loopback, the host's own
    name, or the IPv6 boilerplate Debian ships. Offering those as machines to
    enroll would bury the three that matter."""
    rows = hosts_file.parse(DEBIAN)
    assert _names(rows) == ["ubuntu-lab", "db", "web-01"], _names(rows)


@pytest.mark.parametrize("addr,why", [
    ("127.0.0.1", "loopback"),
    ("127.0.1.1", "loopback"),
    ("::1", "loopback"),
    ("ff02::1", "multicast"),
    ("0.0.0.0", "unspecified"),
    ("169.254.1.1", "link-local"),
])
def test_addresses_that_are_not_machines_are_refused(addr, why):
    rows = hosts_file.parse(f"{addr} somename\n")
    assert rows[0]["enrollable"] is False
    assert rows[0]["skip"] == why, rows[0]


def test_every_line_comes_back_including_the_skipped_ones():
    """An operator looking at a 40-line file that produced 12 candidates has to
    be able to see where the other 28 went. A parser that returns only the 12
    makes that unanswerable."""
    rows = hosts_file.parse(DEBIAN)
    # Counted from the sample rather than written down, so editing DEBIAN
    # cannot quietly make this assertion about a different file.
    content = [ln for ln in DEBIAN.splitlines()
               if ln.split("#", 1)[0].strip()]
    assert len(rows) == len(content), \
        f"{len(rows)} rows for {len(content)} content lines"
    assert all(r["skip_reason"] for r in rows if not r["enrollable"]), \
        "a line was dropped without saying why"


def test_comments_and_blank_lines_are_not_rows():
    rows = hosts_file.parse("# just a comment\n\n   \n10.0.0.1 a\n")
    assert len(rows) == 1 and rows[0]["name"] == "a"


def test_a_trailing_comment_does_not_become_a_hostname():
    rows = hosts_file.parse("10.0.0.1 db   # the primary\n")
    assert rows[0]["names"] == ["db"], rows[0]["names"]


# ---- the same machine twice ----------------------------------------------
def test_the_same_address_twice_is_offered_once():
    """Two names for one box is normal in a hosts file and would be two host
    records for one machine — the duplicate rows the IP check exists to stop."""
    rows = hosts_file.parse("10.0.0.5 db\n10.0.0.5 db-old\n")
    assert rows[0]["enrollable"] is True
    assert rows[1]["enrollable"] is False
    assert rows[1]["skip"] == "duplicate"
    assert "line 1" in rows[1]["skip_reason"], rows[1]["skip_reason"]


def test_the_same_address_written_differently_is_still_the_same_address():
    rows = hosts_file.parse("10.0.0.5 db\n::ffff:0a00:0005 db6\n10.0.0.005 db3\n")
    enrollable = [r for r in rows if r["enrollable"]]
    assert len(enrollable) == 1, [r["address"] for r in enrollable]


# ---- what to call them ----------------------------------------------------
def test_a_short_name_is_preferred_over_the_fqdn():
    """db is a better label than db.internal, and the file gives both."""
    rows = hosts_file.parse("10.0.0.5 db.internal db\n")
    assert rows[0]["name"] == "db"


def test_a_short_name_two_machines_share_is_not_used_for_either():
    """web-01.a and web-01.b would both arrive as "web-01" — a collision the
    operator has to notice and undo by hand. Whether the short form is safe for
    line 1 depends on line 2, so it is counted over the whole file first."""
    rows = hosts_file.parse("10.0.0.6 web-01.a.example.com\n10.0.0.7 web-01.b.example.com\n")
    assert _names(rows) == ["web-01.a.example.com", "web-01.b.example.com"], _names(rows)


def test_a_line_with_no_hostname_is_offered_under_its_own_address():
    """Changed deliberately, with "IP should be optional if using /etc/hosts".

    An address on its own used to be skipped for having no name. But there is
    nothing wrong with the line -- it names a machine the only way it can, and
    refusing it meant an operator pasting a column of addresses got nothing at
    all. It is offered labelled by its address, and the label is editable
    before anything is posted.
    """
    rows = hosts_file.parse("10.0.0.9\n")
    assert rows[0]["enrollable"] is True
    assert rows[0]["name"] == "10.0.0.9"
    assert rows[0]["target"] == "10.0.0.9"


def test_something_that_is_not_an_address_is_not_a_candidate():
    rows = hosts_file.parse("not-an-ip bogus\n")
    assert rows[0]["enrollable"] is False and rows[0]["skip"] == "bad-address"


# ---- the half that cannot be allowed to drift -----------------------------
def test_every_name_offered_is_one_the_enrollment_route_accepts():
    """The browser posts these straight back to /enroll-ssh, whose model
    validates the name. Two independent ideas of "a usable name" would mean a
    screen that proposes a host and an API that then refuses it, with nothing
    to say which of the two was wrong."""
    sample = DEBIAN + "\n".join([
        "10.1.0.1 my_host",
        "10.1.0.2 UPPER-CASE.Example.COM",
        "10.1.0.3 host-with-many-parts.a.b.c.d",
        "10.1.0.4 -starts-with-a-hyphen",
        "10.1.0.5 x" + "y" * 80,
    ]) + "\n"
    rows = hosts_file.parse(sample)
    offered = [r for r in rows if r["enrollable"]]
    assert offered, "nothing was offered at all; this test is checking nothing"
    for r in offered:
        assert HOST_NAME_RE.match(r["name"]), \
            f"line {r['line']} offers {r['name']!r}, which /enroll-ssh refuses"
        assert HOST_ADDR_RE.match(r["address"]), \
            f"line {r['line']} offers address {r['address']!r}, which /enroll-ssh refuses"


def test_a_name_the_route_would_refuse_is_skipped_not_mangled():
    rows = hosts_file.parse("10.0.0.9 -badstart\n")
    assert rows[0]["enrollable"] is False
    assert rows[0]["skip"] == "bad-name"


def test_a_huge_file_is_bounded():
    """A hosts file is not a megabyte. Parsing an unbounded one on a request
    thread is a way to tie up the Controller."""
    rows = hosts_file.parse("\n".join(f"10.{i // 256}.{i % 256}.1 h{i}"
                                      for i in range(5000)), max_lines=100)
    assert len(rows) == 100


# ---- the route reads, and only reads --------------------------------------
def test_there_is_no_bulk_enrollment_endpoint():
    """The whole safety argument. Each candidate goes back through the single
    /enroll-ssh route, so the duplicate-IP check, the host limit and the
    superuser gate stay in the one place they already live."""
    src = open("backend/remote_routes.py", encoding="utf-8").read()
    assert "/hosts-file/preview" in src
    for bad in ("/enroll-ssh-bulk", "/hosts-file/enroll", "/enroll-many"):
        assert bad not in src, f"a second enrollment path appeared: {bad}"


def test_the_preview_route_is_superuser_gated():
    import re
    src = open("backend/remote_routes.py", encoding="utf-8").read()
    m = re.search(r'@router\.post\("/hosts-file/preview"[^)]*\)', src)
    assert m, "the preview route is gone"
    assert "require_superuser" in m.group(0), \
        "reading a hosts file is not gated to a superuser"


def test_the_browser_posts_candidates_through_the_ordinary_route():
    js = open("webgui/frontend/src/views/HostEnrollment.jsx", encoding="utf-8").read()
    assert "api.enrollSsh(" in js, "the bulk path does not use the single-host route"
    assert "hostsFilePreview" in js
    # One password for the batch, cleared afterwards.
    assert "setBulkCreds((c) => ({ ...c, password: \"\" }))" in js, \
        "the batch password is left sitting in the form after the run"
