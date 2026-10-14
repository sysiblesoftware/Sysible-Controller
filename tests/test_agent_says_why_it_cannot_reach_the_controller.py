"""An unreachable controller must say WHY, once.

Reported from a host whose journal read, over and over:

    [agent] could not fetch tasks: HTTPSC>
    [agent] heartbeat failed: HTTPSConnec>
    [agent] config backup thread: HTTPSCo>

Three problems in those three lines:

  * the message is the raw requests exception — a wall of urllib3 text whose
    only useful part (the errno, or "certificate verify failed") is at the very
    END, so journalctl truncates exactly the part that identifies the fault;
  * three completely different faults look identical. requests models SSLError
    as a subclass of ConnectionError, so a STALE PINNED CERT — which is what you
    get the moment a controller is renumbered and re-issues its cert — reads the
    same as "the host is down";
  * all three loops report the same outage every few seconds, so the journal is
    three identical lines a minute for as long as it lasts, burying anything
    else the agent has to say.

These drive the real classifier with the real exception objects requests raises.
"""
import socket
import threading

import pytest
import requests

from host_agent import agent


def _raise_against(url, **kw):
    """Make requests actually fail, and hand back the exception it raised."""
    sess = requests.Session()
    sess.trust_env = False      # no ambient proxy may answer for a dead name
    try:
        sess.get(url, timeout=kw.pop("timeout", 3), **kw)
    except requests.RequestException as e:
        return e
    pytest.fail(f"{url} unexpectedly succeeded")


@pytest.fixture()
def quiet_reporter(monkeypatch):
    """A fresh report state per test, and the printed lines captured."""
    lines = []
    monkeypatch.setattr(agent, "_conn_report", {"why": None, "at": 0.0, "suppressed": 0})
    monkeypatch.setattr(agent, "_conn_report_lock", threading.Lock())
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(str(x) for x in a)))
    return lines


# ---- the reason is identified, from the real exception ---------------------
def test_nothing_listening_is_not_reported_as_anything_else():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    why = agent._why_unreachable(_raise_against(f"https://127.0.0.1:{port}/x"))
    assert "nothing is listening" in why
    assert "different port" in why


def test_a_name_that_does_not_resolve_says_so():
    why = agent._why_unreachable(_raise_against("https://sysible-no-such-host.invalid:9000/x"))
    assert "does not resolve" in why


def test_a_timeout_does_not_pick_one_cause_over_the_other():
    """A silent drop has two very different causes — something in the path
    dropping, or the controller host not answering at all — and the text must
    not send the operator to one of them. It said "usually a firewall" once, on
    an outage whose real answer was that the VM needed a reboot."""
    lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(1)
    try:
        why = agent._why_unreachable(
            _raise_against(f"https://127.0.0.1:{lsock.getsockname()[1]}/x", timeout=2))
    finally:
        lsock.close()
    assert "timed out" in why
    assert "dropping" in why, "it does not offer the in-path cause"
    assert "not answering at all" in why, "it does not offer the far-end cause"
    assert "refuse the connection, not" in why, \
        "it does not say what distinguishes this from a closed port"


def test_a_stale_pinned_cert_is_distinguished_from_an_unreachable_host(tls_server):
    """The one this exists for. requests raises SSLError, which IS a
    ConnectionError — so before the classifier it was indistinguishable from a
    dead host, and the operator went looking at the network."""
    url, stale_pin = tls_server
    exc = _raise_against(url, verify=stale_pin)
    assert isinstance(exc, requests.ConnectionError), \
        "if this stops being a ConnectionError the premise has changed"
    why = agent._why_unreachable(exc)
    assert "pinned controller cert" in why
    assert agent._CA_CERT_FILE in why, "it does not say which file to replace"
    assert "server.crt" in why, "it does not say how to fix it"


# ---- said once, not once per loop per cycle --------------------------------
def test_an_ongoing_outage_is_reported_once(quiet_reporter):
    exc = requests.ConnectionError("Connection refused")
    for _ in range(50):
        agent._report_unreachable(exc)
    assert len(quiet_reporter) == 1, quiet_reporter
    assert "cannot reach the controller" in quiet_reporter[0]


