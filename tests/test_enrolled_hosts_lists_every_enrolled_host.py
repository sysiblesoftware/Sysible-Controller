"""The Enrolled Hosts tab has to list the hosts the Controller thinks it has.

Reported: "ubuntu-lab is showing up in the network topology but isn't
registered as a controller enrolled host."

Both were right, about different lists. The Controller keeps two: agent
enrollments, and standalone SSH host records. This screen rendered /agents
alone. The Visualizer's map, the host picker, Flashback's host list and the
enrollment preview's duplicate check all read /api/hosts -- the Controller's
own MERGE of the two, which marks each entry agent / ssh / merged.

So a host enrolled over SSH whose agent never reported was absent from the one
page whose job is to list enrolled hosts, drawn on the map, counted by
Flashback, and refused by the enroll form as "already managed". Three screens,
three answers, and no way to reconcile them from any of the three.

It reads the merge now and shows the SSH-only ones for what they are. It does
NOT re-derive the match here: a fourth implementation of "is this the same
box" would eventually be a fourth answer.

Driven through the real component in a real browser.

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

# One agent-only host, one SSH-only host, and one box that is BOTH -- which the
# Controller's merge reports once, as "merged", and which must therefore not
# appear twice here.
AGENTS = [
    {"host_id": "h-web", "hostname": "web-01", "ip": "10.0.0.11",
     "environment": "prod", "online": True, "last_seen": 1700000000},
    {"host_id": "h-db", "hostname": "db-01", "ip": "10.0.0.12",
     "environment": "prod", "online": False},
]
MERGED_HOSTS = [
    {"kind": "agent", "id": "h-web", "label": "web-01", "address": "10.0.0.11",
     "environment": "prod", "has_agent": True},
    {"kind": "merged", "id": "h-db", "label": "db-01", "address": "10.0.0.12",
     "environment": "prod", "has_agent": True},
    {"kind": "ssh", "id": "ubuntu-lab", "label": "ubuntu-lab",
     "address": "192.168.155.135", "environment": "", "has_agent": False},
]

_STUB_API = """
const calls = []; window.__calls = calls;
const AGENTS = __AGENTS__;
const HOSTS = __HOSTS__;
const explicit = {
  agents: () => Promise.resolve({ agents: AGENTS }),
  hosts: () => { calls.push(["hosts"]); return Promise.resolve({ hosts: HOSTS }); },
  forgetHost: (n) => { calls.push(["forget", n]); return Promise.resolve({ ok: 1 }); },
  environments: () => Promise.resolve(["prod", "lab"]),
  enrollmentPause: () => Promise.resolve({ paused: false }),
  portalStatus: () => Promise.resolve({}),
  edition: () => Promise.resolve({}),
};
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

_HTML = ('<!doctype html><html lang="en" data-skin="brand"><head><meta charset="utf-8">'
         '<title>Host Enrollment</title><link rel="stylesheet" href="./styles.css">'
         '</head><body><div id="root"></div><script src="./app.js"></script></body></html>')


def _build(tmp, agents, hosts):
    src = tmp / "src"
    (src / "views").mkdir(parents=True)
    shutil.copy2(VIEW, src / "views" / "HostEnrollment.jsx")
    (src / "api.js").write_text(
        _STUB_API.replace("__AGENTS__", json.dumps(agents))
                 .replace("__HOSTS__", json.dumps(hosts)), encoding="utf-8")
    (src / "confirmDialog.js").write_text(_STUB_CONFIRM, encoding="utf-8")
    (src / "main.jsx").write_text(_MAIN, encoding="utf-8")
    (tmp / "index.html").write_text(_HTML, encoding="utf-8")
    shutil.copy2(FRONTEND / "src" / "styles.css", tmp / "styles.css")
    (tmp / "node_modules").symlink_to(NODE_MODULES)
    r = subprocess.run(
        [str(ESBUILD), str(src / "main.jsx"), "--bundle", "--loader:.jsx=jsx",
         "--jsx=automatic", "--format=iife", f"--outfile={tmp / 'app.js'}",
         "--log-level=error"],
        capture_output=True, text=True, cwd=str(FRONTEND))
    assert r.returncode == 0, f"the view does not bundle:\n{r.stderr}"
    return tmp


