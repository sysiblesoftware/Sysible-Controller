"""Opening the console must not be what triggers the fleet patch scan.

    "when I open controller its still scanning the hosts for potential updates.
     Why isn't it constantly scanning and updating?"

Because it wasn't scanning at all between visits. /api/fleet/updates cached for
fifteen minutes and had no background refresher, so the first request after the
TTL lapsed ran the whole sweep INLINE -- fanning out to every agent and blocking
up to the per-host timeout while the tile sat on "scanning...". The numbers were
never fresher than the last person who happened to look, and that person paid
for them.

The cache primer that already existed warms health and posture, and is
deliberately gated on recent dashboard access so an unwatched fleet is left
alone. That gate cannot fix this: it is shut exactly during the gap between
visits, which is the whole of the time the data goes stale in.

Two changes, tested here: a scan that runs on its own cadence whether or not
anyone is watching, and a reply that serves an aged-but-complete scan at once
(with its age) rather than making the reader wait for a new one.
"""
import pathlib
import sys
import threading
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SERVER_SRC = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")


@pytest.fixture
def srv(monkeypatch):
    from webgui import server
    monkeypatch.setitem(server._UPDATES_CACHE, "hosts", None)
    monkeypatch.setitem(server._UPDATES_CACHE, "ts", 0.0)
    monkeypatch.setitem(server._UPDATES_REFRESHING, "on", False)
    # reachability is re-judged from live heartbeats; not what these assert
    monkeypatch.setattr(server, "_with_live_reachability", lambda h: h)
    return server


def _recorder(server, monkeypatch, hosts=None):
    """Replace the sweep; record which thread ran it and how often."""
    calls = []
    done = threading.Event()

    def fake(live=False):
        calls.append(threading.get_ident())
        server._UPDATES_CACHE["hosts"] = hosts if hosts is not None else [{"id": "h1"}]
        server._UPDATES_CACHE["ts"] = time.time()
        done.set()
        return server._UPDATES_CACHE["hosts"], server._UPDATES_CACHE["ts"]

    monkeypatch.setattr(server, "_updates_sweep", fake)
    return calls, done


def test_an_aged_scan_is_served_at_once_not_re_run_on_the_caller(srv, monkeypatch):
    """The whole complaint. An aged scan is complete; it just has an age."""
    srv._UPDATES_CACHE["hosts"] = [{"id": "h1", "total": 3}]
    srv._UPDATES_CACHE["ts"] = time.time() - (srv._UPDATES_TTL + 60)   # stale
    monkeypatch.setattr(srv, "_fleet_ids_or_none", lambda: {"h1"})
    calls, done = _recorder(srv, monkeypatch)

    mine = threading.get_ident()
    out = srv.fleet_updates(refresh=0, live=0, user="u")

    assert out["hosts"] == [{"id": "h1", "total": 3}], "the aged scan was thrown away"
    assert out["stale"] is True, "served aged without saying so"
    assert out["ts"] == srv._UPDATES_CACHE["ts"] or out["ts"] > 0
    done.wait(5)
    assert calls, "nothing refreshed it behind the reply"
    assert mine not in calls, "the sweep ran on the caller -- they are still waiting"


def test_a_cold_cache_still_scans_rather_than_inventing_a_number(srv, monkeypatch):
    """Serving stale needs something to serve. The very first load scans."""
    monkeypatch.setattr(srv, "_fleet_ids_or_none", lambda: {"h1"})
    calls, _ = _recorder(srv, monkeypatch)
    mine = threading.get_ident()
    out = srv.fleet_updates(refresh=0, live=0, user="u")
    assert out["stale"] is False and out["cached"] is False
    assert calls == [mine], "a cold cache must be filled before answering"


def test_a_newly_enrolled_host_is_not_hidden_behind_an_aged_answer(srv, monkeypatch):
    """`_cache_covers` exists because a sweep that predates an enrolment leaves
    the new host silently absent from the patch rollup. Serving that stale would
    reintroduce exactly the bug it was written to catch, so coverage still
    forces a real scan."""
    srv._UPDATES_CACHE["hosts"] = [{"id": "h1"}]
    srv._UPDATES_CACHE["ts"] = time.time() - (srv._UPDATES_TTL + 60)
    monkeypatch.setattr(srv, "_fleet_ids_or_none", lambda: {"h1", "h2"})   # h2 just enrolled
    calls, _ = _recorder(srv, monkeypatch, hosts=[{"id": "h1"}, {"id": "h2"}])
    mine = threading.get_ident()
    out = srv.fleet_updates(refresh=0, live=0, user="u")
    assert calls == [mine], "an uncovered fleet was answered from a stale cache"
    assert out["stale"] is False
    assert {h["id"] for h in out["hosts"]} == {"h1", "h2"}


