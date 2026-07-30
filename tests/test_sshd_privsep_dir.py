"""sshd config-apply must ensure /run/sshd exists before `sshd -t`.

On Debian/Ubuntu `sshd -t` fails with 'Missing privilege separation directory:
/run/sshd' when that runtime dir is absent — which made a VALID hardening config
(PermitRootLogin no, PasswordAuthentication no, …) get rejected and rolled back.
The fix creates the standard root-owned 0755 privsep dir before validating,
WITHOUT bypassing `sshd -t` or the fail-closed restore. These tests pin exactly
that: dir-created-before-validate, validation still runs, backup restore intact.
"""
import client._api_security as s


def _apply_scripts():
    """Every generator that writes sshd_config and validates it."""
    return [
        s._build_sshd_set_option_script("PermitRootLogin", "no", reload=True),
        s._build_sshd_set_option_script("PasswordAuthentication", "no", reload=True),
        s.cmd_sshd_set_option("X11Forwarding", "no"),
    ]


def test_privsep_dir_created_before_validation():
    for cmd in _apply_scripts():
        i_dir = cmd.find("mkdir -p /run/sshd")
        i_val = cmd.find('"$SSHDBIN" -t')
        assert i_dir != -1, "privsep dir /run/sshd is not created"
        assert i_val != -1 and i_dir < i_val, "dir must be created BEFORE sshd -t"


def test_validation_is_not_bypassed_and_restore_kept():
    # The whole point: we make a GOOD config validate, we do NOT skip validation.
    for cmd in _apply_scripts():
        assert '"$SSHDBIN" -t 2>&1; then' in cmd            # sshd -t still gates the apply
        assert "restoring previous sshd_config" in cmd       # bad config still rolls back
        assert "cp /etc/ssh/sshd_config.bak /etc/ssh/sshd_config" in cmd


def test_privsep_dir_is_root_0755_and_best_effort():
    cmd = s._build_sshd_set_option_script("PermitRootLogin", "no")
    # Standard sshd privsep perms; never fail the whole apply if mkdir/chmod can't run.
    assert "chmod 0755 /run/sshd" in cmd
    assert "mkdir -p /run/sshd 2>/dev/null || true" in cmd
    assert "chmod 0755 /run/sshd 2>/dev/null || true" in cmd


def test_reload_and_status_also_ensure_privsep_dir():
    for cmd in (s.cmd_sshd_reload(), s.cmd_sshd_status()):
        i_dir = cmd.find("mkdir -p /run/sshd")
        i_val = cmd.find('"$SSHDBIN" -t')
        assert i_dir != -1 and i_val != -1 and i_dir < i_val
