"""A name the rest of the machine resolves is not a name the Controller does.

Reported as, after the address was made optional: "Still not working properly",
with the console showing

    Could not resolve 'ubuntu-lab'. Give an address, or add the name to this
    host's /etc/hosts or DNS.

while `192.168.155.135  ubuntu-lab` was sitting in that host's /etc/hosts the
whole time. The error was not wrong -- it was ADVICE THAT COULD NOT BE TAKEN.
The Controller runs in a container. A container gets its own /etc/hosts, so the
name the platform host resolves perfectly well had never been visible to the
process being asked to resolve it, and the one instruction the message gave was
the one thing the operator had already done.

Two fixes, because one of them is not always available:

  * the compose file bind-mounts the host's /etc/hosts read-only, so the names
    that machine knows are the names the console offers, with nothing to set up

  * the console has a name list of its own, in the persisted volume, for the
    deployments where there is no such file to mount -- a Controller on a
    different machine from its fleet, or an operator who keeps the list
    somewhere else

What is NOT tested here is a real DNS lookup: these tests must pass on a
machine with no network and no /etc/hosts entries of their own.

Run: pytest tests/
"""
import os
from pathlib import Path

import pytest
import yaml

from backend import hosts_file, name_table

REPO = Path(__file__).resolve().parents[1]
VIEW = (REPO / "webgui" / "frontend" / "src" / "views" / "HostEnrollment.jsx").read_text(
    encoding="utf-8")

HOSTS = """\
# the file an operator actually has
127.0.0.1       localhost
127.0.1.1       slop-host
192.168.155.135 ubuntu-lab
10.0.0.5        db.internal db
fe80::1         link-local-box
224.0.0.1       all-systems
0.0.0.0         nowhere
"""


@pytest.fixture()
def tables(tmp_path, monkeypatch):
    """Point both sources at files this test owns, and clear the parse cache."""
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "host-hosts"))
    name_table._CACHE.clear()
    yield tmp_path
    name_table._CACHE.clear()


# ---- what counts as a name worth keeping -----------------------------------
def test_the_names_an_operator_wrote_down_are_read_back():
    t = name_table.build(HOSTS)
    assert t["ubuntu-lab"] == ["192.168.155.135"]
    assert t["db.internal"] == ["10.0.0.5"]
    assert t["db"] == ["10.0.0.5"], "an alias is a name too"


def test_a_name_is_matched_whatever_case_it_is_typed_in():
    t = name_table.build("10.0.0.9  Web-01\n")
    assert "web-01" in t, "hostnames are case-insensitive; the table must be too"


@pytest.mark.parametrize("line,name", [
    ("127.0.0.1  localhost", "localhost"),
    ("127.0.1.1  slop-host", "slop-host"),
    ("fe80::1    link-local-box", "link-local-box"),
    ("224.0.0.1  all-systems", "all-systems"),
    ("0.0.0.0    nowhere", "nowhere"),
])
def test_an_address_that_cannot_name_a_machine_to_manage_is_dropped(line, name):
    """127.0.1.1 <hostname> is the Debian convention for the machine's OWN name.

    Keeping it would mean that typing your own hostname points enrollment back
    at the Controller -- and with sshd listening, that is a connection that
    could SUCCEED, enrolling the platform host as one of its own managed hosts.
    """
    assert name_table.build(line + "\n") == {}


def test_every_dropped_kind_is_actually_dropped_from_a_real_file():
    assert set(name_table.build(HOSTS)) == {"ubuntu-lab", "db.internal", "db"}


def test_a_name_that_could_not_be_posted_back_is_not_offered():
    """The table feeds the enroll route, which has its own idea of a usable
    name. Recording one it would refuse means proposing a host and then
    rejecting it, with nothing to say which half was wrong."""
    assert name_table.build("10.0.0.9  -oProxyCommand=touch\n") == {}


def test_a_comment_is_not_a_machine():
    assert name_table.build("# 10.0.0.9  ghost\n10.0.0.9 real # trailing\n") == \
        {"real": ["10.0.0.9"]}