def test_a_fresh_cache_is_not_rescanned(srv, monkeypatch):
    srv._UPDATES_CACHE["hosts"] = [{"id": "h1"}]
    srv._UPDATES_CACHE["ts"] = time.time()
    monkeypatch.setattr(srv, "_fleet_ids_or_none", lambda: {"h1"})
    calls, _ = _recorder(srv, monkeypatch)
    out = srv.fleet_updates(refresh=0, live=0, user="u")
    assert out["cached"] is True and out["stale"] is False
    assert calls == [], "a fresh cache triggered a scan anyway"


def test_only_one_sweep_runs_at_a_time(srv, monkeypatch):
    """The agent task loop is SERIAL: a duplicate sweep does not just waste
    work, it queues behind the first on every host and delays whatever the
    operator runs next."""
    srv._UPDATES_CACHE["hosts"] = [{"id": "h1"}]
    srv._UPDATES_CACHE["ts"] = time.time() - (srv._UPDATES_TTL + 60)
    monkeypatch.setattr(srv, "_fleet_ids_or_none", lambda: {"h1"})
    monkeypatch.setitem(srv._UPDATES_REFRESHING, "on", True)   # one already running
    calls, _ = _recorder(srv, monkeypatch)
    srv.fleet_updates(refresh=0, live=0, user="u")
    time.sleep(0.2)
    assert calls == [], "a second sweep started while one was already running"


def test_the_patch_cache_is_kept_warm_without_anyone_watching():
    """The point of the whole change. A primer gated on dashboard access cannot
    keep this warm, because the gate is shut during the gap it has to cover."""
    assert "def _updates_prime_loop" in SERVER_SRC, "no background patch scan at all"
    body = SERVER_SRC[SERVER_SRC.index("def _updates_prime_loop"):]
    body = body[:body.index("\ndef ", 10)]
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert "_HEALTH_CACHE" not in code and "active_window" not in code, \
        "the patch primer copied the idle gate, which is the thing that cannot work here"
    assert "_updates_sweep" in code, "the primer does not actually scan"
    assert 'name="sysible-updates-prime"' in SERVER_SRC, "the loop is never started"


def test_the_primer_starts_after_the_names_it_reads_exist():
    """It reads _UPDATES_TTL as its first act. Registered beside the other
    loops, that raced the rest of the module loading."""
    start = SERVER_SRC.index('name="sysible-updates-prime"')
    assert SERVER_SRC.index("_UPDATES_TTL = ") < start, \
        "the primer thread starts before _UPDATES_TTL is defined"
    assert SERVER_SRC.index("def _updates_sweep") < start


def test_the_spinner_is_only_for_a_cold_start():
    """Reported as seeing 'scanning...' on open. With an aged scan served at
    once, a spinner there would be lying about having nothing."""
    dash = (REPO / "webgui" / "frontend" / "src" / "views" / "Dashboard.jsx").read_text(encoding="utf-8")
    assert "scanned {scanAge(at)}" in dash, "the tile does not say how old its number is"
    assert "scanning…" not in dash, "the open-the-console spinner text is still there"


def test_claiming_the_refresh_is_atomic():
    """Test-then-set is not atomic. Two operators opening the console in the
    same moment could both find the flag clear, both set it, and both sweep --
    the second merely blocking on _UPDATES_LOCK before fanning out to every
    agent again. On a serial agent task loop that is a duplicate round-trip
    queued on every host, which is the thing the flag exists to prevent."""
    body = SERVER_SRC[SERVER_SRC.index("def _claim_updates_refresh"):]
    body = body[:body.index("\ndef ", 10)]
    assert "_UPDATES_REFRESH_GATE" in body, "the flag is read and written unguarded"
    gate = body.index("with _UPDATES_REFRESH_GATE")
    assert gate < body.index('_UPDATES_REFRESHING["on"]'), \
        "the flag is touched before the lock is taken"
    # and nobody may set it outside that one function
    rest = SERVER_SRC.replace(body, "")
    assert '_UPDATES_REFRESHING["on"] = True' not in rest, \
        "something still claims the refresh without going through the gate"


def test_a_second_caller_does_not_start_a_second_sweep(srv, monkeypatch):
    """Driven, not read: many threads race for the claim, exactly one wins."""
    import concurrent.futures
    monkeypatch.setitem(srv._UPDATES_REFRESHING, "on", False)
    won = list(concurrent.futures.ThreadPoolExecutor(16).map(
        lambda _: srv._claim_updates_refresh(), range(64)))
    assert won.count(True) == 1, f"{won.count(True)} threads all believed they were the refresher"
