"""Enrolling a host over SSH puts the AGENT on it, not just a key.

This is the defect the whole week ran on. The console's tab is "Enroll a
Host" and its first line promises the Controller "installs its own key and
enrols the agent". The route did the first half. Its own header still says so:

    CONNECT HOST VIA SSH PASSWORD (route kept as /enroll-ssh for
    compatibility - the GUI calls this "Connect Host", reserving "enroll"
    for the token-based host_agent flow)

So one click produced a host record, an authorized_keys line, and a machine
with no agent -- no health, no terminal, no updates, no config backup. The
console listed it as "SSH only - no agent", which reads as the agent failing
rather than as nothing ever having tried to install it, and every attempt to
chase the agent's own behaviour was chasing a symptom.

The bundle pushed here is the SAME one the download button mints: one
function, one single-use token, one host-cap check. It rides the connection
that is already open and authenticated, because the password is promised to
be used exactly once.

Run: pytest tests/
"""
import re

import pytest

import backend.remote_routes as rr
from backend.models.remote_models import EnrollSSHRequest


class FakeSFTP:
    def __init__(self, rec):
        self.rec = rec

    def mkdir(self, path):
        self.rec["mkdir"] = path

    def chmod(self, path, mode):
        self.rec["chmod"] = (path, oct(mode))

    def file(self, path, mode):
        self.rec["wrote_to"] = path
        rec = self.rec

        class F:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def write(self_inner, data):
                rec["bytes"] = data
        return F()

    def close(self):
        self.rec["sftp_closed"] = True


class FakeChannel:
    def __init__(self, rc):
        self._rc = rc

    def recv_exit_status(self):
        return self._rc


class FakeStream:
    def __init__(self, text, rc=0):
        self._t = text.encode()
        self.channel = FakeChannel(rc)

    def read(self):
        return self._t


class FakeStdin:
    def __init__(self, rec):
        self.rec = rec

    def write(self, data):
        self.rec.setdefault("stdin", "")
        self.rec["stdin"] += data

    def flush(self):
        pass


class FakeClient:
    """Enough paramiko to drive the push, recording everything it is asked."""

    def __init__(self, rc=0, out="installed", sftp_raises=None):
        self.rec = {"commands": []}
        self._rc = rc
        self._out = out
        self._sftp_raises = sftp_raises

    def open_sftp(self):
        if self._sftp_raises:
            raise self._sftp_raises
        return FakeSFTP(self.rec)

    def exec_command(self, cmd, timeout=None):
        # The configured exit code applies to the AGENT INSTALLER only. The
        # route runs two commands on this connection -- the authorized_keys
        # append first -- and a fake that failed both made "a host whose agent
        # would not install is not recorded" pass without the agent push ever
        # running: the key install raised first, with an empty detail, and the
        # assertion about the host record was true for the wrong reason.
        self.rec["commands"].append(cmd)
        rc = self._rc if "run_agent.sh" in cmd else 0
        out = self._out if "run_agent.sh" in cmd else ""
        return (FakeStdin(self.rec), FakeStream(out, rc), FakeStream("", rc))

    def close(self):
        self.rec["closed"] = True


@pytest.fixture()
def minted(monkeypatch):
    """A controller with an address, and a bundle that costs nothing to mint."""
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    import backend.agent_bundle as ab
    monkeypatch.setattr(ab, "bundle_addresses", lambda cfg: ["10.0.0.1"])
    monkeypatch.setattr(ab, "mint_agent_bundle",
                        lambda addrs, port, environment="": ("bundle.zip", b"ZIPBYTES"))
    from backend import db
    monkeypatch.setattr(db, "get_controller_config", lambda: {"port": 9000})
    return True


def _body(**kw):
    kw.setdefault("name", "web-01")
    kw.setdefault("password", "hunter2")
    return EnrollSSHRequest(**kw)


# ---- the bundle gets there and is run --------------------------------------
def test_the_bundle_is_written_and_the_installer_run(minted):
    c = FakeClient()
    out = rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    assert out["installed"] is True
    assert c.rec["bytes"] == b"ZIPBYTES"
    cmd = c.rec["commands"][0]
    assert "run_agent.sh" in cmd, cmd


def test_it_unpacks_with_python_not_unzip(minted):
    """unzip is missing from plenty of minimal images, and the agent needs
    python3 anyway -- so depending on unzip would turn "this image is small"
    into "enrollment failed"."""
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    cmd = c.rec["commands"][0]
    assert "zipfile" in cmd and "python3" in cmd, cmd
    assert not re.search(r"\bunzip\b", cmd), cmd


def test_the_working_directory_is_private_and_unguessable(minted):
    """The zip carries a live single-use enrollment token. A predictable path
    in a world-writable directory is one unprivileged local user away from
    being read before it is unpacked."""
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    d = c.rec["mkdir"]
    assert d.startswith("/tmp/"), d
    assert len(d) > len("/tmp/.sysible-enroll-"), d
    assert c.rec["chmod"] == (d, oct(0o700)), c.rec["chmod"]


