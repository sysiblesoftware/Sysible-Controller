"""Rolling maintenance: the rules that keep a pool serving while you patch it.

Today "Install all updates" at 02:00 and "Reboot" at 04:00 dispatch to every
selected host at once. Point that at a load-balanced pool and the whole service
goes down together, on a schedule, unattended.

Rolling takes one member out of rotation at a time. That is only an improvement
if it is strictly safer than doing nothing, so these tests are written from the
failure side: each one is a way a rolling run could take the service down, or
quietly shrink the pool, and the assertion is that it does not.

The engine takes its side effects as callables, so every case below is a real run
against a fake pool — the ordering, the gates and the stop-on-failure are executed,
not inspected.
"""
import pytest

from webgui import rolling


class FakePool:
    """A pool of hosts that can be drained, acted on, and asked how they are."""

    def __init__(self, members, *, unhealthy=(), fail_drain=(), fail_act=(),
                 fail_health_after=(), fail_undrain=(), fail_settle=()):
        self.members = list(members)
        self.rotation = {h: True for h in members}       # in rotation?
        self.up = {h: h not in unhealthy for h in members}
        self.fail_drain = set(fail_drain)
        self.fail_act = set(fail_act)
        self.fail_health_after = set(fail_health_after)  # unhealthy once acted on
        self.fail_undrain = set(fail_undrain)
        self.fail_settle = set(fail_settle)
        self.acted = []
        self.calls = []
        # The high-water mark of how many members were out of rotation at once.
        self.max_out = 0
        # ...and whether the pool was ever serving nothing at all.
        self.ever_empty = False

    def _observe(self):
        out = [h for h in self.members if not self.rotation[h]]
        self.max_out = max(self.max_out, len(out))
        serving = [h for h in self.members if self.rotation[h] and self.up[h]]
        if not serving:
            self.ever_empty = True

    def drain(self, host):
        self.calls.append(("drain", host))
        if host in self.fail_drain:
            return False, "the balancer refused"
        self.rotation[host] = False
        self._observe()
        return True, "out of rotation"

    def undrain(self, host):
        self.calls.append(("undrain", host))
        if host in self.fail_undrain:
            return False, "the balancer refused"
        self.rotation[host] = True
        self._observe()
        return True, "back in rotation"

    def act(self, host):
        self.calls.append(("act", host))
        if not self.rotation[host]:
            pass                                   # correct: it is drained first
        else:                                      # pragma: no cover - guarded below
            raise AssertionError(f"{host} was acted on while still in rotation")
        self.acted.append(host)
        if host in self.fail_act:
            return False, "exit 1"
        if host in self.fail_health_after:
            self.up[host] = False
        return True, "done"

    def settle(self, host):
        self.calls.append(("settle", host))
        if host in self.fail_settle:
            return False, "never came back"
        return True, "back"

    def health(self, host):
        self.calls.append(("health", host))
        return self.up[host], "up" if self.up[host] else "down"

    def as_pool(self, with_settle=False):
        return rolling.Pool(self.drain, self.undrain, self.act, self.health,
                            settle=self.settle if with_settle else None)


THREE = ["web-1", "web-2", "web-3"]


# ---- the reason this exists ------------------------------------------------
def test_the_pool_never_loses_more_than_one_member_at_a_time():
    p = FakePool(THREE)
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.OK, r["detail"]
    assert p.acted == THREE, "the members were not rolled in order"
    assert p.max_out == 1, f"{p.max_out} members were out of rotation together"
    assert not p.ever_empty, "the pool stopped serving entirely during the run"


def test_every_member_is_drained_before_it_is_touched():
    """FakePool.act raises if its host is still in rotation, so a run that
    dispatches first and drains afterwards cannot pass."""
    p = FakePool(THREE)
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.OK
    for h in THREE:
        mine = [c for c in p.calls if c[1] == h]
        assert mine.index(("drain", h)) < mine.index(("act", h)) < mine.index(("undrain", h)), \
            f"{h} was acted on outside the drain/undrain window: {mine}"


def test_a_member_goes_back_in_before_the_next_comes_out():
    p = FakePool(THREE)
    rolling.run(THREE, p.as_pool())
    seq = [c for c in p.calls if c[0] in ("drain", "undrain")]
    assert seq == [("drain", "web-1"), ("undrain", "web-1"),
                   ("drain", "web-2"), ("undrain", "web-2"),
                   ("drain", "web-3"), ("undrain", "web-3")]


# ---- rule 1: never take the pool to zero -----------------------------------
def test_a_single_member_pool_is_refused_rather_than_taken_down():
    """One node behind a VIP is not a pool you can roll — draining it IS the
    outage. Better to say so than to perform one."""
    p = FakePool(["only-1"])
    r = rolling.run(["only-1"], p.as_pool())
    assert r["state"] == rolling.FAILED
    assert "must keep serving" in r["detail"]
    assert p.acted == [], "the only member was patched anyway"
    assert p.rotation["only-1"] is True, "the only member was left out of rotation"


