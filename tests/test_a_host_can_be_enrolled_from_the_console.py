"""You could not type a hostname anywhere in the Controller.

Reported as "this doesn't make sense because there's no way to enter a
hostname", on the Host Enrollment page. It was not a missing field on one tab:
"Enroll a Host" rendered a `curl` one-liner to run ON the target, and that was
the only way in. Enrolment was entirely agent-pull, so you needed a shell on the
machine you were enrolling — which is the thing enrolment is meant to give you.

The galling part is that the push path was already built. `/remote/enroll-ssh`
has been in backend/remote_routes.py all along, with its own design note:
"enrollment is a one-time, automated handoff from password auth to key auth …
uses the host's password exactly once, in memory … the password itself is never
persisted anywhere". The BFF exposed it at /api/enroll-ssh. Nothing in the
console called either one.

So this file pins the WHOLE path — backend route, BFF route, API helper, form —
because any one of them going missing restores "there is nowhere to type a
hostname", and three of the four were already fine while the feature was
unreachable.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BACKEND = (REPO / "backend" / "remote_routes.py").read_text()
BFF = (REPO / "webgui" / "server.py").read_text()
APIJS = (REPO / "webgui" / "frontend" / "src" / "api.js").read_text()
VIEW = (REPO / "webgui" / "frontend" / "src" / "views" / "HostEnrollment.jsx").read_text()

FIELDS = ("name", "ip", "username", "password")


class TestTheBackendStillOffersPushEnrollment:
    def test_the_route_exists(self):
        assert '@router.post("/enroll-ssh"' in BACKEND

    def test_it_is_superuser_gated(self):
        m = re.search(r'@router\.post\("/enroll-ssh"[^)]*\)', BACKEND, re.S)
        assert m and "require_superuser" in m.group(0), (
            "push enrollment takes a password and installs a key — it cannot be "
            "reachable by a lesser role"
        )

    def test_the_password_is_not_persisted(self):
        """The module's own promise. A regression here is a credential at rest."""
        assert "never persisted" in BACKEND


class TestTheBffExposesIt:
    def test_the_route_exists(self):
        assert '@app.post("/api/enroll-ssh")' in BFF

    @pytest.mark.parametrize("f", FIELDS)
    def test_it_forwards_every_field_the_backend_needs(self, f):
        m = re.search(r'def enroll_ssh\(.*?return _superuser_request', BFF, re.S)
        assert m, "the BFF handler is gone"
        assert f'"{f}"' in m.group(0), f"{f} is no longer forwarded"

    def test_the_environment_is_optional(self):
        m = re.search(r'def enroll_ssh\(.*?return _superuser_request', BFF, re.S)
        assert "if body.environment" in m.group(0), \
            "an unassigned host can no longer be enrolled"


