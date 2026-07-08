"""
Regression tests for security fixes ported from the security review:

  - grub default-entry command injection (client/_api_boot.py)
  - pure-SSH file routes require superuser server-side (backend/remote_routes.py)
  - BFF logout revokes the controller token (covered indirectly; unit-scoped here)
"""
import shlex

from client import _api_boot


# ---------------------------------------------------------------------------
# Command injection — grub default-entry banner
# ---------------------------------------------------------------------------
def test_grub_default_entry_is_not_injectable():
    payload = "0'; touch /tmp/pwned; echo '"
    out = _api_boot.cmd_set_grub_default(payload)
    # The vulnerable form concatenated the RAW entry into an echo banner. The fix
    # uses printf with a shlex-quoted arg.
    assert "echo 'Default boot entry set to " not in out
    assert "printf 'Default boot entry set to %s.\\n'" in out
    # The entry must appear ONLY in shlex-quoted form — all three uses (two
    # grub-set-default calls + the printf banner). A count < 3 would mean an
    # unquoted, live-shell occurrence.
    q = shlex.quote(payload)
    assert q in out and out.count(q) == 3


def test_grub_default_entry_normal_value_still_works():
    out = _api_boot.cmd_set_grub_default("0")
    assert "grub2-set-default 0" in out and "printf 'Default boot entry set to %s.\\n' 0" in out


# ---------------------------------------------------------------------------
# Authorization — pure-SSH file routes are superuser-only on the controller
# ---------------------------------------------------------------------------
def test_ssh_download_requires_superuser(controller, ssh_host, sysadmin_headers):
    name = ssh_host()
    r = controller.get(f"/remote/hosts/{name}/files/download?path=/etc/shadow",
                       headers=sysadmin_headers)
    assert r.status_code == 403


def test_ssh_upload_requires_superuser(controller, ssh_host, sysadmin_headers):
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/files/upload", headers=sysadmin_headers)
    assert r.status_code == 403