def test_max_unavailable_cannot_empty_the_pool():
    """An operator who sets max_unavailable=2 on a two-node pool has asked for an
    outage. The pool's floor wins."""
    p = FakePool(["web-1", "web-2"])
    r = rolling.run(["web-1", "web-2"], p.as_pool(), max_unavailable=2)
    assert r["state"] == rolling.FAILED
    assert p.acted == []
    assert not p.ever_empty


def test_a_larger_pool_may_take_more_than_one_out_when_asked():
    """max_unavailable is the operator saying this pool can lose N and still
    serve — it has to actually do that, or it is a setting that does nothing."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    p = FakePool(four)
    r = rolling.run(four, p.as_pool(), max_unavailable=2)
    assert r["state"] == rolling.OK
    assert p.max_out == 2
    assert not p.ever_empty


def test_the_floor_is_checked_against_the_pool_as_it_is_now():
    """A member that failed earlier is still out. The next batch has to be judged
    against what is actually serving, not against the pool as it was at 02:00."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    # web-1 breaks and is left out; the run stops there, so 2,3,4 are untouched.
    p = FakePool(four, fail_health_after=["web-1"])
    r = rolling.run(four, p.as_pool())
    assert r["state"] == rolling.FAILED
    assert p.rotation["web-1"] is False
    assert p.acted == ["web-1"], "the run carried on after a member failed"


# ---- rule 2: stop at the first failure -------------------------------------
def test_a_failed_action_stops_the_run():
    """The whole point of one at a time: a bad patch costs one node, not four."""
    p = FakePool(THREE, fail_act=["web-2"])
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.FAILED
    assert p.acted == ["web-1", "web-2"], "web-3 was patched after web-2 failed"
    assert r["skipped"] == ["web-3"]


def test_a_host_that_never_comes_back_stops_the_run():
    p = FakePool(THREE, fail_settle=["web-2"])
    r = rolling.run(THREE, p.as_pool(with_settle=True))
    assert r["state"] == rolling.FAILED
    assert "never came back" in r["detail"]
    assert "web-3" in r["skipped"]


def test_a_provider_that_raises_is_a_failure_not_a_pass():
    """An exception from a drain script must not read as 'drained'."""
    def boom(host):
        raise RuntimeError("socket closed")
    p = FakePool(THREE)
    pool = rolling.Pool(boom, p.undrain, p.act, p.health)
    r = rolling.run(THREE, pool)
    assert r["state"] == rolling.FAILED
    assert "socket closed" in r["detail"]
    assert p.acted == [], "the action ran even though the drain blew up"


# ---- rule 3: a failed member stays out -------------------------------------
def test_a_member_that_fails_its_health_check_is_not_put_back():
    """Returning a node that just failed its health check to a live pool serves
    errors to real traffic — the one outcome worse than being a node short."""
    p = FakePool(THREE, fail_health_after=["web-1"])
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.FAILED
    assert p.rotation["web-1"] is False, "a failed member was returned to rotation"
    assert r["drained"] == ["web-1"]
    assert "LEFT OUT of rotation" in r["detail"]


def test_a_member_whose_action_failed_is_not_put_back():
    p = FakePool(THREE, fail_act=["web-1"])
    r = rolling.run(THREE, p.as_pool())
    assert p.rotation["web-1"] is False
    assert ("undrain", "web-1") not in p.calls


def test_health_is_checked_before_the_member_goes_back():
    p = FakePool(THREE)
    rolling.run(THREE, p.as_pool())
    for h in THREE:
        mine = [c for c in p.calls if c[1] == h]
        assert mine.index(("health", h)) < mine.index(("undrain", h)), \
            f"{h} was returned to rotation before anyone asked if it was up"


# ---- rule 4: a pool that quietly shrinks ------------------------------------
def test_failing_to_return_a_member_is_a_failure():
    """The node is fine and the pool is one short. Left as a warning, a monthly
    window removes a node a month until the last one is carrying everything."""
    p = FakePool(THREE, fail_undrain=["web-1"])
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.FAILED
    assert "one member short" in r["detail"]
    assert p.acted == ["web-1"], "the run carried on shrinking the pool"


# ---- rule 5: do not roll a pool that is already down ------------------------
def test_an_already_degraded_pool_is_refused():
    p = FakePool(THREE, unhealthy=["web-3"])
    r = rolling.run(THREE, p.as_pool())
    assert r["state"] == rolling.REFUSED
    assert "already unhealthy" in r["detail"] and "web-3" in r["detail"]
    assert p.acted == []


def test_a_degraded_pool_can_be_rolled_deliberately():
    """Sometimes the down node is the one you are trying to fix. The operator can
    say so — the point is that it is a decision, not a default."""
    p = FakePool(THREE, unhealthy=["web-3"])
    r = rolling.run(THREE, p.as_pool(), allow_degraded=True)
    assert r["state"] in (rolling.OK, rolling.FAILED)
    assert p.acted, "proceeding while degraded still did nothing"
    assert not p.ever_empty


