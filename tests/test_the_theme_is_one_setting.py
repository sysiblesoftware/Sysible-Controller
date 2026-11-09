"""The theme is ONE setting for the whole suite, not one per app.

Reported as: the SLOP sign-in page was light, the Controller behind it was dark.

Two separate faults, and both had to go:

  1. THREE KEYS FOR ONE SETTING. The Controller stored its choice under
     "sysible_theme", SLOP under "slop-theme", SLEP under "slep_theme". Behind
     the gateway all three are served from the SAME ORIGIN, so they share
     localStorage and could always have agreed — they simply never read the
     same key.
  2. TWO DIFFERENT DEFAULTS. SLOP followed prefers-color-scheme; the Controller
     and SLEP hard-coded dark. So on a light-mode desktop the split appeared
     without anyone touching a toggle, which is exactly how it was found.

The fix is one script, embedded byte-identically in every console in all three
repos. Duplication is deliberate: these are separate services in separate images
with no shared bundle, so the only thing that can keep them honest is a test
that fails when a copy drifts. That is this file, and it lives in each repo so
neither of the others has to be checked out.

It also pins the script's BEHAVIOUR, because "they all embed the same string" is
worthless if the string stops migrating old keys or stops listening for changes.
"""
import hashlib
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# The canonical script. Changing it means changing this hash and the same string
# in the other two repos, which is the point: a one-sided edit fails here.
CANONICAL_SHA256 = "62c4bfd9b00d060f0697444610d4a6918c60fc29287f9b453fb9bd5764cc339b"
KEY = "sysible-theme"
LEGACY = ("sysible_theme", "slop-theme", "slep_theme")

# Every file in THIS repo that embeds the script.
EMBEDS = (
    "webgui/frontend/index.html",
)

# Files that may legitimately name a legacy key: only the script itself.
def _scripts():
    found = []
    for rel in EMBEDS:
        p = REPO / rel
        assert p.is_file(), f"{rel} is gone — did a console lose its theme script?"
        text = p.read_text()
        for m in re.finditer(r"\(function\(\)\{var K='sysible-theme'.*?\}\)\(\);", text, re.S):
            # Python sources carry it split across adjacent string literals.
            found.append((rel, _unsplit(m.group(0))))
    return found


def _unsplit(s: str) -> str:
    """Join a snippet that a Python source stores as adjacent string literals."""
    return re.sub(r'"\s*\n\s*"', "", s).replace('\\"', '"').replace("\\\\", "\\")


class TestEveryConsoleEmbedsIt:
    def test_each_file_has_exactly_one_copy(self):
        by_file = {}
        for rel, body in _scripts():
            by_file.setdefault(rel, []).append(body)
        for rel in EMBEDS:
            assert rel in by_file, f"{rel} embeds no theme script"
            assert len(by_file[rel]) >= 1

    def test_every_copy_is_byte_identical(self):
        bodies = {}
        for rel, body in _scripts():
            bodies.setdefault(body, []).append(rel)
        assert len(bodies) == 1, (
            "the theme script has drifted between files in this repo: "
            + "; ".join(f"{len(v)} copy/copies in {v}" for v in bodies.values())
        )

    def test_it_matches_the_suite_wide_canonical_script(self):
        """The hash is shared with the other two repos. If this fails because you
        changed the script ON PURPOSE, update the string and this hash in ALL
        THREE, or the consoles stop agreeing again."""
        body = _scripts()[0][1]
        got = hashlib.sha256(body.encode()).hexdigest()
        assert got == CANONICAL_SHA256, (
            f"this repo's theme script hashes to {got}, not the canonical "
            f"{CANONICAL_SHA256} — it has diverged from the other consoles"
        )


class TestWhatTheScriptActuallyDoes:
    """Identical copies of a broken script would still pass the tests above."""

    @pytest.fixture()
    def body(self):
        return _scripts()[0][1]

    def test_it_reads_the_one_shared_key(self, body):
        assert f"'{KEY}'" in body

    @pytest.mark.parametrize("old", LEGACY)
    def test_it_migrates_each_legacy_key_forward(self, body, old):
        """Nobody loses the theme they had already chosen."""
        assert f"'{old}'" in body, f"{old} is no longer migrated"

    def test_it_falls_back_to_the_os_preference(self, body):
        """The Controller and SLEP hard-coded dark, which is what made a
        light-mode desktop show two different themes."""
        assert "prefers-color-scheme" in body

    def test_it_applies_before_react_or_any_app_code(self, body):
        assert "setAttribute('data-theme'" in body

    def test_it_repaints_when_another_app_changes_it(self, body):
        """'changing changes them all', including consoles already open."""
        assert "'storage'" in body and "addEventListener" in body

    def test_it_exposes_a_setter_for_the_toggles(self, body):
        assert "window.sysibleTheme" in body and "set:" in body

    def test_storage_access_cannot_throw_the_page_down(self, body):
        """localStorage throws outright in some privacy modes; a theme is never
        worth a blank console."""
        assert body.count("catch") >= 3


class TestNothingBypassesIt:
    """A second place that reads or writes a theme key is how this started."""

    def test_no_app_code_touches_a_legacy_key(self):
        offenders = []
        for p in REPO.rglob("*"):
            if p.is_dir() or p.suffix not in (".py", ".js", ".jsx", ".html"):
                continue
            rel = p.relative_to(REPO).as_posix()
            if any(seg in rel for seg in ("node_modules/", "/dist/", ".git/", "tests/")):
                continue
            text = p.read_text(errors="ignore")
            for old in LEGACY:
                for m in re.finditer(rf"localStorage\.(get|set)Item\(\s*['\"]{re.escape(old)}['\"]", text):
                    # Inside the canonical script is the only legitimate place.
                    if f"var K='{KEY}'" in text[max(0, m.start() - 400):m.start()]:
                        continue
                    offenders.append(f"{rel}: {old}")
        assert not offenders, (
            "these read or write a per-app theme key directly, which is what "
            "let the consoles disagree: " + ", ".join(offenders)
        )
