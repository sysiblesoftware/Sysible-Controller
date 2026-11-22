"""The apps' background loops do not run inside a test process.

Two full runs of this suite died with `Fatal Python error: Bus error` and no
failing test -- the worst shape a failure can take, because there is nothing in
the report to read. The stack said it plainly once it was captured:

    Current thread: tests/conftest.py _isolate -> backend/db.py init_db
    Thread sysible-task-reclaim: backend/db.py _connect  (PRAGMA journal_mode=WAL)

backend/app.py starts two loops AT IMPORT, remote_routes a third, and the
webgui three more from its startup handler, which TestClient fires. The autouse
_isolate fixture unlinks the SQLite file and its -wal/-shm sidecars before every
one of 2400-odd tests. Deleting a WAL index out from under an open connection is
explicitly unsafe in SQLite: the loop thread keeps the old -shm mmap'd, and a
read through that stale mapping once the file is recreated raises SIGBUS, which
kills the process rather than failing a test.

So: off in a test process, on in the service. These tests hold both halves of
that, and the source scan catches the next loop someone adds without the gate.
"""
import ast
import os
import pathlib
import re
import subprocess
import sys
import threading

REPO = pathlib.Path(__file__).resolve().parents[1]
GATED_FILES = ("backend/app.py", "backend/remote_routes.py", "webgui/server.py")


def test_no_loop_threads_are_running_in_this_process():
    """conftest imports the apps the way the suite does, so by the time any test
    runs the threads either exist or they do not."""
    live = sorted(t.name for t in threading.enumerate()
                  if t.name.startswith("sysible-"))
    assert live == [], f"background loops are running during tests: {live}"


def test_the_fixture_that_crashed_still_deletes_the_wal_sidecars():
    """The premise. If _isolate stopped removing -wal/-shm the race would be
    gone and this guard would be protecting nothing -- which is worth knowing
    rather than assuming."""
    src = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert '"-wal", "-shm"' in src or "'-wal', '-shm'" in src, \
        "_isolate no longer deletes the WAL sidecars; re-read this guard"


def test_the_switch_is_set_before_the_apps_are_imported():
    """Set after an import it does nothing, because the loops start at import."""
    src = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    switch = src.index("SYSIBLE_DISABLE_BACKGROUND_LOOPS")
    first_app_import = min(src.index("import backend.db"),
                           src.index("import backend.app"))
    assert switch < first_app_import, \
        "the switch is set after the apps are imported, so the loops already started"


def test_every_loop_start_is_behind_the_switch():
    """A source scan, so a NEW loop added without the gate fails here instead of
    crashing a run two weeks later."""
    ungated = []
    for rel in GATED_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"threading\.Thread\(\s*target=(\w+)", text):
            target = m.group(1)
            if not target.endswith("_loop"):
                continue            # one-shot workers are not the hazard
            # The guard may be an `if` above the start, or an early return in
            # the function that performs it. Look back far enough to see either.
            window = text[max(0, m.start() - 600):m.start()]
            if "_background_loops_enabled" not in window:
                line = text[:m.start()].count("\n") + 1
                ungated.append(f"{rel}:{line} starts {target}")
    assert not ungated, "background loops started without the switch:\n" + "\n".join(ungated)


def test_the_predicate_agrees_in_every_file():
    """Three copies, one meaning. Each deployable unit carries its own so none of
    them has to import the others, which is exactly how they drift."""
    bodies = {}
    for rel in GATED_FILES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_background_loops_enabled"), None)
        assert fn is not None, f"{rel} has no _background_loops_enabled"
        bodies[rel] = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
    distinct = set(bodies.values())
    assert len(distinct) == 1, \
        "the three copies of _background_loops_enabled no longer agree:\n" + \
        "\n".join(f"  {k}" for k in bodies)


def test_the_service_still_starts_them_by_default():
    """The whole point of a default: production behaviour is unchanged. Driven in
    a subprocess WITHOUT the variable, because this process has it set."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SYSIBLE_DISABLE_BACKGROUND_LOOPS",)}
    env["SYSIBLE_DATA_DIR"] = str(REPO / "tests" / ".tmp-loopcheck")
    env["SYSIBLE_API_KEY"] = "loopcheck"
    env.setdefault("PATH", "/usr/bin:/bin")
    out = subprocess.run(
        [sys.executable, "-c",
         "import threading, backend.app;"
         "print(','.join(sorted(t.name for t in threading.enumerate()"
         " if t.name.startswith('sysible-'))))"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    started = [n for n in out.stdout.strip().splitlines()[-1].split(",") if n]
    assert started, "the service no longer starts its background loops by default"
    assert "sysible-task-reclaim" in started, started