def test_a_pool_with_nothing_healthy_is_refused_even_deliberately():
    """There is no node to keep serving, so every drain is an outage."""
    p = FakePool(THREE, unhealthy=THREE)
    r = rolling.run(THREE, p.as_pool(), allow_degraded=True)
    assert r["state"] == rolling.REFUSED
    assert p.acted == []


def test_an_empty_pool_is_refused():
    p = FakePool([])
    r = rolling.run([], p.as_pool())
    assert r["state"] == rolling.REFUSED


# ---- an operator can stop it -----------------------------------------------
def test_a_cancelled_run_stops_without_leaving_a_member_out():
    """Cancel between members, not mid-member: stopping with a node drained is
    the same outage the whole design is avoiding."""
    p = FakePool(["web-1", "web-2", "web-3", "web-4"])
    seen = {"n": 0}

    def cancelled():
        seen["n"] += 1
        return len(p.acted) >= 2          # ask to stop once two are done

    r = rolling.run(["web-1", "web-2", "web-3", "web-4"], p.as_pool(),
                    cancelled=cancelled)
    assert r["state"] == rolling.CANCELLED
    assert p.acted == ["web-1", "web-2"]
    assert all(p.rotation[h] for h in p.members), \
        "the run stopped with a member still out of rotation"


# ---- what the console gets to show -----------------------------------------
def test_every_step_is_reported_in_order():
    p = FakePool(["web-1", "web-2"])
    seen = []
    r = rolling.run(["web-1", "web-2"], p.as_pool(), on_event=lambda e: seen.append(e))
    assert [e.phase for e in seen if e.host == "web-1"] == \
        ["drain", "act", "health", "undrain"]
    assert r["events"][0]["host"] == "web-1"
    assert all(set(e) >= {"host", "phase", "status", "detail", "ts"} for e in r["events"])


def test_a_refusal_says_which_members_were_never_touched():
    p = FakePool(THREE, fail_act=["web-1"])
    r = rolling.run(THREE, p.as_pool())
    assert r["skipped"] == ["web-2", "web-3"]
    assert r["members"] == THREE


def test_batches_default_to_one_at_a_time():
    assert rolling.batches(THREE) == [["web-1"], ["web-2"], ["web-3"]]
    assert rolling.batches(THREE, 2) == [["web-1", "web-2"], ["web-3"]]
    # A nonsense value must not become "all of them at once".
    for bad in (0, -5, None):
        assert rolling.batches(THREE, bad) == [["web-1"], ["web-2"], ["web-3"]]


# ---- a bystander in a failed batch is not collateral -----------------------
def test_a_healthy_bystander_goes_back_when_the_batch_fails():
    """With max_unavailable=2, web-1 and web-2 come out together. If web-2's
    patch fails, web-1 is fine, patched and out of rotation for no reason —
    leaving it there costs a node's capacity to punish the wrong host."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    p = FakePool(four, fail_act=["web-2"])
    r = rolling.run(four, p.as_pool(), max_unavailable=2)

    assert r["state"] == rolling.FAILED
    assert p.rotation["web-1"] is True, "the healthy member of the batch was left out"
    assert p.rotation["web-2"] is False, "the member that failed was returned to rotation"
    assert r["drained"] == ["web-2"]


def test_a_bystander_that_is_not_healthy_stays_out_too():
    """Putting it back is conditional on its own health check, not on being
    someone else's collateral."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    p = FakePool(four, fail_act=["web-2"], fail_health_after=["web-1"])
    r = rolling.run(four, p.as_pool(), max_unavailable=2)

    assert r["state"] == rolling.FAILED
    assert p.rotation["web-1"] is False, "an unhealthy member was returned to rotation"
    assert sorted(r["drained"]) == ["web-1", "web-2"]


def test_a_failed_drain_puts_back_the_rest_of_its_batch():
    """web-1 drained, web-2 would not. web-1 is untouched and out of rotation."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    p = FakePool(four, fail_drain=["web-2"])
    r = rolling.run(four, p.as_pool(), max_unavailable=2)

    assert r["state"] == rolling.FAILED
    assert "could not be taken out of rotation" in r["detail"]
    assert p.rotation["web-1"] is True, "a member drained for nothing was left out"
    assert p.acted == [], "the batch was patched despite one member never draining"


def test_a_cancellation_mid_batch_still_reads_as_cancelled():
    """It is a decision, not a fault. Reporting it as FAILED sends someone
    looking for a broken host that does not exist."""
    four = ["web-1", "web-2", "web-3", "web-4"]
    p = FakePool(four)
    r = rolling.run(four, p.as_pool(), max_unavailable=2,
                    cancelled=lambda: len(p.acted) >= 1)
    assert r["state"] == rolling.CANCELLED
    assert all(p.rotation[h] for h in four), \
        "cancelling left a member out of rotation"
