"""Enrolling one host and enrolling forty, driven through the real component.

There were two panels: "Enroll a host from here" and "Enroll several from a
hosts file". The same job described twice -- both ended at the same
api.enrollSsh with the same SSH account -- and an operator with one host to
add had to decide which half of the page was for them.

Now there is one. A typed host is written into the same list a pasted file
goes into, so there is one parser, one preview, one set of credentials and
one button. The risk in a merge like this is that the one-host case quietly
stops working while the bulk case, which has the tests, stays green. So this
drives the ONE-HOST path end to end in a real browser: type it, add it, see
the row, enrol it, and check exactly what was posted.

It bundles the ACTUAL HostEnrollment.jsx against a stand-in api, so a change
to the view is a change to what this drives.

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

SERVER_HOSTS = ("# the lab rack\n"
                "127.0.0.1\tlocalhost\n"
                "192.168.155.135\tubuntu-lab\n")

# The preview answers from the text it is given, the way the real route does,
# so "Add wrote the line" and "the preview saw it" are one assertion and not
# two things that can agree while both being wrong.
_STUB_API = """
const calls = []; window.__calls = calls;
const SERVER_HOSTS = __SERVER_HOSTS__;
function parse(text) {
  const rows = [];
  (text || "").split("\\n").forEach((raw, i) => {
    const body = raw.split("#")[0].trim();
    if (!body) return;
    const f = body.split(/\\s+/);
    const looksLikeAddr = /^[0-9.]+$|:/.test(f[0]);
    const address = looksLikeAddr ? f[0] : "";
    const names = looksLikeAddr ? f.slice(1) : f;
    const name = names[0] || "";
    const loop = address.startsWith("127.");
    rows.push({ line: i + 1, address, names, name,
                target: address || name,
                enrollable: !loop,
                skip_reason: loop ? "loopback address" : undefined });
  });
  return { rows };
}
const explicit = {
  hostsFilePreview: (t) => { calls.push(["preview", t]); return Promise.resolve(parse(t)); },
  serverHosts: () => { calls.push(["serverHosts"]);
    return Promise.resolve({ text: SERVER_HOSTS, path: "/host/etc/hosts",
                             present: true, live: true, stale_note: "",
                             in_container: true }); },
  enrollSsh: (b) => { calls.push(["enroll", b.name, b.ip, b.username, b.environment]);
                      return Promise.resolve({ detail: b.name + " enrolled." }); },
  environments: () => Promise.resolve(["prod", "lab"]),
  enrollmentPause: () => Promise.resolve({ paused: false }),
  portalStatus: () => Promise.resolve({}),
  names: () => Promise.resolve({ text: "", entries: [], sources: [
    { key: "own", path: "/data/name_table", label: "This Controller's own list",
      present: false, names: 0 },
    { key: "server", path: "/host/etc/hosts", label: "The server's /etc/hosts",
      present: true, names: 1, live: true, stale_note: "", in_container: true },
  ] }),
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

_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<title>Host Enrollment</title><link rel="stylesheet" href="./styles.css">'
         '</head><body><div id="root"></div><script src="./app.js"></script></body></html>')


@pytest.fixture(scope="module")
def page_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("one-enroll")
    src = d / "src"
    (src / "views").mkdir(parents=True)
    shutil.copy2(VIEW, src / "views" / "HostEnrollment.jsx")
    (src / "api.js").write_text(
        _STUB_API.replace("__SERVER_HOSTS__", json.dumps(SERVER_HOSTS)),
        encoding="utf-8")
    (src / "confirmDialog.js").write_text(_STUB_CONFIRM, encoding="utf-8")
    (src / "main.jsx").write_text(_MAIN, encoding="utf-8")
    (d / "index.html").write_text(_HTML, encoding="utf-8")
    shutil.copy2(FRONTEND / "src" / "styles.css", d / "styles.css")
    (d / "node_modules").symlink_to(NODE_MODULES)
    r = subprocess.run(
        [str(ESBUILD), str(src / "main.jsx"), "--bundle", "--loader:.jsx=jsx",
         "--jsx=automatic", "--format=iife", f"--outfile={d / 'app.js'}",
         "--log-level=error"],
        capture_output=True, text=True, cwd=str(FRONTEND))
    assert r.returncode == 0, f"the view does not bundle:\n{r.stderr}"
    return d


def _rows(pg):
    return pg.evaluate(
        """() => [...document.querySelectorAll('table tbody tr')].map(tr => {
             const cb = tr.querySelector('input[type=checkbox]');
             const nm = tr.querySelector('td:nth-child(3) input');
             return { ticked: cb ? cb.checked : null,
                      addr: tr.children[1].textContent.trim(),
                      name: nm ? nm.value : null };
           })""")