class TestTheConsoleCanActuallyReachIt:
    """The half that was missing. Everything above was already true while the
    feature was unusable."""

    def test_the_api_helper_exists(self):
        assert "enrollSsh:" in APIJS
        assert '"/api/enroll-ssh"' in APIJS

    def test_the_helper_allows_a_long_call(self):
        """It opens an SSH connection, writes authorized_keys and verifies the
        result. On the default timeout a slow host fails identically to a wrong
        password, which is the least debuggable error this page could give."""
        m = re.search(r"enrollSsh: \([^)]*\) => req\([^;]*?\)", APIJS, re.S)
        assert m, "the helper is not in the expected shape"
        t = re.search(r"timeout:\s*(\d+)", m.group(0))
        assert t and int(t.group(1)) >= 60000, "the enrollment call can time out too early"

    def test_the_form_exists_and_calls_the_helper(self):
        assert "api.enrollSsh(" in VIEW, "nothing in the console calls push enrollment"

    @pytest.mark.parametrize("f", FIELDS)
    def test_every_caller_sends_every_required_field(self, f):
        """EVERY call site, not the first one found.

        There is ONE now. It was two -- a single-host form and a bulk import
        from a hosts file, the same job described twice -- and a typed host is
        a one-line list, so they were merged into one. The count is asserted
        rather than tolerated: a second call site appearing means a second way
        in has been added back, and whoever adds it should have to say so
        here. Anchoring on `the first api.enrollSsh( in the file` was the old
        mistake, and it is a test about nothing in particular.
        """
        sites = re.findall(r"api\.enrollSsh\(\{(.*?)\}\)", VIEW, re.S)
        assert len(sites) == 1, (
            f"enrollment has one way in; found {len(sites)} call site(s)")
        for i, body in enumerate(sites):
            assert f"{f}:" in body, f"call site {i + 1} does not send {f}"

    def test_there_is_one_enroll_panel_and_it_has_both_steps(self):
        """The merge itself. Two panels side by side made an operator with one
        host to add decide which half of the page was for them, and both halves
        ended at the same call with the same credentials."""
        assert VIEW.count("<legend>Enroll hosts</legend>") == 1, \
            "there is no single Enroll hosts panel"
        for gone in ("Enroll a host from here", "Enroll several from a hosts file"):
            assert gone not in VIEW, f"the old separate panel {gone!r} is back"
        assert "Which hosts" in VIEW and "How to connect" in VIEW, \
            "the one panel no longer says which hosts, then how to connect"

    def test_there_is_somewhere_to_give_an_address(self):
        """The literal complaint that started this screen: a host whose name
        the Controller cannot resolve needs an address typed somewhere.

        That used to be a field of its own. It is a line in the list now --
        `192.168.155.135  ubuntu-lab`, exactly as /etc/hosts spells it -- so
        what is asserted is that the list SAYS so. An input whose format
        nobody can see is an input nobody can use, and removing the labelled
        field without showing the format would be the worse half of this
        change on its own."""
        # The ENROLL list's own placeholder, not "/etc/hosts appears
        # somewhere in the file" -- it appears in half the comments, so that
        # assertion passed with the example gone.
        m = re.search(r"value=\{bulkText\}(.*?)/>", VIEW, re.S)
        assert m, "the enroll list is gone or no longer in the expected shape"
        ph = re.search(r'placeholder=\{"(.*?)"\}', m.group(1), re.S)
        assert ph, f"the list offers no example of what to type: {m.group(1)[:200]}"
        example = ph.group(1)
        assert "/etc/hosts" in example, example
        assert re.search(r"\d+\.\d+\.\d+\.\d+", example), \
            f"no address-and-name example to copy: {example}"
        assert "push.name" not in VIEW and "push.ip" not in VIEW, \
            "the separate Host name / Address pair is back"

    def test_the_password_field_is_masked(self):
        m = re.search(r'value=\{bulkCreds\.password\}[^/]*', VIEW, re.S)
        assert m and 'type="password"' in VIEW[max(0, m.start() - 200):m.end()], \
            "the password is rendered in clear text"

    # The two code paths that enroll, by a stable anchor each rather than by
    # position in the file.
    # One code path that enrolls, by a stable anchor rather than by position
    # in the file. It was two.
    HANDLERS = {
        "the enrollment run": (r"async function enrollPicked\(\)", r"\n  \}"),
    }

    def _handler(self, which):
        start_re, end_re = self.HANDLERS[which]
        m = re.search(start_re, VIEW)
        assert m, f"{which} is gone, or no longer in the expected shape"
        rest = VIEW[m.start():]
        e = re.search(end_re, rest)
        assert e, f"could not find the end of {which}"
        return rest[:e.end()]

    @pytest.mark.parametrize("which", list(HANDLERS))
    def test_the_password_is_cleared_after_enrolling(self, which):
        """It has done its one job; leaving it on screen is leaving a credential
        on screen -- more so now that the one password is used against every
        machine in the list."""
        body = self._handler(which)
        assert re.search(r'password:\s*""', body), \
            f"the password survives enrollment in {which}"

    @pytest.mark.parametrize("which", list(HANDLERS))
    def test_the_host_list_refreshes_afterwards(self, which):
        """A host that enrolled but does not appear looks like a failure."""
        body = self._handler(which)
        assert "load();" in body, f"{which} does not reload the host list"

    def test_it_does_not_call_a_loader_that_does_not_exist(self):
        """The first version of this called loadAgents(), which this component
        has never defined. esbuild parses an undefined identifier happily, so it
        would have thrown at the one moment it must not: right after an
        enrollment SUCCEEDED, reporting a crash for a host that is now enrolled."""
        called = set(re.findall(r"\b(load[A-Za-z]*)\(\)", VIEW))
        defined = set(re.findall(r"(?:function|const)\s+(load[A-Za-z]*)\s*[=(]", VIEW))
        assert called <= defined, f"calls undefined loader(s): {sorted(called - defined)}"
