"""Bulk enrollment, driven through the real component in a real browser.

tests/test_a_list_of_hosts_can_be_enrolled_at_once.py covers the parser and the
route. This covers the half that only exists once the screen is running: which
rows arrive ticked, what a skipped line shows instead of a checkbox, and -- the
one that matters -- exactly which hosts a click posts.

It bundles the ACTUAL HostEnrollment.jsx with esbuild against a stand-in api
module, so a change to the view is a change to what this drives. A
re-implementation of the component in the test would pass forever.

Skips cleanly where esbuild, node_modules or Chromium are missing; those are
build-machine facts, not failures.

Run: pytest tests/
"""
import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = REPO / "webgui" / "frontend"
VIEW = FRONTEND / "src" / "views" / "HostEnrollment.jsx"
ESBUILD = FRONTEND / "node_modules" / ".bin" / "esbuild"
NODE_MODULES = FRONTEND / "node_modules"

_CHROMIUM_CANDIDATES = ("/opt/pw-browsers/chromium", "/usr/bin/chromium",
                        "/usr/bin/chromium-browser", "/usr/bin/google-chrome")


def _chromium():
    for c in _CHROMIUM_CANDIDATES:
        if pathlib.Path(c).is_file():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def _playwright():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not ESBUILD.is_file(), reason="esbuild is not installed"),
    pytest.mark.skipif(not _playwright(), reason="playwright is not installed"),
    pytest.mark.skipif(not _chromium(), reason="no chromium to drive"),
]

# What the Controller reports for the pasted file. Covers every shape the table
# has to render: a line that cannot be enrolled, two that can, one that can but
# collides with an existing name, and one whose address is already managed.
_PREVIEW = {
    "rows": [
        {"line": 1, "address": "127.0.0.1", "names": ["localhost"], "name": "",
         "enrollable": False, "skip": "loopback", "skip_reason": "loopback address"},
        {"line": 2, "address": "192.168.155.135", "names": ["ubuntu-lab"],
         "name": "ubuntu-lab", "enrollable": True},
        {"line": 3, "address": "10.0.0.5", "names": ["db.internal", "db"],
         "name": "db", "enrollable": True},
        {"line": 4, "address": "10.0.0.6", "names": ["web-01"], "name": "web-01",
         "enrollable": True, "conflict": "a host called 'web-01' already exists"},
        {"line": 5, "address": "10.0.0.7", "names": ["old"], "name": "old",
         "enrollable": False, "skip": "managed",
         "skip_reason": "already managed as 'legacy-01'"},
        # AFTER the one that fails, deliberately. With the failure last,
        # "a failure does not stop the rest" asserts nothing: an early `break`
        # skips nothing and the test stays green. Found exactly that way.
        {"line": 6, "address": "10.0.0.8", "names": ["app-01"], "name": "app-01",
         "enrollable": True},
    ],
}

_STUB_API = """
const calls = []; window.__calls = calls;
const PREVIEW = __PREVIEW__;
const explicit = {
  hostsFilePreview: (t) => { calls.push(["preview", t.length]); return Promise.resolve(PREVIEW); },
  enrollSsh: (b) => {
    calls.push(["enroll", b.name, b.ip, b.username, b.environment]);
    // One host fails, so the run has to carry on past it and say so.
    if (b.name === "db") return Promise.reject(new Error("SSH authentication failed"));
    return Promise.resolve({ detail: b.name + " enrolled." });
  },
  environments: () => Promise.resolve(["prod", "lab"]),
  enrollmentPause: () => Promise.resolve({ paused: false }),
  portalStatus: () => Promise.resolve({}),
};
// Anything else the view asks for answers empty, so it can mount with no backend.
const api = new Proxy(explicit, {
  get(t, k) { return (k in t) ? t[k] : (typeof k === "string" ? () => Promise.resolve([]) : undefined); },
});
export default api; export { api };
"""

_STUB_CONFIRM = """
export function confirmDialog(msg) {
  (window.__confirms = window.__confirms || []).push(msg);
  return Promise.resolve(true);
}
"""