# ---- which source wins -----------------------------------------------------
def test_the_hosts_file_mounted_from_the_platform_host_is_read(tables):
    (tables / "host-hosts").write_text(HOSTS)
    assert name_table.lookup("ubuntu-lab") == ["192.168.155.135"]


def test_the_controllers_own_list_is_read(tables):
    (tables / "own").write_text("10.1.1.1  ubuntu-lab\n")
    assert name_table.lookup("ubuntu-lab") == ["10.1.1.1"]


def test_what_the_operator_typed_here_beats_the_mounted_copy(tables):
    """A bind-mounted single file does not follow a rename, so the mounted copy
    can be stale while the console's is current. The one an operator typed into
    this console is the one they can see and correct."""
    (tables / "host-hosts").write_text(HOSTS)
    (tables / "own").write_text("10.9.9.9  ubuntu-lab\n")
    assert name_table.lookup("ubuntu-lab") == ["10.9.9.9"]


def test_a_name_in_neither_is_simply_not_known(tables):
    (tables / "host-hosts").write_text(HOSTS)
    assert name_table.lookup("not-a-host") == []


def test_nothing_breaks_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "a"))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "b"))
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", str(tmp_path / "c"))
    name_table._CACHE.clear()
    assert name_table.lookup("ubuntu-lab") == []
    assert not any(s["present"] for s in name_table.sources())
    assert name_table.entries() == []


def test_a_directory_where_a_file_was_expected_is_not_a_crash(tables):
    (tables / "own").mkdir()
    assert name_table.lookup("ubuntu-lab") == []


# ---- the cache must not outlive the file -----------------------------------
def test_editing_the_list_takes_effect_without_a_restart(tables):
    own = tables / "own"
    own.write_text("10.0.0.1  box\n")
    assert name_table.lookup("box") == ["10.0.0.1"]
    # Rewritten in place, as the console's save does.
    os.utime(own, (0, 0))
    own.write_text("10.0.0.2  box\n")
    assert name_table.lookup("box") == ["10.0.0.2"], \
        "a cached parse outlived the file it came from"


def test_a_file_replaced_by_a_rename_is_noticed(tables):
    """`sed -i`, and most editors, write a new file and rename it over the old
    one. Same path, same size, possibly the same mtime -- different inode."""
    own = tables / "own"
    own.write_text("10.0.0.1  box\n")
    assert name_table.lookup("box") == ["10.0.0.1"]
    new = tables / "staged"
    new.write_text("10.0.0.3  box\n")      # same length as the line it replaces
    st = os.stat(own)
    # NANOSECOND precision on purpose: os.utime with float seconds rounds, and
    # the rounding alone would change the stamp -- which would leave this test
    # passing while testing nothing.
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(new, own)
    assert os.stat(own).st_mtime_ns == st.st_mtime_ns and \
        os.stat(own).st_size == st.st_size, \
        "this test only means something if mtime and size are unchanged"
    assert name_table.lookup("box") == ["10.0.0.3"]


# ---- saving ----------------------------------------------------------------
def test_saving_the_list_makes_the_name_resolvable(tables):
    assert name_table.lookup("ubuntu-lab") == []
    assert name_table.save(HOSTS) == 3, "the count is what the console reports"
    assert name_table.lookup("ubuntu-lab") == ["192.168.155.135"]


def test_saving_keeps_the_text_as_written_not_as_parsed(tables):
    """The operator gets their own file back to edit, comments and all."""
    name_table.save(HOSTS)
    assert name_table.read_own() == HOSTS


def test_saving_leaves_no_half_written_file_behind(tables):
    name_table.save(HOSTS)
    assert not (tables / "own.tmp").exists()


def test_the_list_is_kept_where_an_update_cannot_take_it(monkeypatch):
    """/data is the volume; anything else is lost on the next image."""
    monkeypatch.delenv("SYSIBLE_NAME_TABLE", raising=False)
    monkeypatch.setenv("SYSIBLE_HOME", "/data")
    assert name_table.own_table_path().startswith("/data/")