def _drive(page_dir, after=None):
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        pg = b.new_page(viewport={"width": 1180, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"file://{page_dir}/index.html", wait_until="load")
        pg.wait_for_timeout(600)
        if after:
            after(pg)
            pg.wait_for_timeout(500)
        out["rows"] = pg.evaluate(
            """() => [...document.querySelectorAll('.he-host')].map(r => ({
                 name: (r.querySelector('.he-host-name')||{}).textContent || '',
                 meta: (r.querySelector('.he-host-meta')||{}).textContent || '',
                 hasCheckbox: !!r.querySelector('input[type=checkbox]'),
                 dot: (r.querySelector('.dot')||{}).className || '',
                 buttons: [...r.querySelectorAll('button')].map(b => b.textContent.trim()),
               }))""")
        out["body"] = pg.locator("body").text_content()
        out["calls"] = pg.evaluate("() => window.__calls || []")
        out["confirms"] = pg.evaluate("() => window.__confirms || []")
        out["checked"] = pg.evaluate(
            """() => [...document.querySelectorAll('.he-host input[type=checkbox]')]
                      .filter(c => c.checked).length""")
        out["errors"] = errors
        b.close()
    return out


@pytest.fixture(scope="module")
def shown(tmp_path_factory):
    return _drive(_build(tmp_path_factory.mktemp("enrolled"), AGENTS, MERGED_HOSTS))


# ---- the host that was missing ---------------------------------------------
def test_the_component_renders_without_throwing(shown):
    assert shown["errors"] == [], shown["errors"]


def test_the_console_asks_for_the_merged_list(shown):
    assert ["hosts"] in shown["calls"], shown["calls"]


def test_an_ssh_only_host_is_listed(shown):
    """The literal report. It was on the map and not on this page."""
    names = [r["name"] for r in shown["rows"]]
    assert any("ubuntu-lab" in n for n in names), names


def test_every_host_is_listed_once(shown):
    """A box with BOTH an agent and an SSH record is one host. The Controller's
    merge already calls that "merged"; re-deriving the match here is how it
    would come back as two rows."""
    names = [re.sub(r"\s+", " ", r["name"]).strip() for r in shown["rows"]]
    labels = [n.split(" ")[0] for n in names]
    assert sorted(labels) == ["db-01", "ubuntu-lab", "web-01"], labels


def test_the_ssh_only_host_says_what_it_is(shown):
    row = next(r for r in shown["rows"] if "ubuntu-lab" in r["name"])
    assert "no agent" in row["name"], row["name"]
    assert "SSH only" in row["meta"], row["meta"]


def test_it_is_not_drawn_as_a_failure(shown):
    """Nothing has failed; something has not arrived. A red dot next to a host
    that is working perfectly over SSH is a page that cries wolf."""
    row = next(r for r in shown["rows"] if "ubuntu-lab" in r["name"])
    assert "warn" in row["dot"] and "bad" not in row["dot"], row["dot"]


# ---- what it may and may not be asked to do --------------------------------
def test_an_ssh_only_host_offers_none_of_the_agent_actions(shown):
    """Revoke, Reissue, Resume and Restore are all agent routes. `!a.revoked`
    is true for a row that has no agent at all, so they all rendered."""
    row = next(r for r in shown["rows"] if "ubuntu-lab" in r["name"])
    for gone in ("Revoke", "Reissue", "Resume", "Restore"):
        assert gone not in row["buttons"], row["buttons"]


def test_an_ssh_only_host_can_be_forgotten(shown):
    row = next(r for r in shown["rows"] if "ubuntu-lab" in r["name"])
    assert "Forget" in row["buttons"], row["buttons"]


def test_an_ssh_only_host_has_no_checkbox(shown):
    """Every bulk action under the list posts to an agent route with a
    host_id. Ticking this row would send it "ssh:ubuntu-lab"."""
    row = next(r for r in shown["rows"] if "ubuntu-lab" in r["name"])
    assert row["hasCheckbox"] is False, row


def test_the_agent_rows_are_untouched(shown):
    row = next(r for r in shown["rows"] if "web-01" in r["name"])
    assert row["hasCheckbox"] is True
    assert "Revoke" in row["buttons"] and "Reissue" in row["buttons"]
    assert "Forget" not in row["buttons"]


def test_select_all_does_not_reach_the_ssh_rows(tmp_path_factory):
    out = _drive(_build(tmp_path_factory.mktemp("select"), AGENTS, MERGED_HOSTS),
                 after=lambda pg: pg.get_by_role("button", name="Select All", exact=True).click())
    assert out["checked"] == 2, f"{out['checked']} checked; only the two agents should be"


def test_forgetting_calls_the_single_host_route(tmp_path_factory):
    """Not the bulk DELETE. Until this screen called it, the only way to clear
    one stale record was the route that forgets every one of them."""
    def click_forget(pg):
        # By text, not by role-name. Chromium computes a different accessible
        # name for this one button than for its neighbours of identical shape,
        # and chasing that is not what this test is about: it is about which
        # route the click posts to.
        pg.get_by_text("Forget", exact=True).click()
    out = _drive(_build(tmp_path_factory.mktemp("forget"), AGENTS, MERGED_HOSTS),
                 after=click_forget)
    assert ["forget", "ubuntu-lab"] in out["calls"], out["calls"]
    assert out["confirms"], "it removed a record without asking"
    assert "Nothing on the host itself is touched" in out["confirms"][0]


# ---- the empty state still means empty -------------------------------------
def test_no_hosts_at_all_still_says_so(tmp_path_factory):
    out = _drive(_build(tmp_path_factory.mktemp("none"), [], []))
    assert "No hosts enrolled yet" in out["body"]


def test_ssh_only_hosts_are_not_called_no_hosts(tmp_path_factory):
    """The empty state used to be `agents.length === 0`, so a Controller whose
    every host was SSH-only told the operator it had none -- while listing
    them."""
    out = _drive(_build(tmp_path_factory.mktemp("sshonly"), [],
                        [MERGED_HOSTS[2]]))
    assert "No hosts enrolled yet" not in out["body"], out["body"][:300]
    assert any("ubuntu-lab" in r["name"] for r in out["rows"]), out["rows"]


# ---- disenroll has to undo what enroll did ---------------------------------
#
# Reported: "I manually enrolled ubuntu then disenrolled it. When I go back
# it's still stuck in the controller db or something?" -- and the enroll
# screen answered "already managed as 'ubuntu'" for the same address.
#
# Enrolling over SSH writes TWO records: the SSH host record, under the name
# the operator typed, and the agent. Disenroll removed one. The controller's
# own cleanup forgets an SSH record only when the agent's hostname AND ip both
# match it, which is right for the auto-created shadow it was written for and
# never matches a record named by hand -- a box enrolled as "ubuntu" whose
# agent calls itself "ubuntu-lab" misses on the hostname every time.

DISENROLL_AGENTS = [
    {"host_id": "h-ubu", "hostname": "ubuntu-lab", "ip": "192.168.155.135",
     "environment": "", "online": True},
]
DISENROLL_HOSTS = [
    {"kind": "agent", "id": "h-ubu", "label": "ubuntu-lab",
     "address": "192.168.155.135", "environment": "", "has_agent": True},
    # The operator's own SSH record, under the name THEY typed, at the same
    # address. A different name, so hostname-matching cannot find it.
    {"kind": "ssh", "id": "ubuntu", "label": "ubuntu",
     "address": "192.168.155.135", "environment": "", "has_agent": False},
    # A different machine, which must not be touched.
    {"kind": "ssh", "id": "other-box", "label": "other-box",
     "address": "10.0.0.77", "environment": "", "has_agent": False},
]

_DISENROLL_STUB = _STUB_API.replace(
    "  forgetHost: (n) =>",
    "  removeHost: (id) => { calls.push([\"removeHost\", id]); return Promise.resolve({}); },\n"
    "  forgetHost: (n) =>")


@pytest.fixture(scope="module")
def disenrolled(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("disenroll")
    global _STUB_API
    keep, _STUB_API = _STUB_API, _DISENROLL_STUB
    try:
        page = _build(tmp, DISENROLL_AGENTS, DISENROLL_HOSTS)
    finally:
        _STUB_API = keep

    def act(pg):
        pg.locator(".he-host input[type=checkbox]").first.check()
        pg.get_by_role("button", name="Disenroll Host(s)").click()

    return _drive(page, after=act)


def test_disenrolling_removes_the_agent(disenrolled):
    assert ["removeHost", "h-ubu"] in disenrolled["calls"], disenrolled["calls"]


def test_disenrolling_also_forgets_the_ssh_record_at_that_address(disenrolled):
    """The record that was blocking re-enrollment. Matched on ADDRESS against
    the Controller's merged list -- not on the agent's idea of its own name,
    which is the match that could never succeed here."""
    assert ["forget", "ubuntu"] in disenrolled["calls"], disenrolled["calls"]


def test_a_different_machine_is_left_alone(disenrolled):
    forgotten = [c[1] for c in disenrolled["calls"] if c[0] == "forget"]
    assert "other-box" not in forgotten, forgotten


def test_the_confirm_says_the_ssh_record_goes_too(disenrolled):
    """Removing a connection record an operator did not ask about, without
    saying so, is the mirror of the bug."""
    assert disenrolled["confirms"], "it disenrolled without asking"
    msg = disenrolled["confirms"][0]
    assert "ubuntu" in msg and "enrolled again" in msg, msg
