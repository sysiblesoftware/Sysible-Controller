"""The SLOP checkout is found whether it sits beside this repo or above it.

This repo lives in two shapes, and the cross-repo tests have to work in both:

    standalone   <parent>/Sysible-Controller  +  <parent>/sysible-linux-...
    vendored     <slop>/apps/controller

Every resolver here used to look only SIDEWAYS. In the vendored tree -- the one
SLOP builds and ships from -- that found apps/ and gave up, so 21 cross-app
tests skipped with "the SLOP checkout is not next to this repo" while the
checkout was two directories up. The skip reads as a fact about the machine,
which is why it survived.

Driven in a subprocess against a FAKE tree, so this proves the resolution logic
rather than agreeing with whatever happens to be installed here.
"""
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
RESOLVER = HERE / "_slop.py"


def _slop_root(root: pathlib.Path) -> pathlib.Path:
    """A directory that looks like a real SLOP checkout from the inside."""
    (root / "flashback" / "backend").mkdir(parents=True, exist_ok=True)
    (root / "flashback" / "backend" / "store.py").write_text("# store\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    return root


def _controller_at(repo: pathlib.Path) -> pathlib.Path:
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(RESOLVER, repo / "tests" / "_slop.py")
    return repo


def _ask(repo: pathlib.Path, env_dir=None) -> str:
    """What slop_dir() answers for a controller checkout at `repo`."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}
    if env_dir is not None:
        env["SYSIBLE_SLOP_DIR"] = str(env_dir)
    out = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, 'tests');\n"
         "import _slop; print(_slop.slop_dir() or '')"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_found_when_it_sits_beside_this_repo(tmp_path):
    parent = tmp_path / "work"
    slop = _slop_root(parent / "sysible-linux-operations-platform")
    repo = _controller_at(parent / "Sysible-Controller")
    assert _ask(repo) == str(slop)


def test_found_when_this_repo_is_vendored_inside_it(tmp_path):
    """The case that was broken. apps/ is not SLOP; the directory above it is."""
    slop = _slop_root(tmp_path / "slop-root")
    repo = _controller_at(slop / "apps" / "controller")
    assert _ask(repo) == str(slop)


def test_found_when_the_checkout_is_named_slop(tmp_path):
    parent = tmp_path / "work"
    slop = _slop_root(parent / "slop")
    repo = _controller_at(parent / "Sysible-Controller")
    assert _ask(repo) == str(slop)


def test_a_directory_with_the_right_name_but_no_checkout_is_not_it(tmp_path):
    """Named right, empty inside. Matching on the name would return a path whose
    files do not exist, and the failure would land wherever it was first read."""
    parent = tmp_path / "work"
    (parent / "sysible-linux-operations-platform").mkdir(parents=True)
    repo = _controller_at(parent / "Sysible-Controller")
    assert _ask(repo) == ""


def test_an_ancestor_that_is_not_a_checkout_is_not_it(tmp_path):
    """The ancestor walk passes through ordinary directories on its way up. One
    of them answering would be worse than finding nothing."""
    repo = _controller_at(tmp_path / "a" / "b" / "c" / "controller")
    assert _ask(repo) == ""


def test_an_explicit_SYSIBLE_SLOP_DIR_wins(tmp_path):
    """An operator pointing the suite somewhere specific is not overruled by a
    checkout that happens to be lying beside it."""
    parent = tmp_path / "work"
    _slop_root(parent / "sysible-linux-operations-platform")
    chosen = _slop_root(tmp_path / "elsewhere")
    repo = _controller_at(parent / "Sysible-Controller")
    assert _ask(repo, env_dir=chosen) == str(chosen)