# ---- the resolver actually uses it -----------------------------------------
def test_enrollment_resolves_a_name_only_this_list_knows(tables, monkeypatch):
    import backend.remote_routes as rr

    def no_dns(*a, **k):
        raise OSError("this test has no network, and does not need one")
    monkeypatch.setattr(rr.socket, "getaddrinfo", no_dns)

    assert rr._resolve_addrs("ubuntu-lab") == set()
    (tables / "host-hosts").write_text(HOSTS)
    assert rr._resolve_addrs("ubuntu-lab") == {"192.168.155.135"}


def test_the_written_down_answer_beats_dns(tables, monkeypatch):
    """The same precedence /etc/hosts has over DNS on any machine, for the same
    reason: what the operator wrote down beats what the network says."""
    import backend.remote_routes as rr
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("203.0.113.9", 0))])
    (tables / "own").write_text("192.168.155.135  ubuntu-lab\n")
    assert rr._resolve_addrs("ubuntu-lab") == {"192.168.155.135"}


def test_an_address_still_never_triggers_a_lookup(tables, monkeypatch):
    import backend.remote_routes as rr

    def boom(*a, **k):
        raise AssertionError("an address was sent off to be resolved")
    monkeypatch.setattr(rr.socket, "getaddrinfo", boom)
    (tables / "own").write_text("10.0.0.1  192.168.155.135\n")   # a trap
    assert rr._resolve_addrs("192.168.155.135") == {"192.168.155.135"}


def test_a_name_this_list_knows_is_caught_as_a_duplicate(tables, monkeypatch):
    """The whole point of resolving: `ubuntu-lab` and `192.168.155.135` are one
    machine, and enrolling it twice under two labels is what the check exists
    to stop."""
    import backend.remote_routes as rr
    monkeypatch.setattr(rr, "load_hosts",
                        lambda: {"lab": {"ip": "192.168.155.135", "addr": "192.168.155.135"}})
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    (tables / "own").write_text(HOSTS)
    assert rr._ip_owner("ubuntu-lab") == "lab"


# ---- the advice the operator is given --------------------------------------
def _unresolvable_message(tmp_path, monkeypatch):
    """The message an operator really gets, by making the real call fail."""
    import backend.remote_routes as rr
    from backend.models.remote_models import EnrollSSHRequest
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "nothing"))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "nothing-either"))
    name_table._CACHE.clear()
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    with pytest.raises(Exception) as caught:
        rr.enroll_ssh(EnrollSSHRequest(name="ubuntu-lab", password="x"))
    exc = caught.value
    assert getattr(exc, "status_code", None) == 400, \
        f"an unresolvable name should be a plain 400, got {exc!r}"
    return exc.detail


def test_the_failure_no_longer_gives_advice_that_cannot_be_taken(tmp_path, monkeypatch):
    msg = _unresolvable_message(tmp_path, monkeypatch)
    assert "ubuntu-lab" in msg, "the message does not say which name failed"
    assert "add the name to this host's /etc/hosts" not in msg, \
        "still telling the operator to do the thing they already did"
    assert "container" in msg, "nothing explains why the name is unknown here"
    assert "Hosts list" in msg, "the message does not say where the fix is"


def test_the_console_offers_the_list_the_message_points_at():
    """The panel the failure names has to be the panel that exists. It was
    renamed to "Hosts list" and the message still pointed at the old one."""
    import re
    legends = set(re.findall(r"<legend>([^<]+)</legend>", VIEW))
    assert "Hosts list" in legends, f"no such panel; legends are {sorted(legends)}"
    assert "api.addName" in VIEW and "api.saveNames" in VIEW


def test_the_console_says_when_the_host_file_was_never_mounted():
    """"It cannot resolve the name" and "the mount is missing" are the same
    failure from the console and have different fixes."""
    assert "/etc/hosts:/host/etc/hosts:ro" in VIEW, \
        "the console does not tell the operator what to add"


