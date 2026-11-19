"""An enrollment that fails has to leave something to read.

Asked for: "there should be some kind of logging to see if there are errors
here when trying to enroll."

The controller's reasons were already specific -- "SSH authentication failed",
"Could not reach host: ...", a host-key mismatch with the path to the pinned
entry -- and they had nowhere to be read. A run reported a count ("Enrolled 11
of 12") and put every error into one red box, which the next click replaced.
Nothing was written down anywhere: the one thing an operator looks at after a
failed enrollment did not outlive the tab.

Two records now, for the two different questions.

  The RUN LOG, in the console, written as the run goes: one line per host,
  what it is doing, and the controller's own reason when it fails. A 40-host
  run can be watched instead of waited out, and the failure is still on screen
  afterwards, next to a Copy button.

  The ACTIVITY LOG, on the controller: every attempt, succeeded or failed,
  with the account that made it. That is the copy that is still there
  tomorrow, and the one that says who.

Run: pytest tests/
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = REPO / "webgui" / "frontend"
VIEW = FRONTEND / "src" / "views" / "HostEnrollment.jsx"
ESBUILD = FRONTEND / "node_modules" / ".bin" / "esbuild"
NODE_MODULES = FRONTEND / "node_modules"
VIEW_SRC = VIEW.read_text(encoding="utf-8")

_CHROMIUM = ("/opt/pw-browsers/chromium", "/usr/bin/chromium",
             "/usr/bin/chromium-browser", "/usr/bin/google-chrome")


def _chromium():
    for c in _CHROMIUM:
        if pathlib.Path(c).is_file():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def _playwright():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


# ---- the controller writes down every attempt ------------------------------
def _attempt(monkeypatch, *, fail_with=None, token=None):
    """Drive the real route and return the activity rows it wrote."""
    import backend.remote_routes as rr
    from backend.models.remote_models import EnrollSSHRequest
    from backend import db

    rows = []
    monkeypatch.setattr(db, "log_activity",
                        lambda *a, **k: rows.append(a), raising=False)
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)

    def inner(body):
        if fail_with:
            raise rr.HTTPException(status_code=400, detail=fail_with)
        return {"enrolled": True, "host": body.name}
    monkeypatch.setattr(rr, "_enroll_ssh", inner)

    class Req:
        headers = {"X-Sysible-Admin-Token": token} if token else {}

    body = EnrollSSHRequest(name="ubuntu-lab", ip="192.168.155.135",
                            username="ops", password="x")
    try:
        rr.enroll_ssh(body, Req())
    except Exception:
        pass
    return rows


def test_a_failed_enrollment_is_written_down(monkeypatch):
    rows = _attempt(monkeypatch, fail_with="SSH authentication failed")
    assert rows, "a failed enrollment left no trace anywhere"
    joined = " ".join(str(x) for x in rows[0])
    assert "failed" in joined and "SSH authentication failed" in joined, joined


def test_a_successful_enrollment_is_written_down_too(monkeypatch):
    rows = _attempt(monkeypatch)
    assert rows, "nothing records that the host WAS enrolled"
    assert "succeeded" in " ".join(str(x) for x in rows[0])


def test_the_row_names_the_host_and_the_login(monkeypatch):
    rows = _attempt(monkeypatch, fail_with="Could not reach host: timed out")
    joined = " ".join(str(x) for x in rows[0])
    assert "ubuntu-lab" in joined
    assert "ops@192.168.155.135" in joined, joined


def test_an_unknown_failure_is_recorded_as_well(monkeypatch):
    """The route can raise eight different HTTPExceptions and, in principle,
    something nobody wrote a line for. The wrapper exists so that none of them
    is the one that goes unlogged."""
    import backend.remote_routes as rr
    from backend.models.remote_models import EnrollSSHRequest
    from backend import db
    rows = []
    monkeypatch.setattr(db, "log_activity", lambda *a, **k: rows.append(a), raising=False)
    monkeypatch.setattr(rr, "_enroll_ssh",
                        lambda body: (_ for _ in ()).throw(RuntimeError("boom")))
    class Req:
        headers = {}
    with pytest.raises(RuntimeError):
        rr.enroll_ssh(EnrollSSHRequest(name="x", password="p"), Req())
    assert rows and "RuntimeError: boom" in " ".join(str(x) for x in rows[0])


def test_a_broken_audit_log_does_not_break_enrolling(monkeypatch):
    """A controller that cannot write the row must not turn a successful
    enrollment into a failure, nor a failed one into a 500 that hides the real
    reason."""
    import backend.remote_routes as rr
    from backend.models.remote_models import EnrollSSHRequest
    from backend import db
    monkeypatch.setattr(db, "log_activity",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(rr, "_enroll_ssh", lambda body: {"enrolled": True})
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    class Req:
        headers = {}
    assert rr.enroll_ssh(EnrollSSHRequest(name="x", password="p"), Req()) == {"enrolled": True}


def test_an_unattributable_attempt_is_still_attributed_to_someone(monkeypatch):
    rows = _attempt(monkeypatch, fail_with="nope")
    assert rows[0][0], "the row has no actor at all"


# ---- and the console shows it while it happens -----------------------------
pytestmark_browser = [
    pytest.mark.skipif(not ESBUILD.is_file(), reason="esbuild is not installed"),
    pytest.mark.skipif(not _playwright(), reason="playwright is not installed"),
    pytest.mark.skipif(not _chromium(), reason="no chromium to drive"),
]

_STUB_API = """
const calls = []; window.__calls = calls;
function parse(text) {
  const rows = [];
  (text || "").split("\\n").forEach((raw, i) => {
    const body = raw.split("#")[0].trim();
    if (!body) return;
    const f = body.split(/\\s+/);
    const address = /^[0-9.]+$|:/.test(f[0]) ? f[0] : "";
    const names = address ? f.slice(1) : f;
    rows.push({line: i + 1, address, names, name: names[0] || "",
               target: address || names[0] || "", enrollable: true});
  });
  return {rows};
}
const explicit = {
  hostsFilePreview: (t) => Promise.resolve(parse(t)),
  enrollSsh: (b) => {
    calls.push(["enroll", b.name]);
    // The second host refuses the password, the way a real one does.
    if (b.name === "db-01") return Promise.reject(new Error("SSH authentication failed"));
    return Promise.resolve({ detail: b.name + " enrolled." });
  },
  environments: () => Promise.resolve(["prod"]),
  enrollmentPause: () => Promise.resolve({ paused: false }),
  portalStatus: () => Promise.resolve({}),
  hosts: () => Promise.resolve({ hosts: [] }),
  names: () => Promise.resolve({ text: "", entries: [], sources: [] }),
};
const api = new Proxy(explicit, {
  get(t, k) { return (k in t) ? t[k] : (typeof k === "string" ? () => Promise.resolve([]) : undefined); },
});
export default api; export { api };
"""

_STUB_CONFIRM = """
export function confirmDialog(m) {
  (window.__confirms = window.__confirms || []).push(m);
  return Promise.resolve(true);
}
"""

_MAIN = """
import React from "react";
import { createRoot } from "react-dom/client";
import HostEnrollment from "./views/HostEnrollment.jsx";
createRoot(document.getElementById("root")).render(<HostEnrollment />);
"""

_HTML = ('<!doctype html><html lang="en" data-skin="brand"><head><meta charset="utf-8">'
         '<title>Host Enrollment</title><link rel="stylesheet" href="./styles.css">'
         '</head><body><div id="root"></div><script src="./app.js"></script></body></html>')


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    if not (ESBUILD.is_file() and _playwright() and _chromium()):
        pytest.skip("needs esbuild, playwright and chromium")
    d = tmp_path_factory.mktemp("runlog")
    src = d / "src"
    (src / "views").mkdir(parents=True)
    shutil.copy2(VIEW, src / "views" / "HostEnrollment.jsx")
    (src / "api.js").write_text(_STUB_API, encoding="utf-8")
    (src / "confirmDialog.js").write_text(_STUB_CONFIRM, encoding="utf-8")
    (src / "main.jsx").write_text(_MAIN, encoding="utf-8")
    (d / "index.html").write_text(_HTML, encoding="utf-8")
    shutil.copy2(FRONTEND / "src" / "styles.css", d / "styles.css")
    (d / "node_modules").symlink_to(NODE_MODULES)
    r = subprocess.run(
        [str(ESBUILD), str(src / "main.jsx"), "--bundle", "--loader:.jsx=jsx",
         "--jsx=automatic", "--format=iife", f"--outfile={d / 'app.js'}",
         "--log-level=error"], capture_output=True, text=True, cwd=str(FRONTEND))
    assert r.returncode == 0, r.stderr

    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        pg = b.new_page(viewport={"width": 1180, "height": 1200})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"file://{d}/index.html", wait_until="load")
        pg.wait_for_timeout(400)
        pg.get_by_role("button", name="Enroll a Host", exact=True).click()
        pg.wait_for_timeout(300)
        main = pg.locator(".enroll-main")
        out["panels_open_before"] = pg.locator("legend").all_text_contents()
        main.locator("fieldset").first.locator("textarea").fill(
            "10.0.0.11  web-01\n10.0.0.12  db-01")
        main.get_by_role("button", name="Read the list").click()
        pg.wait_for_timeout(400)
        main.get_by_label("SSH user").fill("ops")
        main.locator("input[type=password]").last.fill("hunter2")
        main.get_by_role("button", name="Enroll", exact=False).last.click()
        pg.wait_for_timeout(1500)
        out["log"] = pg.evaluate(
            """() => [...document.querySelectorAll('.run-log tbody tr')].map(tr => ({
                 dot: (tr.querySelector('.dot')||{}).className || '',
                 cells: [...tr.querySelectorAll('td')].map(td => td.textContent.trim()),
               }))""")
        out["heading"] = pg.locator(".run-log").first.text_content()
        out["body"] = pg.locator("body").text_content()
        out["errors"] = errs
        b.close()
    return out


@pytest.mark.parametrize("_", [0])
def test_the_run_leaves_a_line_per_host(ran, _):
    assert ran["errors"] == [], ran["errors"]
    names = [c["cells"][1] for c in ran["log"]]
    assert names == ["web-01", "db-01"], names


def test_the_failure_carries_the_controllers_own_reason(ran):
    row = next(c for c in ran["log"] if c["cells"][1] == "db-01")
    assert "SSH authentication failed" in " ".join(row["cells"]), row


def test_the_two_outcomes_look_different(ran):
    ok = next(c for c in ran["log"] if c["cells"][1] == "web-01")
    bad = next(c for c in ran["log"] if c["cells"][1] == "db-01")
    assert "ok" in ok["dot"] and "bad" not in ok["dot"], ok["dot"]
    assert "bad" in bad["dot"], bad["dot"]


def test_the_log_counts_both(ran):
    assert "1 enrolled" in ran["heading"] and "1 failed" in ran["heading"], ran["heading"]


def test_the_error_banner_points_at_the_log_instead_of_repeating_it(ran):
    """One red box holding every reason at once is what this replaces."""
    assert "run log below" in ran["body"], ran["body"][:400]


def test_the_log_can_be_copied_and_cleared(ran):
    assert "Copy" in ran["body"] and "Clear" in ran["body"]


def test_the_other_ways_in_are_folded_away(ran):
    """Three always-open reference panels under the form made the page four
    screens long. Folded, not deleted: a host with no inbound SSH still needs
    the bundle."""
    legends = ran["panels_open_before"]
    assert "Other ways to enroll" in legends, legends
    for ref in ("Download the agent bundle", "Headless install (curl one-liner)",
                "Mass enrollment (Ansible ad-hoc)"):
        assert ref in VIEW_SRC, f"{ref} was deleted rather than folded"


def test_the_fold_is_shut_by_default():
    m = re.search(r"useState\(false\);\s*\n\s*const \[copiedLog", VIEW_SRC)
    assert "otherWaysOpen, setOtherWaysOpen] = useState(false)" in VIEW_SRC, \
        "the reference panels are open again on every visit"