_MAIN = """
import React from "react";
import { createRoot } from "react-dom/client";
import HostEnrollment from "./views/HostEnrollment.jsx";
createRoot(document.getElementById("root")).render(<HostEnrollment />);
"""

_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<title>Host Enrollment</title><link rel="stylesheet" href="./styles.css">'
         '</head><body><div id="root"></div><script src="./app.js"></script></body></html>')

HOSTS_FILE = """127.0.0.1 localhost
192.168.155.135  ubuntu-lab
10.0.0.5         db.internal db
10.0.0.6         web-01
10.0.0.7         old
10.0.0.8         app-01
"""


@pytest.fixture(scope="module")
def page_dir(tmp_path_factory):
    """The real view, bundled against the stand-in api."""
    d = tmp_path_factory.mktemp("bulk-enroll")
    src = d / "src"
    (src / "views").mkdir(parents=True)
    shutil.copy2(VIEW, src / "views" / "HostEnrollment.jsx")
    (src / "api.js").write_text(
        _STUB_API.replace("__PREVIEW__", json.dumps(_PREVIEW)), encoding="utf-8")
    (src / "confirmDialog.js").write_text(_STUB_CONFIRM, encoding="utf-8")
    (src / "main.jsx").write_text(_MAIN, encoding="utf-8")
    (d / "index.html").write_text(_HTML, encoding="utf-8")
    shutil.copy2(FRONTEND / "src" / "styles.css", d / "styles.css")
    # esbuild resolves react from here.
    (d / "node_modules").symlink_to(NODE_MODULES)
    r = subprocess.run(
        [str(ESBUILD), str(src / "main.jsx"), "--bundle", "--loader:.jsx=jsx",
         "--jsx=automatic", "--format=iife", f"--outfile={d / 'app.js'}",
         "--log-level=error"],
        capture_output=True, text=True, cwd=str(FRONTEND))
    assert r.returncode == 0, f"the view does not bundle:\n{r.stderr}"
    return d


@pytest.fixture(scope="module")
def driven(page_dir):
    """Paste the file, read it, enrol what is ticked. Returns what happened."""
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        pg = b.new_page(viewport={"width": 1180, "height": 1200})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"file://{page_dir}/index.html", wait_until="load")
        pg.wait_for_timeout(500)
        pg.get_by_role("button", name="Enroll a Host", exact=True).click()
        pg.wait_for_timeout(300)

        ta = pg.locator("textarea")
        assert ta.count(), "the bulk fieldset did not render"
        ta.first.fill(HOSTS_FILE)
        pg.get_by_role("button", name="Read the list").click()
        pg.wait_for_timeout(400)

        out["rows"] = pg.evaluate(
            """() => [...document.querySelectorAll('table tbody tr')].map(tr => {
                 const cb = tr.querySelector('input[type=checkbox]');
                 const nm = tr.querySelector('td:nth-child(3) input');
                 return { ticked: cb ? cb.checked : null, hasCheckbox: !!cb,
                          addr: tr.children[1].textContent.trim(),
                          name: nm ? nm.value : null,
                          note: tr.children[3].textContent.trim() };
               })""")
        out["enroll_label"] = pg.get_by_role(
            "button", name="Enroll", exact=False).last.text_content()

        pg.locator("input[type=password]").last.fill("hunter2")
        pg.get_by_role("button", name="Enroll", exact=False).last.click()
        pg.wait_for_timeout(1500)

        out["confirms"] = pg.evaluate("() => window.__confirms || []")
        out["calls"] = pg.evaluate("() => window.__calls || []")
        out["body"] = pg.locator("body").text_content()
        out["password_after"] = pg.evaluate(
            """() => [...document.querySelectorAll('input[type=password]')].map(i => i.value)""")
        out["errors"] = errors
        b.close()
    return out


# ---- what the table shows -------------------------------------------------
def test_the_component_renders_without_throwing(driven):
    assert not driven["errors"], driven["errors"]