# ---- the mount is a contract, not a suggestion -----------------------------
@pytest.mark.parametrize("compose", [
    REPO / "docker-compose.yml",
])
def test_the_compose_file_mounts_the_hosts_file_read_only(compose):
    doc = yaml.safe_load(compose.read_text(encoding="utf-8"))
    vols = doc["services"]["controller"]["volumes"]
    assert "/etc/hosts:/host/etc/hosts:ro" in vols, \
        (f"{compose.name} no longer mounts the host's /etc/hosts; enrolling by "
         f"name silently stops working")


def test_the_mount_is_read_only_everywhere_it_appears():
    """Read-only is the whole claim the console makes about it."""
    for compose in REPO.rglob("docker-compose*.yml"):
        for line in compose.read_text(encoding="utf-8").splitlines():
            if "/host/etc/hosts" in line and not line.strip().startswith("#"):
                assert line.rstrip().endswith(":ro"), \
                    f"{compose}: {line.strip()} is not read-only"


def test_the_path_the_code_reads_is_the_path_the_compose_file_mounts(monkeypatch):
    monkeypatch.delenv("SYSIBLE_HOST_HOSTS_FILE", raising=False)
    assert name_table.host_hosts_path() == "/host/etc/hosts"


# ---- the console route, end to end ------------------------------------------
def _bff():
    import tempfile
    os.environ.setdefault("SYSIBLE_API_KEY", "test-names-key")
    os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-names-"))
    from fastapi.testclient import TestClient
    import webgui.server as w
    return w, TestClient(w.app)


HDRS = {"origin": "http://testserver", "host": "testserver"}


def test_the_name_list_is_not_readable_without_signing_in():
    """It is a map of the fleet. An unauthenticated reader must not get it."""
    w, client = _bff()
    assert client.get("/api/names", headers=HDRS).status_code in (401, 403)
    assert client.put("/api/names", json={"text": "10.0.0.1 x"},
                      headers=HDRS).status_code in (401, 403)


@pytest.mark.parametrize("method,path", [("GET", "/api/names"), ("PUT", "/api/names")])
def test_the_name_routes_declare_the_login_check_themselves(method, path):
    """Checked SEPARATELY from the refusal above, because that refusal has two
    causes: the login check, and the proxy helper having no superuser token to
    forward. Either one alone returns 403, so deleting the login check leaves
    the status quo looking identical -- until something gives the helper a
    token and the route is open with nothing to notice."""
    w, _ = _bff()
    route = next(r for r in w.app.routes
                 if getattr(r, "path", None) == path and method in getattr(r, "methods", ()))
    names = [d.call.__name__ for d in route.dependant.dependencies]
    assert "require_operator" in names, \
        f"{method} {path} no longer checks that anyone is signed in: {names}"


def test_saving_the_list_goes_to_the_superuser_gated_route():
    w, client = _bff()
    calls = []

    def fake(method, path, request, **kw):
        calls.append((method, path, kw.get("json")))
        return {"names": 1, "sources": []}

    orig = w._superuser_request
    w.app.dependency_overrides[w.require_operator] = lambda: "root"
    w._superuser_request = fake
    try:
        r = client.put("/api/names", json={"text": "10.0.0.1  box"}, headers=HDRS)
        assert r.status_code == 200, r.text
        assert calls == [("PUT", "/remote/names", {"text": "10.0.0.1  box"})], calls
    finally:
        w._superuser_request = orig
        w.app.dependency_overrides.clear()


def test_the_controller_route_stores_what_the_console_sends(tables):
    import backend.remote_routes as rr
    out = rr.names_put({"text": HOSTS})
    assert out["names"] == 3
    assert name_table.lookup("ubuntu-lab") == ["192.168.155.135"]
    assert rr.names_get()["text"] == HOSTS


def test_the_controller_route_refuses_something_that_is_not_a_list(tables):
    import backend.remote_routes as rr
    for bad in ({}, {"text": None}, {"text": 7}, {"text": ["10.0.0.1 x"]}):
        with pytest.raises(Exception) as caught:
            rr.names_put(bad)
        assert getattr(caught.value, "status_code", None) == 400