def test_it_cleans_up_after_itself_and_keeps_the_exit_code(minted):
    """A second command to tidy up would not run if the first one died -- and
    after sudo the unpacked files are root-owned, so a later unprivileged rm
    could not have removed them anyway."""
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    cmd = c.rec["commands"][0]
    assert "rm -rf" in cmd, cmd
    assert "exit $rc" in cmd, cmd
    assert len(c.rec["commands"]) == 1, c.rec["commands"]


# ---- the password is used, and not leaked ----------------------------------
def test_a_non_root_login_escalates_with_the_password_on_stdin(minted):
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="ops"), "10.0.0.5")
    cmd = c.rec["commands"][0]
    assert cmd.startswith("sudo -S"), cmd
    assert c.rec.get("stdin") == "hunter2\n", c.rec.get("stdin")


def test_the_password_is_never_in_the_command_line(minted):
    """A command line is in the remote process table and whatever logs it."""
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="ops"), "10.0.0.5")
    assert "hunter2" not in c.rec["commands"][0], c.rec["commands"][0]


def test_root_does_not_sudo(minted):
    c = FakeClient()
    rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    assert not c.rec["commands"][0].startswith("sudo"), c.rec["commands"][0]
    assert "stdin" not in c.rec


# ---- when it cannot ---------------------------------------------------------
def test_a_failing_installer_is_an_error_with_its_output(minted):
    c = FakeClient(rc=3, out="line one\nline two\nfatal: no python3\n")
    with pytest.raises(Exception) as caught:
        rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    detail = caught.value.detail
    assert "exited 3" in detail, detail
    assert "no python3" in detail, detail
    assert "Nothing was enrolled" in detail, detail


def test_no_sftp_says_so_rather_than_failing_obscurely(minted):
    c = FakeClient(sftp_raises=OSError("sftp subsystem not available"))
    with pytest.raises(Exception) as caught:
        rr._push_agent_over_ssh(c, _body(username="root"), "10.0.0.5")
    assert "SFTP" in caught.value.detail, caught.value.detail


def test_a_controller_with_no_address_refuses_up_front(monkeypatch, minted):
    """An agent given nowhere to report to is an agent that will never appear,
    and the operator would be left looking at the host instead of the setting."""
    import backend.agent_bundle as ab
    monkeypatch.setattr(ab, "bundle_addresses", lambda cfg: [])
    with pytest.raises(Exception) as caught:
        rr._push_agent_over_ssh(FakeClient(), _body(username="root"), "10.0.0.5")
    assert "no address" in caught.value.detail, caught.value.detail


# ---- and the route uses it --------------------------------------------------
def _drive_enroll(monkeypatch, client, **body_kw):
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(rr, "_resolve_addrs", lambda t: {"10.0.0.5"})
    monkeypatch.setattr(rr, "_ip_owner", lambda *a, **k: None)
    monkeypatch.setattr(rr, "_ensure_controller_key", lambda: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(rr, "_looks_like_ssh_public_key", lambda k: True)
    monkeypatch.setattr(rr, "_new_ssh_client", lambda: client)
    monkeypatch.setattr(rr, "_ssh_connect", lambda *a, **k: None)
    return rr._enroll_ssh(_body(**body_kw))


def test_enrolling_installs_the_agent(monkeypatch, minted):
    c = FakeClient()
    out = _drive_enroll(monkeypatch, c, username="root", ip="10.0.0.5")
    assert out["enrolled"] is True
    assert out["agent"] and out["agent"]["installed"] is True
    assert any("run_agent.sh" in cmd for cmd in c.rec["commands"]), c.rec["commands"]


def test_a_host_whose_agent_would_not_install_is_not_recorded(monkeypatch, minted):
    """Half-enrolled is the state this whole change exists to stop producing:
    a record for a machine that is managed in name only."""
    from backend.remote_routes import load_hosts
    c = FakeClient(rc=1, out="boom")
    with pytest.raises(Exception):
        _drive_enroll(monkeypatch, c, username="root", ip="10.0.0.5")
    assert "web-01" not in (load_hosts() or {}), \
        "a host record was left behind for a machine with no agent"


def test_ssh_only_is_still_available_on_purpose(monkeypatch, minted):
    """A box deliberately managed over SSH alone, or one whose agent is
    installed some other way. Off by request, never by accident."""
    from backend.remote_routes import load_hosts
    c = FakeClient()
    out = _drive_enroll(monkeypatch, c, username="root", ip="10.0.0.5",
                        install_agent=False)
    assert out["agent"] is None
    assert c.rec["commands"] == [] or all(
        "run_agent.sh" not in cmd for cmd in c.rec["commands"]), c.rec["commands"]
    assert "web-01" in (load_hosts() or {})


def test_installing_the_agent_is_the_default():
    """The console promises it, so it cannot be something a caller has to know
    to ask for."""
    assert EnrollSSHRequest(name="x", password="p").install_agent is True
