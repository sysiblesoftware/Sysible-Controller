"""Rolling maintenance across a pool: one node out at a time, health-gated.

The fleet screens dispatch to every selected host at once. On a load-balanced
pool that is the whole service going down together — which is exactly what a
schedule like "Install all updates 02:00" then "Reboot 04:00" does to one today.

Rolling turns that into: take ONE member out of rotation, do the work, wait for
it to come back, prove it is healthy, put it back, and only then start the next.

This module is the state machine and nothing else. Every side effect — draining,
dispatching the command, probing health, waiting for a rebooted host — arrives as
a callable, so the rules below are tested against a fake pool rather than argued
about. The wiring to real hosts lives in webgui/server.py, which owns dispatch.

The rules, in the order they matter:

  1. NEVER drain the last healthy member. A rolling update that takes the pool to
     zero is worse than no rolling update at all: the un-rolled version at least
     failed loudly and all at once.
  2. Stop at the first failure. The point of going one at a time is that a bad
     patch costs you one node; carrying on costs you all of them.
  3. A member that failed stays DRAINED. Returning a node that just failed its
     health check to a live pool serves errors to real traffic.
  4. A failed undrain is a failure too. Silently shrinking the pool by one node
     per maintenance window is a slow outage nobody notices until the last one.
  5. Refuse to start on an already-degraded pool unless told otherwise. Rolling
     through a pool that is one node down is how one down node becomes none up.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Phases, in the order a member goes through them. Named so the console can show
# where a member is without the UI knowing what any of them do.
PHASES = ("drain", "act", "settle", "health", "undrain")

# Terminal states for a whole run.
OK = "ok"
FAILED = "failed"
REFUSED = "refused"        # never started: the pre-flight said no
CANCELLED = "cancelled"


@dataclass
class Event:
    """One thing that happened, in order. The console's live log."""
    host: str
    phase: str
    status: str            # "ok" | "error" | "skipped" | "info"
    detail: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"host": self.host, "phase": self.phase, "status": self.status,
                "detail": self.detail, "ts": self.ts}


class Pool:
    """The callables a run needs, in one place so tests can supply fakes.

    Each returns (ok, detail). `health` additionally answers for a host that this
    run has not touched, which is how the pre-flight and rule 1 work.
    """

    def __init__(self, drain, undrain, act, health, settle=None):
        self.drain = drain
        self.undrain = undrain
        self.act = act
        self.health = health
        # After the action (a reboot, say) the host is gone for a while. `settle`
        # blocks until it is back, or gives up. Without one, health is polled
        # directly, which is right for actions that do not interrupt the host.
        self.settle = settle


def batches(members: list[str], max_unavailable: int = 1) -> list[list[str]]:
    """Split the roll order into groups that may be out of rotation together.

    Default 1 — the only value that is safe without knowing the pool's capacity.
    Anything larger is the operator saying "this pool can lose N and still serve".
    """
    n = max(1, int(max_unavailable or 1))
    return [members[i:i + n] for i in range(0, len(members), n)]


def preflight(members: list[str], health, *, allow_degraded: bool = False) -> tuple[bool, str, dict]:
    """Ask every member how it is before touching any of them.

    Returns (may_start, reason, {host: healthy}). A pool that is already short is
    refused by default: the operator can see it and decide, which is a different
    thing from a maintenance window quietly finishing the job.
    """
    states: dict[str, bool] = {}
    for h in members:
        try:
            ok, _ = health(h)
        except Exception:
            ok = False
        states[h] = bool(ok)
    healthy = [h for h, ok in states.items() if ok]
    if not members:
        return False, "the pool has no members", states
    if not healthy:
        return False, ("no member of this pool is healthy — there is nothing to roll "
                       "through, and draining one would take the pool to zero"), states
    if len(healthy) < len(members) and not allow_degraded:
        down = ", ".join(h for h in members if not states[h])
        return False, (f"{len(members) - len(healthy)} of {len(members)} members are "
                       f"already unhealthy ({down}). Rolling through a degraded pool is "
                       f"how one down node becomes none up — fix them, or start the run "
                       f"with 'proceed while degraded'."), states
    return True, "", states