def test_a_file_far_too_big_to_be_a_hosts_file_is_refused(tables):
    import backend.remote_routes as rr
    with pytest.raises(Exception) as caught:
        rr.names_put({"text": "x" * (name_table.MAX_BYTES + 1)})
    assert getattr(caught.value, "status_code", None) == 413


def test_a_volume_that_cannot_be_written_says_so(tables, monkeypatch):
    """"500" sends an operator to the logs. Naming the path and the variable
    sends them to the fix."""
    import backend.remote_routes as rr
    monkeypatch.setattr(name_table, "save",
                        lambda t: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    with pytest.raises(Exception) as caught:
        rr.names_put({"text": "10.0.0.1  box"})
    assert getattr(caught.value, "status_code", None) == 500
    assert "SYSIBLE_NAME_TABLE" in caught.value.detail
    assert "Permission denied" in caught.value.detail


def test_the_console_is_told_which_sources_exist(tables):
    import backend.remote_routes as rr
    (tables / "host-hosts").write_text(HOSTS)
    srcs = {s["key"]: s for s in rr.names_get()["sources"]}
    assert srcs["host"]["present"] and srcs["host"]["names"] == 3
    assert not srcs["own"]["present"]


# ---- saying WHY, not just no ------------------------------------------------
def test_the_failure_says_the_hosts_file_was_never_mounted(tmp_path, monkeypatch):
    """The first thing to check, and the console could not say it."""
    msg = _unresolvable_message(tmp_path, monkeypatch)
    assert "no host list at all" in msg
    assert "/nothing-either" in msg, "the path it looked at is not named"


def test_the_failure_distinguishes_a_mounted_file_that_lacks_the_name(
        tmp_path, monkeypatch):
    """A different cause and a different fix: the mount is fine, the entry is
    missing. Reported identically before this."""
    import backend.remote_routes as rr
    from backend.models.remote_models import EnrollSSHRequest
    (tmp_path / "host").write_text("10.0.0.5  db\n10.0.0.6  web\n")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "host"))
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    name_table._CACHE.clear()
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    with pytest.raises(Exception) as caught:
        rr.enroll_ssh(EnrollSSHRequest(name="ubuntu-lab", password="x"))
    msg = caught.value.detail
    assert "is mounted and lists 2 names" in msg, msg
    assert "neither names 'ubuntu-lab'" in msg, msg


def test_the_count_reads_as_english_for_one(tables):
    (tables / "host-hosts").write_text("10.0.0.5  db\n")
    assert "lists 1 name," in name_table.diagnose("x")
    assert "1 names" not in name_table.diagnose("x")


# ---- checking a name without trying to enroll it ----------------------------
def test_checking_a_name_says_which_source_answered(tables):
    import backend.remote_routes as rr
    (tables / "host-hosts").write_text(HOSTS)
    out = rr.names_check(name="ubuntu-lab")
    assert out["addresses"] == ["192.168.155.135"]
    assert out["via"] == "host"
    assert "/etc/hosts" in out["via_label"]


def test_checking_a_name_the_console_list_answers_for(tables):
    import backend.remote_routes as rr
    (tables / "host-hosts").write_text(HOSTS)
    (tables / "own").write_text("10.9.9.9  ubuntu-lab\n")
    out = rr.names_check(name="ubuntu-lab")
    assert out["addresses"] == ["10.9.9.9"] and out["via"] == "own"


def test_checking_an_address_does_not_pretend_to_have_resolved_it(tables, monkeypatch):
    import backend.remote_routes as rr
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("an address was sent off to be resolved")))
    out = rr.names_check(name="192.168.155.135")
    assert out["addresses"] == ["192.168.155.135"] and out["via"] == "literal"
    # The LABEL is what the operator reads, so it has to be honest too: a
    # screen that says an address came "from DNS" is evidence of a lookup that
    # never happened, and the next person debugging believes it.
    assert "DNS" not in out["via_label"], out["via_label"]
    assert "address" in out["via_label"]


