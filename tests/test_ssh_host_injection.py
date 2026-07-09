"""
Security regression: a stored SSH host's `user`/`ip` flow into the ssh command
line (`user@ip` as the destination). A value like `-oProxyCommand=...` would be
parsed by ssh as an option and run code on the CONTROLLER as root. Reject such
values at ingest (charset validation), and keep the `--` separator in the argv.
"""
from conftest import key_headers


def _add(controller, headers, **body):
    return controller.post("/remote/hosts", headers=headers, json=body)


class TestHostFieldValidation:
    def test_option_injection_user_rejected(self, controller, superuser_headers):
        r = _add(controller, superuser_headers, name="evil", ip="1.2.3.4",
                 user="-oProxyCommand=sh -c id #")
        assert r.status_code == 422, r.text

    def test_metachars_in_ip_rejected(self, controller, superuser_headers):
        r = _add(controller, superuser_headers, name="evil2", ip="1.2.3.4; touch /tmp/x",
                 user="root")
        assert r.status_code == 422

    def test_leading_dash_ip_rejected(self, controller, superuser_headers):
        r = _add(controller, superuser_headers, name="evil3", ip="-oProxyCommand=x",
                 user="root")
        assert r.status_code == 422

    def test_bad_name_rejected(self, controller, superuser_headers):
        r = _add(controller, superuser_headers, name="../etc", ip="1.2.3.4", user="root")
        assert r.status_code == 422

    def test_normal_host_accepted(self, controller, superuser_headers):
        r = _add(controller, superuser_headers, name="web-01", ip="10.0.0.5", user="deploy")
        # 200 (added) or a benign non-422 — the point is it's NOT rejected as invalid input.
        assert r.status_code != 422, r.text


def test_ssh_argv_has_double_dash_separator():
    from backend import remote_routes
    argv = remote_routes._ssh_argv("/tmp/key", "root@10.0.0.5", "id")
    assert "--" in argv
    assert argv.index("--") < argv.index("root@10.0.0.5")
    assert argv.index("root@10.0.0.5") < argv.index("id")
