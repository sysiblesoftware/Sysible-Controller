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
    def test_the_form_sends_every_required_field(self, f):
        m = re.search(r"api\.enrollSsh\(\{(.*?)\}\)", VIEW, re.S)
        assert m, "the enrollSsh call is not in the expected shape"
        assert f"{f}:" in m.group(1), f"the form does not send {f}"

    def test_there_is_somewhere_to_type_an_address(self):
        """The literal complaint."""
        assert "push.ip" in VIEW and "push.name" in VIEW

    def test_the_password_field_is_masked(self):
        m = re.search(r'value=\{push\.password\}[^/]*', VIEW, re.S)
        assert m and 'type="password"' in VIEW[max(0, m.start() - 200):m.end()], \
            "the password is rendered in clear text"

    def test_the_password_is_cleared_after_a_successful_enrollment(self):
        """It has done its one job; leaving it on screen is leaving a credential
        on screen."""
        m = re.search(r"api\.enrollSsh\(\{.*?\}\);(.*?)\} catch", VIEW, re.S)
        assert m, "the success branch is not in the expected shape"
        assert re.search(r'password:\s*""', m.group(1)), \
            "the password field survives a successful enrollment"

    def test_the_host_list_refreshes_afterwards(self):
        """A host that enrolled but does not appear looks like a failure."""
        m = re.search(r"api\.enrollSsh\(\{.*?\}\);(.*?)\} catch", VIEW, re.S)
        assert "load();" in m.group(1), "the page does not reload its hosts"

    def test_it_does_not_call_a_loader_that_does_not_exist(self):
        """The first version of this called loadAgents(), which this component
        has never defined. esbuild parses an undefined identifier happily, so it
        would have thrown at the one moment it must not: right after an
        enrollment SUCCEEDED, reporting a crash for a host that is now enrolled."""
        called = set(re.findall(r"\b(load[A-Za-z]*)\(\)", VIEW))
        defined = set(re.findall(r"(?:function|const)\s+(load[A-Za-z]*)\s*[=(]", VIEW))
        assert called <= defined, f"calls undefined loader(s): {sorted(called - defined)}"