def test_checking_a_name_only_dns_knows_says_so(tables, monkeypatch):
    import backend.remote_routes as rr
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("203.0.113.9", 0))])
    out = rr.names_check(name="elsewhere")
    assert out["addresses"] == ["203.0.113.9"] and out["via"] == "dns"


def test_a_name_nothing_knows_comes_back_with_the_reason(tables, monkeypatch):
    import backend.remote_routes as rr
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    out = rr.names_check(name="ubuntu-lab")
    assert out["addresses"] == [] and out["via"] is None
    assert "no host list at all" in out["reason"], out["reason"]


def test_checking_nothing_is_refused(tables):
    import backend.remote_routes as rr
    with pytest.raises(Exception) as caught:
        rr.names_check(name="   ")
    assert getattr(caught.value, "status_code", None) == 400


def test_the_check_opens_no_connection_to_the_host(tables, monkeypatch):
    """It answers a question about names. Dialling the host is enrollment's
    job, and doing it here would make a diagnostic tool change something."""
    import backend.remote_routes as rr
    (tables / "host-hosts").write_text(HOSTS)
    monkeypatch.setattr(rr.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("the check shelled out to ssh")))
    assert rr.names_check(name="ubuntu-lab")["addresses"] == ["192.168.155.135"]


def test_the_check_route_declares_the_login_check_too():
    w, _ = _bff()
    route = next(r for r in w.app.routes
                 if getattr(r, "path", None) == "/api/names/check"
                 and "GET" in getattr(r, "methods", ()))
    assert "require_operator" in [d.call.__name__ for d in route.dependant.dependencies]


# ---- the example in the box has to BE an example ---------------------------
def test_the_bulk_placeholder_is_a_file_the_parser_would_accept():
    """It was prose in the middle of a hosts file: "…or, with addresses:",
    uncommented. Anyone who copied the example got a line the parser rejects,
    and the box teaches the shape it expects."""
    import re
    m = re.search(r'placeholder=\{"((?:[^"\\]|\\.)*)"\}', VIEW[VIEW.index("Read the list") - 3000:])
    assert m, "the bulk placeholder is no longer where this test looks"
    text = m.group(1).encode().decode("unicode_escape")
    rows = hosts_file.parse(text)
    bad = [r for r in rows if not r["enrollable"]]
    assert not bad, f"the example contains lines the parser rejects: {bad}"
    assert rows, "the example parses to nothing at all"
    assert text.lstrip().startswith("#"), "the explaining line is not a comment"


def test_the_check_result_uses_classes_the_stylesheet_actually_defines():
    """It was written with className="ok" / "warn". Neither is a text class in
    this stylesheet -- .ok exists only as .dot.ok, .warn only under .ug-tile --
    so the result rendered in body text and the one case that most needs to
    stand out, a name that resolves to nothing, looked like prose."""
    css = (REPO / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    import re
    used = set(re.findall(r'checkResult\.addresses\.length \? "([\w-]+)" : "([\w-]+)"', VIEW))
    assert used, "the check result no longer picks a class this way"
    for pair in used:
        for cls in pair:
            assert re.search(r'(^|[\s,}])\.' + re.escape(cls) + r'\s*\{', css, re.M), \
                (f'the check result renders with class "{cls}", which this '
                 f'stylesheet does not define on its own')


# ---- the hosts list: what it reads, and what it can be told ----------------
@pytest.fixture()
def all_three(tmp_path, monkeypatch):
    """All three sources under this test's control."""
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "mounted"))
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", str(tmp_path / "local"))
    name_table._CACHE.clear()
    yield tmp_path
    name_table._CACHE.clear()


def test_the_controller_reads_its_own_etc_hosts_with_nothing_mounted(all_three):
    """The source that is always there. Natively it IS the platform host's
    file, so a Controller on the metal needs none of the rest of this; in a
    container it carries whatever extra_hosts: put there."""
    (all_three / "local").write_text("192.168.155.135  ubuntu-lab\n")
    assert name_table.lookup("ubuntu-lab") == ["192.168.155.135"]