@pytest.fixture(scope="module")
def one_host(page_dir):
    """Type ONE host, add it, enrol it. What the screen did, start to finish."""
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        pg = b.new_page(viewport={"width": 1180, "height": 1200})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"file://{page_dir}/index.html", wait_until="load")
        pg.wait_for_timeout(400)
        pg.get_by_role("button", name="Enroll a Host", exact=True).click()
        pg.wait_for_timeout(300)

        out["panels"] = pg.locator("legend").all_text_contents()
        out["passwords_before"] = pg.locator("input[type=password]").count()

        main = pg.locator(".enroll-main")
        main.get_by_label("Host name").fill("ubuntu-lab")
        main.get_by_label("Address", exact=False).first.fill("192.168.155.135")
        main.get_by_role("button", name="Add", exact=True).click()
        pg.wait_for_timeout(400)

        out["rows"] = _rows(pg)
        out["textarea"] = pg.locator("textarea").first.input_value()
        out["name_field_after_add"] = main.get_by_label("Host name").input_value()
        out["enroll_label"] = main.get_by_role(
            "button", name="Enroll", exact=False).last.text_content()
        # Two buttons called "Add" on one screen -- one adds a host to enrol,
        # one adds a name to the lookup table -- was itself the ambiguity this
        # merge was meant to remove. The aside's now says "Add name".
        out["add_buttons"] = pg.get_by_role("button", name="Add", exact=True).count()

        main.get_by_label("SSH user").fill("ops")
        main.locator("input[type=password]").last.fill("hunter2")
        main.get_by_role("button", name="Enroll", exact=False).last.click()
        pg.wait_for_timeout(1200)

        out["calls"] = pg.evaluate("() => window.__calls || []")
        out["body"] = pg.locator("body").text_content()
        out["errors"] = errors
        b.close()
    return out


@pytest.fixture(scope="module")
def from_server(page_dir):
    """The other way in: take the list off the server."""
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        pg = b.new_page(viewport={"width": 1180, "height": 1200})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"file://{page_dir}/index.html", wait_until="load")
        pg.wait_for_timeout(400)
        pg.get_by_role("button", name="Enroll a Host", exact=True).click()
        pg.wait_for_timeout(300)
        pg.get_by_role("button", name="Use the server's hosts file").click()
        pg.wait_for_timeout(600)
        out["textarea"] = pg.locator("textarea").first.input_value()
        out["rows"] = _rows(pg)
        out["calls"] = pg.evaluate("() => window.__calls || []")
        out["errors"] = errors
        b.close()
    return out


# ---- there is one panel ----------------------------------------------------
def test_the_component_renders_without_throwing(one_host):
    assert one_host["errors"] == [], one_host["errors"]


def test_the_enroll_tab_has_a_single_enroll_panel(one_host):
    enroll = [p for p in one_host["panels"] if p.lower().startswith("enroll")]
    assert enroll == ["Enroll hosts"], enroll


def test_only_one_button_on_the_screen_is_called_add(one_host):
    assert one_host["add_buttons"] == 1, (
        "two buttons called Add on one screen, doing different things")


def test_there_is_one_set_of_credentials(one_host):
    """Two panels meant two SSH accounts and two password boxes on one screen,
    and an operator filling the wrong one got an authentication failure for a
    password they had typed correctly."""
    assert one_host["passwords_before"] == 1, (
        f"{one_host['passwords_before']} password fields on the enroll tab")


# ---- the one-host path -----------------------------------------------------
def test_adding_one_host_writes_it_into_the_list(one_host):
    assert one_host["textarea"].strip() == "192.168.155.135  ubuntu-lab"


def test_adding_one_host_shows_it_as_a_row_already_ticked(one_host):
    assert one_host["rows"] == [
        {"ticked": True, "addr": "192.168.155.135", "name": "ubuntu-lab"}]


def test_the_add_fields_clear_so_the_next_host_can_be_typed(one_host):
    assert one_host["name_field_after_add"] == ""


def test_the_button_names_the_single_host_rather_than_counting_it(one_host):
    """"Enroll 1 hosts" is the tell that a bulk screen was pointed at one
    machine. It should read like what it is about to do."""
    assert one_host["enroll_label"].strip() == "Enroll ubuntu-lab"


def test_enrolling_posts_that_host_with_those_credentials(one_host):
    enrolls = [c for c in one_host["calls"] if c[0] == "enroll"]
    assert enrolls == [["enroll", "ubuntu-lab", "192.168.155.135", "ops", ""]]


def test_the_screen_says_it_worked(one_host):
    assert "Enrolled 1 of 1" in one_host["body"], one_host["body"][:400]


# ---- the list-off-the-server path ------------------------------------------
def test_the_server_hosts_button_reads_the_server(from_server):
    assert from_server["errors"] == [], from_server["errors"]
    assert ["serverHosts"] in from_server["calls"]
    assert "ubuntu-lab" in from_server["textarea"]
    assert "# the lab rack" in from_server["textarea"], \
        "the comments were dropped on the way in"


def test_the_server_list_is_previewed_without_a_second_click(from_server):
    """It was fetched and parsed in one go; making the operator then press
    "Read the list" on a list the Controller just read is a step for nobody."""
    names = [r["name"] for r in from_server["rows"]]
    assert "ubuntu-lab" in names, from_server["rows"]


def test_loopback_from_the_server_file_is_not_offered(from_server):
    """127.0.0.1 localhost is in every hosts file and is never a host to
    manage -- and it resolves to this container."""
    row = next(r for r in from_server["rows"] if r["addr"] == "127.0.0.1")
    assert row["ticked"] is None, "localhost arrived with a checkbox"