def test_every_line_is_shown_including_the_ones_that_cannot_be_enrolled(driven):
    """A 40-line file that produces 12 candidates has to say where the other 28
    went. Dropping them from the table makes that unanswerable."""
    assert len(driven["rows"]) == len(_PREVIEW["rows"])


def test_a_line_that_cannot_be_enrolled_has_no_checkbox_and_says_why(driven):
    loop = next(r for r in driven["rows"] if r["addr"] == "127.0.0.1")
    assert loop["hasCheckbox"] is False, "a loopback address is offered for enrollment"
    assert loop["note"] == "loopback address"
    managed = next(r for r in driven["rows"] if r["addr"] == "10.0.0.7")
    assert managed["hasCheckbox"] is False
    assert "already managed as 'legacy-01'" in managed["note"]


def test_the_enrollable_rows_arrive_ticked(driven):
    for addr in ("192.168.155.135", "10.0.0.5"):
        row = next(r for r in driven["rows"] if r["addr"] == addr)
        assert row["ticked"] is True, f"{addr} was not ticked"


def test_a_name_that_already_exists_is_offered_but_NOT_ticked(driven):
    """It may well be a host that moved, so it is not refused -- but ticked by
    default it would silently overwrite an existing record on a click aimed at
    the other eleven."""
    row = next(r for r in driven["rows"] if r["addr"] == "10.0.0.6")
    assert row["hasCheckbox"] is True, "a name conflict removed the row entirely"
    assert row["ticked"] is False, "a conflicting name was ticked by default"
    assert "already exists" in row["note"]


def test_the_name_can_be_edited_before_enrolling(driven):
    row = next(r for r in driven["rows"] if r["addr"] == "10.0.0.5")
    assert row["name"] == "db", f"the short name was not offered: {row['name']!r}"


def test_the_button_counts_what_is_ticked(driven):
    assert "3" in driven["enroll_label"], driven["enroll_label"]


# ---- what a click actually does -------------------------------------------
def test_it_asks_before_touching_anything(driven):
    assert driven["confirms"], "it enrolled without confirming"
    assert "3 host(s)" in driven["confirms"][0], driven["confirms"][0]


def test_exactly_the_ticked_hosts_are_posted(driven):
    """The one that matters. Not the skipped lines, not the unticked conflict."""
    enrolls = [c for c in driven["calls"] if c[0] == "enroll"]
    assert [(c[1], c[2]) for c in enrolls] == [
        ("ubuntu-lab", "192.168.155.135"), ("db", "10.0.0.5"),
        ("app-01", "10.0.0.8")], enrolls


def test_each_goes_through_the_ordinary_one_host_route(driven):
    """There is no bulk endpoint: the browser posts them one at a time, so the
    duplicate-IP check, host limit and superuser gate all still apply."""
    enrolls = [c for c in driven["calls"] if c[0] == "enroll"]
    assert len(enrolls) == 3
    assert all(c[3] == "root" for c in enrolls), "the batch SSH user was not used"


def test_one_failure_does_not_stop_the_rest(driven):
    """db is stubbed to refuse, and app-01 comes AFTER it. If the loop gave up
    on the first error, app-01 would never be posted."""
    enrolls = [c for c in driven["calls"] if c[0] == "enroll"]
    assert len(enrolls) == 3, "it stopped at the failure instead of carrying on"
    assert enrolls[-1][1] == "app-01", \
        f"the host after the failure was never attempted: {enrolls}"


def test_both_halves_of_the_outcome_are_reported(driven):
    """"Enrolled 1 of 2" with no word on the second is the report that sends
    someone hunting through the activity log."""
    body = driven["body"]
    assert "Enrolled 2 of 3" in body, body[:400]
    assert "SSH authentication failed" in body, "the failure is not named"
    assert "db" in body


def test_the_batch_password_does_not_stay_on_screen(driven):
    assert all(v == "" for v in driven["password_after"]), \
        "a password was left in a field after the run"