def test_the_default_local_path_is_the_real_one(monkeypatch):
    monkeypatch.delenv("SYSIBLE_LOCAL_HOSTS_FILE", raising=False)
    assert name_table.local_hosts_path() == "/etc/hosts"


def test_the_sources_are_tried_in_one_declared_order(all_three):
    """own, then the mounted copy, then the container's own file."""
    for f, addr in (("local", "10.0.0.3"), ("mounted", "10.0.0.2"), ("own", "10.0.0.1")):
        (all_three / f).write_text(f"{addr}  box\n")
        name_table._CACHE.clear()
    assert name_table.where("box") == (["10.0.0.1"], "own")
    (all_three / "own").unlink(); name_table._CACHE.clear()
    assert name_table.where("box") == (["10.0.0.2"], "host")
    (all_three / "mounted").unlink(); name_table._CACHE.clear()
    assert name_table.where("box") == (["10.0.0.3"], "local")


def test_every_source_appears_in_the_list_the_console_shows(all_three):
    """lookup, where, sources and the list each used to walk their own copy of
    the source order. A source added to one and not the others is a name the
    console offers and enrollment then cannot resolve."""
    listed = {s["key"] for s in name_table.sources()}
    assert listed == {k for k, _p, _l in name_table._source_specs()}


def test_the_list_shows_one_row_per_name_with_where_it_came_from(all_three):
    (all_three / "local").write_text("172.18.0.4  sysible-controller\n")
    (all_three / "mounted").write_text("10.0.0.5  db\n")
    name_table.add_entry("ubuntu-lab", "192.168.155.135")
    rows = {r["name"]: r for r in name_table.entries()}
    assert rows["ubuntu-lab"]["source"] == "own" and rows["ubuntu-lab"]["editable"]
    assert rows["db"]["source"] == "host" and not rows["db"]["editable"]
    assert rows["sysible-controller"]["source"] == "local"


def test_a_name_in_two_files_is_one_row_that_says_what_it_covers(all_three):
    """Showing both would show a choice the resolver does not offer -- and an
    operator whose edit appears to do nothing needs to see what covers it."""
    (all_three / "mounted").write_text("10.0.0.9  box\n")
    name_table.add_entry("box", "10.0.0.1")
    rows = [r for r in name_table.entries() if r["name"] == "box"]
    assert len(rows) == 1 and rows[0]["address"] == "10.0.0.1"
    assert rows[0]["shadows"] == ["The platform host's /etc/hosts"]


def test_adding_a_name_twice_replaces_it(all_three):
    """Two lines for one name is a file whose meaning depends on which the
    parser reaches first."""
    name_table.add_entry("box", "10.0.0.1")
    name_table.add_entry("box", "10.0.0.2")
    assert name_table.lookup("box") == ["10.0.0.2"]
    assert len([r for r in name_table.entries() if r["name"] == "box"]) == 1


def test_adding_a_name_keeps_the_others_on_its_line(all_three):
    name_table.save("10.0.0.5  db.internal db\n")
    name_table.add_entry("db", "10.9.9.9")
    assert name_table.lookup("db") == ["10.9.9.9"]
    assert name_table.lookup("db.internal") == ["10.0.0.5"], \
        "an alias on the same line was collateral damage"


@pytest.mark.parametrize("name,addr,why", [
    ("box", "127.0.0.1", "loopback"),
    ("box", "not-an-ip", "not an IP"),
    ("box", "", "no address"),
    ("-oProxyCommand=x", "10.0.0.1", "unusable name"),
    ("box", "224.0.0.1", "multicast"),
])
def test_the_list_refuses_what_enrollment_would_refuse(all_three, name, addr, why):
    with pytest.raises(ValueError):
        name_table.add_entry(name, addr)


def test_removing_a_name_takes_it_out(all_three):
    name_table.add_entry("box", "10.0.0.1")
    name_table.remove_entry("box")
    assert name_table.lookup("box") == []


def test_removing_one_name_from_a_shared_line_keeps_the_rest(all_three):
    name_table.save("10.0.0.5  db.internal db\n")
    name_table.remove_entry("db")
    assert name_table.lookup("db") == [] and name_table.lookup("db.internal") == ["10.0.0.5"]


