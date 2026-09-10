"""SLOP SSO must not dead-end on a name the controller already knows.

THE BUG THIS PINS. SSO provisioning refused any username already held by a
locally-created administrator. The refusal was terminal, and it landed on the
single most common deployment: a controller set up standalone and later put
behind SLOP. Both default to the name `admin`, so the very first SSO sign-in
hit the refusal, the console could not mint a session, and the operator was
shown the console's OWN login form — which, in SSO mode, answers every
credential with "this console has no separate login". No retry cleared it.

Adoption cannot escalate: the role written is the one SLOP asserts, and only a
SLOP superuser can mint a username at all — they can already reach any role here
by picking a name that is free. It DOES move ownership, so the tests below pin
the two things that must not ride along with it.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-sso-adopt-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-ssoadopt-"))

import pytest  # noqa: E402

import backend.app as A  # noqa: E402
import backend.portal_auth as PA  # noqa: E402

LOCAL_PW = "Loc4l-Passw0rd!"


@pytest.fixture()
def local_admin(db_path):
    """A controller that was set up standalone first: a local superuser named
    `admin`, with the Connect sudo grant a superuser had given them."""
    salt, pw_hash = PA.hash_password(LOCAL_PW)
    A.add_administrator("admin", pw_hash, salt, must_change_password=0,
                        created_by="setup", role="superuser")
    A.set_administrator_sudo_connect("admin", True)
    return A.get_administrator("admin")


@pytest.fixture()
def db_path(monkeypatch, tmp_path):
    import backend.db as D
    monkeypatch.setattr(D, "DB_PATH", tmp_path / "sso.db")
    D.init_db()
    return D.DB_PATH


def _local_password_still_works(row):
    try:
        return PA.verify_password(LOCAL_PW, row["password_salt"], row["password_hash"])
    except Exception:
        return False            # scrubbed to something unusable — that is the point


def test_a_pre_existing_local_admin_no_longer_blocks_sso(local_admin):
    A.ensure_sso_account("admin", "superuser", "gateway(test)")
    assert (A.get_administrator("admin") or {})["created_by"] == "sso"


def test_the_role_written_is_the_one_slop_asserts(local_admin):
    """Not the local one. SLOP is the identity authority in this mode, so a local
    superuser whom SLOP calls an operator becomes a sysadmin here."""
    A.ensure_sso_account("admin", "sysadmin", "gateway(test)")
    assert A.get_administrator("admin")["role"] == "sysadmin"


def test_adoption_cannot_be_used_to_climb(local_admin):
    """The asserted role is a ceiling as well as a floor: an auditor stays an
    auditor even though the adopted row was a superuser a moment ago."""
    A.ensure_sso_account("admin", "auditor", "gateway(test)")
    assert A.get_administrator("admin")["role"] == "auditor"


def test_the_local_password_stops_working(local_admin):
    """The controller's own /admin/login is still reachable on its backend port.
    Leaving the adopted account's password live would keep a credential SLOP's
    sign-out has no idea about."""
    assert _local_password_still_works(local_admin)          # before
    A.ensure_sso_account("admin", "superuser", "gateway(test)")
    assert not _local_password_still_works(A.get_administrator("admin"))


def test_the_sudo_connect_grant_does_not_ride_along(local_admin):
    """It was granted to the LOCAL admin. Whoever holds that name in SLOP is not
    necessarily the same human, and must be granted it again deliberately."""
    assert local_admin["sudo_connect"] == 1
    A.ensure_sso_account("admin", "superuser", "gateway(test)")
    assert A.get_administrator("admin")["sudo_connect"] == 0


def test_the_takeover_is_recorded(local_admin, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "log_admin_audit",
                        lambda action, target, detail="": seen.append((action, target, detail)))
    A.ensure_sso_account("admin", "sysadmin", "gateway(10.0.0.9)")
    assert any(a == "sso_account_adopted" for a, _, _ in seen), seen
    detail = [d for a, _, d in seen if a == "sso_account_adopted"][0]
    assert "created_by=setup" in detail and "role=superuser" in detail
    assert "10.0.0.9" in detail


def test_an_sso_owned_account_is_still_just_role_synced(db_path, monkeypatch):
    """The existing path is unchanged — adoption is only for rows SSO did not
    create, so a normal SSO user's password is not re-scrubbed on every sign-in."""
    A.ensure_sso_account("bob", "sysadmin", "gateway(test)")
    before = A.get_administrator("bob")["password_hash"]
    A.ensure_sso_account("bob", "auditor", "gateway(test)")
    after = A.get_administrator("bob")
    assert after["role"] == "auditor" and after["password_hash"] == before


def test_a_first_seen_name_is_still_provisioned_fresh(db_path):
    A.ensure_sso_account("carol", "superuser", "gateway(test)")
    row = A.get_administrator("carol")
    assert row["created_by"] == "sso" and row["role"] == "superuser"
    assert row["sudo_connect"] == 0 and row["must_change_password"] == 0


# --- the console has to SAY which door it is ---------------------------------
# Even with adoption in place a sign-in can fail for other reasons (controller
# unreachable, a malformed name, a seat cap). The SPA used to fall straight
# through to its login form, which under SSO answers 403 to every credential —
# an operator staring at a form that cannot work, with nothing telling them why.

def _bff_client():
    from fastapi.testclient import TestClient
    import webgui.server as w
    return TestClient(w.app, base_url="https://testserver"), w


def test_the_console_tells_an_anonymous_caller_which_sign_in_it_uses(monkeypatch):
    c, w = _bff_client()
    monkeypatch.setattr(w, "_TRUST_SSO", True)
    monkeypatch.setattr(w, "_SSO_SECRET", "shh")
    assert c.get("/api/auth-mode").json() == {"sso": True}


def test_a_standalone_console_reports_its_own_login(monkeypatch):
    c, w = _bff_client()
    monkeypatch.setattr(w, "_TRUST_SSO", False)
    monkeypatch.setattr(w, "_SSO_SECRET", "")
    assert c.get("/api/auth-mode").json() == {"sso": False}


def test_the_probe_leaks_nothing_else(monkeypatch):
    """It is reachable without a session, so it must answer exactly one question —
    not the shared secret, not a username, not why a sign-in failed."""
    c, w = _bff_client()
    monkeypatch.setattr(w, "_TRUST_SSO", True)
    monkeypatch.setattr(w, "_SSO_SECRET", "super-secret-value")
    body = c.get("/api/auth-mode")
    assert set(body.json()) == {"sso"}
    assert "super-secret-value" not in body.text
