"""One unvalidated interpolation must not become "reach any controller endpoint".

client/api.py builds nearly every path with an f-string around a value that came
from a browser — a host id, an admin name, a task id. `requests` resolves dot
segments before it sends, so a single missed validation anywhere in that module
is a way to call a DIFFERENT controller endpoint with the caller's credentials
attached. Rather than audit forty call sites forever, the path is checked once,
at the door.
"""
import pytest

import client.api as capi


def test_requests_really_does_collapse_traversal():
    """The premise. If this ever stops being true the guard is still correct, but
    the reason for it should be recorded rather than assumed."""
    from requests.models import PreparedRequest
    r = PreparedRequest()
    r.prepare_url("https://c:9000/agents/../admin/x", None)
    assert r.url == "https://c:9000/admin/x"


@pytest.mark.parametrize("path", [
    "/agents/config-poll-times",
    "/agents/host-1/tasks",
    "/agents/host-1/config-restores/12/payload",
    "/admin/administrators/alice/password",
    "/activity?limit=200&since_id=0",
    "/hosts/",
])
def test_the_real_paths_are_all_accepted(path):
    assert capi._safe_path(path) == path


@pytest.mark.parametrize("path", [
    "/agents/../admin/sso-provision",
    "/agents/a/../../admin/administrators",
    "//evil.example/admin",
    "agents/host-1/tasks",                 # not absolute
    "/agents/host\n-1/tasks",              # header/path injection attempt
    "/agents/host\x00/tasks",
    "/agents/./tasks",
    "/agents/x/../y",
])
def test_a_path_that_could_land_somewhere_else_is_refused(path):
    with pytest.raises(ValueError):
        capi._safe_path(path)


def test_the_query_string_is_not_second_guessed():
    """Only the PATH is normalized — a legitimate query may contain anything,
    including characters that would look like traversal."""
    p = "/search?q=../../etc/passwd&limit=5"
    assert capi._safe_path(p) == p


def test_the_guard_is_actually_wired_into_the_request_path():
    """A helper nothing calls protects nothing."""
    import inspect
    assert "_safe_path(path)" in inspect.getsource(capi._request)
    assert "_safe_path(path)" in inspect.getsource(capi._download_binary)