def test_removing_a_name_that_is_not_ours_is_refused_not_pretended(all_three):
    """Rewriting our file and reporting success for a name from somewhere else
    tells an operator it was removed while it still resolves."""
    (all_three / "mounted").write_text("10.0.0.9  theirs\n")
    with pytest.raises(ValueError) as caught:
        name_table.remove_entry("theirs")
    assert "/etc/hosts" in str(caught.value)
    assert name_table.lookup("theirs") == ["10.0.0.9"], "it was removed anyway"


def test_a_name_added_to_the_list_can_be_enrolled_at_once(all_three, monkeypatch):
    """The property the whole panel exists for: no mount, no restart."""
    import backend.remote_routes as rr
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    assert rr._resolve_addrs("ubuntu-lab") == set()
    rr.names_add({"name": "ubuntu-lab", "address": "192.168.155.135"})
    assert rr._resolve_addrs("ubuntu-lab") == {"192.168.155.135"}


def test_the_entry_routes_report_the_new_list(all_three):
    import backend.remote_routes as rr
    out = rr.names_add({"name": "box", "address": "10.0.0.1"})
    assert [e["name"] for e in out["entries"]] == ["box"]
    out = rr.names_remove(name="box")
    assert out["entries"] == []


def test_the_entry_routes_turn_a_refusal_into_a_400(all_three):
    import backend.remote_routes as rr
    for call in (lambda: rr.names_add({"name": "box", "address": "127.0.0.1"}),
                 lambda: rr.names_remove(name="never-added")):
        with pytest.raises(Exception) as caught:
            call()
        assert getattr(caught.value, "status_code", None) == 400, caught.value


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/names/entry"), ("DELETE", "/api/names/entry")])
def test_the_entry_routes_check_that_anyone_is_signed_in(method, path):
    w, _ = _bff()
    route = next(r for r in w.app.routes
                 if getattr(r, "path", None) == path and method in getattr(r, "methods", ()))
    assert "require_operator" in [d.call.__name__ for d in route.dependant.dependencies]


# ---- where it sits on the page ---------------------------------------------
def test_the_hosts_list_is_beside_the_forms_not_below_them():
    css = (REPO / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "enroll-aside" in VIEW, "the hosts list is no longer in its own column"
    assert "<aside" in VIEW
    import re
    m = re.search(r"\.enroll-split\s*\{([^}]*)\}", css)
    assert m, "the two-column layout is gone from the stylesheet"
    cols = re.search(r"grid-template-columns:([^;]*);", m.group(1))
    assert cols and cols.group(1).count("minmax") == 2, \
        f"enroll-split is not two columns: {m.group(1)}"
    # ...and ONE column when there is not room for the list to be readable.
    assert re.search(r"@media[^{]*max-width:\s*11\d\dpx[^{]*\{[^}]*\.enroll-split", css, re.S), \
        "nothing collapses the split on a narrow window"


def test_an_unexpected_names_response_does_not_blank_the_console():
    """`setState(fn)` is an UPDATER to React, and it calls it.

    The hosts list arrives as `d.entries`. Every array has an `.entries` --
    Array.prototype.entries, a function -- so a response that is an array
    rather than the expected object passed a function to the setter, React
    called it with the previous state, and the whole Host Enrollment page went
    blank with "Cannot convert undefined or null to object". A truthiness
    check does not catch it because a function is truthy; only a type check
    does."""
    checked = 0
    for line in VIEW.splitlines():
        for field, setter in (("entries", "setNamesEntries"),
                              ("sources", "setNamesSources")):
            if setter + "(" not in line:
                continue
            if not any(f"d{sep}{field}" in line for sep in (".", "?.")):
                continue        # a literal, not the server's answer
            checked += 1
            assert "Array.isArray" in line, (
                f"a server-supplied {field} reaches {setter} with no type "
                f"check, which blanks the page: {line.strip()!r}")
    assert checked >= 3, (
        f"only {checked} such calls found; this test has stopped looking at "
        f"the code it is about")