def run(members: list[str], pool: Pool, *, max_unavailable: int = 1,
        allow_degraded: bool = False, on_event=None, cancelled=None,
        min_healthy: int = 1) -> dict:
    """Roll the action through the pool. Returns a result dict.

    `cancelled` is polled between steps so an operator can stop a run without
    killing the process; a cancelled run stops where it is, with whatever it has
    already put back still in rotation.
    """
    events: list[Event] = []

    def emit(host, phase, status, detail=""):
        e = Event(host, phase, status, detail)
        events.append(e)
        if on_event:
            try:
                on_event(e)
            except Exception:
                pass
        return e

    def stop_requested() -> bool:
        try:
            return bool(cancelled and cancelled())
        except Exception:
            return False

    def result(state, detail, done, drained_out):
        return {"state": state, "detail": detail, "done": done,
                "members": list(members), "drained": sorted(drained_out),
                "skipped": [m for m in members if m not in done and m not in drained_out],
                "events": [e.as_dict() for e in events]}

    may, why, states = preflight(members, pool.health, allow_degraded=allow_degraded)
    if not may:
        emit("", "drain", "error", why)
        return result(REFUSED, why, [], set())

    healthy_now = {h for h, ok in states.items() if ok}
    done: list[str] = []
    left_drained: set[str] = set()

    for group in batches(members, max_unavailable):
        if stop_requested():
            return result(CANCELLED, "cancelled by the operator", done, left_drained)

        # RULE 1, checked against the pool as it is RIGHT NOW rather than as it was
        # when the run started: an earlier member may have failed and been left out.
        taking = [h for h in group if h in healthy_now]
        if len(healthy_now) - len(taking) < min_healthy:
            why = (f"draining {', '.join(group)} would leave "
                   f"{len(healthy_now) - len(taking)} healthy member(s), below the "
                   f"{min_healthy} this pool must keep serving")
            emit(group[0], "drain", "error", why)
            return result(FAILED, why, done, left_drained)

        # The group moves through the phases TOGETHER: every member of it is out
        # of rotation at once, which is what max_unavailable means. Doing it
        # host-by-host inside the group would silently make every setting behave
        # like 1 — a control that reads as configured and does nothing.
        drained: list[str] = []

        def bail(detail, blamed, state=FAILED):
            """Stop, putting back the members of this group that are fine.

            Only the one that actually failed stays out (rule 3). A bystander —
            drained but never touched, or patched and healthy — is a node's worth
            of capacity lost for nothing, so it goes back, and only after its own
            health check says it can.
            """
            for h in drained:
                if h in blamed:
                    continue
                ok_h, d_h = _safe(pool.health, h)
                emit(h, "health", "ok" if ok_h else "error", d_h)
                if not ok_h:
                    continue                      # not healthy: it stays out too
                ok_u, d_u = _safe(pool.undrain, h)
                emit(h, "undrain", "ok" if ok_u else "error", d_u)
                if ok_u:
                    left_drained.discard(h)
                    healthy_now.add(h)
            return result(state, detail, done, left_drained)

        for host in group:
            ok, detail = _safe(pool.drain, host)
            emit(host, "drain", "ok" if ok else "error", detail)
            if not ok:
                # It is still in rotation, so nothing is blamed — but whatever we
                # already took out of this group has to go back.
                return bail(f"{host}: could not be taken out of rotation — {detail}",
                            set())
            healthy_now.discard(host)
            left_drained.add(host)
            drained.append(host)

        for host in drained:
            if stop_requested():
                # A cancellation is not a failure, but it must still put back what
                # it took out — stopping with a member drained is the outage this
                # whole design exists to avoid.
                return bail("cancelled by the operator", set(), state=CANCELLED)
            ok, detail = _safe(pool.act, host)
            emit(host, "act", "ok" if ok else "error", detail)
            if not ok:
                # RULE 3: it stays drained. It is out of rotation and broken; the
                # one thing not to do is hand it traffic.
                return bail(f"{host}: the maintenance action failed — {detail}. "
                            f"It has been LEFT OUT of rotation.", {host})

        if pool.settle:
            for host in drained:
                ok, detail = _safe(pool.settle, host)
                emit(host, "settle", "ok" if ok else "error", detail)
                if not ok:
                    return bail(f"{host}: never came back after the action — {detail}. "
                                f"It has been LEFT OUT of rotation.", {host})

        for host in drained:
            ok, detail = _safe(pool.health, host)
            emit(host, "health", "ok" if ok else "error", detail)
            if not ok:
                return bail(f"{host}: did not come back healthy — {detail}. "
                            f"It has been LEFT OUT of rotation.", {host})

        for host in drained:
            ok, detail = _safe(pool.undrain, host)
            emit(host, "undrain", "ok" if ok else "error", detail)
            if not ok:
                # RULE 4. The node is fine; the pool is one short and will stay
                # that way. That is a failure, not a footnote.
                return bail(f"{host}: patched and healthy, but could NOT be returned "
                            f"to rotation — {detail}. The pool is one member short.",
                            {host})
            left_drained.discard(host)
            healthy_now.add(host)
            done.append(host)

    return result(OK, f"{len(done)} of {len(members)} member(s) rolled", done, left_drained)


def _safe(fn, host) -> tuple[bool, str]:
    """A provider that raises is a provider that failed — never one that passed."""
    try:
        ok, detail = fn(host)
        return bool(ok), str(detail or "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