def test_a_changed_reason_is_news_and_is_reported(quiet_reporter):
    """An outage that turns from 'refused' into a cert failure is a different
    problem, and staying quiet about it would hide the thing that just changed."""
    agent._report_unreachable(requests.ConnectionError("Connection refused"))
    agent._report_unreachable(requests.ConnectionError("certificate verify failed"))
    assert len(quiet_reporter) == 2, quiet_reporter
    assert "nothing is listening" in quiet_reporter[0]
    assert "pinned controller cert" in quiet_reporter[1]


def test_recovery_is_announced_and_counts_what_was_suppressed(quiet_reporter):
    exc = requests.ConnectionError("Connection refused")
    for _ in range(10):
        agent._report_unreachable(exc)
    agent._report_reachable()
    assert len(quiet_reporter) == 2, quiet_reporter
    assert "reachable again" in quiet_reporter[1]
    assert "9" in quiet_reporter[1], "it does not say how much was suppressed"


def test_recovery_says_nothing_when_nothing_was_wrong(quiet_reporter):
    """A healthy agent must not log a recovery line on every successful call."""
    for _ in range(5):
        agent._report_reachable()
    assert quiet_reporter == []


@pytest.fixture()
def tls_server(tmp_path):
    """A real TLS listener presenting one self-signed cert, plus a DIFFERENT
    self-signed cert to pin against — exactly a renumbered controller and an
    agent still holding the old pin."""
    import http.server
    import ssl
    import subprocess

    def mkcert(tag):
        k, c = tmp_path / f"{tag}.key", tmp_path / f"{tag}.crt"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(k), "-out", str(c), "-days", "1",
                        "-subj", "/CN=controller",
                        "-addext", "subjectAltName=IP:127.0.0.1"],
                       capture_output=True, check=True)
        return str(c)

    served, stale_pin = mkcert("served"), mkcert("stale")

    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(served, str(tmp_path / "served.key"))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"https://127.0.0.1:{httpd.server_address[1]}/x", stale_pin
    finally:
        httpd.shutdown()


# ---- which network this host is actually on --------------------------------
# A fleet lost three hosts for days because the controller was at 192.168.8.139
# and those hosts were on 192.168.100.44 — different networks, no route between
# them — while thirteen other hosts reached it fine. Every log line named only
# the address being dialled, so from the host with the problem the mismatch was
# invisible, and the investigation went to firewalls, certificates and the
# controller instead.
def test_the_message_says_which_address_this_host_would_use():
    """Printing both addresses is what makes a wrong-network obvious."""
    note = agent._route_note("https://192.168.8.139:9000")
    assert "would reach it from" in note
    import re
    assert re.search(r"from \d+\.\d+\.\d+\.\d+\.", note), note


def test_no_route_at_all_is_said_outright(monkeypatch):
    """When the kernel has nothing for that network, say so — it is a stronger
    statement than showing two addresses and leaving the reader to compare."""
    import errno as _errno
    import socket as _socket

    class NoRoute(_socket.socket):
        def connect(self, addr):
            raise OSError(_errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(agent.socket, "socket", NoRoute)
    note = agent._route_note("https://192.168.8.139:9000")
    assert "NO ROUTE to 192.168.8.139" in note
    assert "nothing routes" in note


def test_a_hostname_gets_no_route_note():
    """A name that does not resolve is a DNS failure, already named by the
    classifier; a route note there would be noise or a lie."""
    assert agent._route_note("https://controller.lan:9000") == ""


def test_the_route_note_reaches_the_reported_line(quiet_reporter, monkeypatch):
    monkeypatch.setattr(agent, "CONTROLLER", "https://192.168.8.139:9000")
    agent._report_unreachable(requests.ConnectionError("Connection refused"))
    assert len(quiet_reporter) == 1
    assert "192.168.8.139" in quiet_reporter[0]
    assert "would reach it from" in quiet_reporter[0] or "NO ROUTE" in quiet_reporter[0]


def test_a_route_note_never_breaks_the_report(quiet_reporter, monkeypatch):
    """Diagnostics must not be able to take out the thing they are diagnosing."""
    monkeypatch.setattr(agent, "_route_note", lambda _u: 1 / 0)
    try:
        agent._report_unreachable(requests.ConnectionError("Connection refused"))
    except ZeroDivisionError:
        pytest.fail("a failure inside the route note silenced the outage report")
