"""No tool parameter may become shell by closing the heredoc that carries it.

Several builders ship a multi-line file to the host inside a heredoc with a
QUOTED delimiter — a unit file, krb5.conf, sssd.conf, authorized_keys, a
password hash for chpasswd. That is the right transport: a quoted delimiter
stops every kind of expansion inside the body, which is what you want for file
contents full of quotes, backslashes and `$`.

What a quoted delimiter does NOT stop is a body line that IS the delimiter. The
shell ends the heredoc there and reads the rest as commands, at the task's
privilege. shlex.quote does not help — the value is not in argv.

This is checked the only way worth checking it: by asking a real shell to parse
the command the builder actually emits. The payload's escaped half is
`((((((((` — harmless text inside a heredoc body, an unterminated subshell
anywhere it is parsed as code. So `sh -n` is clean when the value stayed data
and fails when it became code, with no need to run anything.

Every action in the registry is swept, every text-ish parameter, against every
delimiter that action's own command uses.
"""
import shutil
import subprocess

import pytest

from webgui import actions

# Harmless as data (a line in a file), a parse error as code — in ANY context,
# including inside a `case` arm, which is why it is not `;;` or `)`.
_ESCAPED = "((((((((" 
_SHELLS = [s for s in ("dash", "bash") if shutil.which(s)]


def _parse_ok(shell, command):
    """(ok, stderr) from `<shell> -n` — parse only, nothing is executed."""
    r = subprocess.run([shell, "-n"], input=command, text=True,
                       capture_output=True, timeout=30)
    return r.returncode == 0, (r.stderr or "").strip()


def _example(param):
    """The "e.g. ..." the form itself shows, so a validator that insists on a
    cron spec or a CIDR gets one. Without this the strict builders drop out of
    the sweep entirely — which is how the krb5.conf heredoc came to be missed
    by the first version of this file."""
    import re
    for text in (param.placeholder, param.help):
        m = re.search(r"e\.g\.\s*([^)\n]+)", text or "")
        if m:
            return m.group(1).strip().rstrip(".,").strip("'\"")
    return None


# A value each strict validator will accept, by parameter name. Without these
# the builders with the tightest input rules drop OUT of the sweep, which is the
# opposite of what should happen — they are the ones worth sweeping. Keyed on
# name because that is what the validators key on.
_HINTS = {
    "port": "636", "host": "host.example.com", "server": "host.example.com",
    "domain": "corp.example.com", "realm": "CORP.EXAMPLE.COM",
    "kdc": "kdc.corp.example.com", "admin_server": "kdc.corp.example.com",
    "base_dn": "dc=corp,dc=example,dc=com", "table": "filter", "chain": "INPUT",
    "family": "inet", "hook": "input", "priority": "0",
    "destination_cidr": "10.0.0.0/24", "via_gateway": "10.0.0.1",
    "gateway": "10.0.0.1", "ip_cidr": "10.0.0.5/24", "dns": "10.0.0.2",
    "mtu": 1500, "value": "027", "name": "SYSIBLE_EXAMPLE", "number": "1",
    "target": "noble", "devices": "/dev/sdb /dev/sdc", "path": "/tmp/example",
    "export_path": "/exports/data", "share": "example",
    "schedule": "0 2 * * *", "mode": "0644", "umask": "027",
}


def _benign(param):
    if param.type == "checkbox":
        return False
    if param.type == "select" and param.options:
        opt = param.options[0]
        return opt[0] if isinstance(opt, (list, tuple)) else opt
    if param.type == "number":
        hint = _HINTS.get(param.name)
        if isinstance(hint, int):
            return hint
        try:
            return int(hint if hint is not None else _example(param))
        except (TypeError, ValueError):
            return 1
    return _HINTS.get(param.name) or _example(param) or "benign"


# Where one parameter name means different things in different actions.
_ACTION_HINTS = {
    "health_journal": {"priority": "err"},
    "fw_nft_delete_rule": {"handle": "1"},
    "fw_ufw_add_rule": {"source": "10.0.0.0/24"},
}


def _benign_params(action):
    out = {p.name: _benign(p) for p in action.params}
    out.update({k: v for k, v in _ACTION_HINTS.get(action.name, {}).items() if k in out})
    return out


def _delimiters(command):
    """Heredoc delimiters this command opens, in the forms this codebase uses."""
    import re
    return sorted({m.group(1) or m.group(2) or m.group(3) for m in
                   re.finditer(r"<<-?(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))",
                               command)})


def _sweep():
    """Every action, built from benign values; the heredoc ones with the
    delimiters their command opens."""
    heredoc, unbuildable = [], []
    for name, action in sorted(actions._ACTIONS.items()):
        try:
            cmd = action.build(_benign_params(action))
        except Exception as e:
            unbuildable.append((name, f"{type(e).__name__}: {e}"))
            continue
        if isinstance(cmd, str) and "<<" in cmd:
            heredoc.append((name, action, cmd))
    return heredoc, unbuildable


_HEREDOC_ACTIONS, _UNBUILDABLE = _sweep()

# The actions that ship a file body inside a heredoc. Named rather than counted:
# if one is renamed or stops using a heredoc, this list says so instead of the
# coverage quietly shrinking to nothing.
_EXPECTED_HEREDOC = {
    "backup_schedule",        # /usr/local/sbin/sysible-backup.sh
    "dir_config_ldap_client", # /etc/sssd/sssd.conf
    "dir_krb_config",         # /etc/krb5.conf
    "policy_sudo",            # a sudoers drop-in
    "repo_create",            # a yum/apt repo file
    "svc_create",             # a .service unit
    "svc_set_deps",           # a unit drop-in override
    "timer_create",           # a .service + .timer pair
    "user_create",            # the new account's authorized_keys
    "user_set_password",      # chpasswd -e
}


def test_the_sweep_can_build_every_action():
    """Guards the sweep itself. An action that cannot be built from these values
    is an action this file silently does not test — which is how the krb5.conf
    heredoc was missed the first time round. Add a hint above instead of letting
    it drop out."""
    assert _SHELLS, "no shell to parse with"
    assert _UNBUILDABLE == [], _UNBUILDABLE


def test_the_sweep_found_every_heredoc_builder():
    assert {n for n, _, _ in _HEREDOC_ACTIONS} == _EXPECTED_HEREDOC


@pytest.mark.parametrize("shell", _SHELLS)
def test_the_oracle_catches_an_escape_it_is_shown(shell):
    """Self-test. A deliberately vulnerable builder must be reported unsafe, and
    the same shape with the body refused must be reported safe."""
    body_safe = "a: 1\nb: 2"
    body_bad = f"a: 1\nSYSEOF\n{_ESCAPED}\ncat > /dev/null <<'SYSEOF'\nb: 2"
    tmpl = "cat > /tmp/x <<'SYSEOF'\n%s\nSYSEOF\necho done"
    assert _parse_ok(shell, tmpl % body_safe)[0]
    ok, err = _parse_ok(shell, tmpl % body_bad)
    assert not ok, "the oracle did not notice the escape"
    assert err


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("name,action,benign_cmd",
                         _HEREDOC_ACTIONS, ids=[n for n, _, _ in _HEREDOC_ACTIONS])
def test_no_parameter_closes_the_heredoc(shell, name, action, benign_cmd):
    ok, err = _parse_ok(shell, benign_cmd)
    assert ok, f"{name}: the benign command does not even parse: {err}"

    delims = _delimiters(benign_cmd)
    assert delims, f"{name}: a command with '<<' but no delimiter found"

    textish = [p for p in action.params if p.type in ("text", "password", "textarea")]
    for param in textish:
        for delim in delims:
            payload = f"ok\n{delim}\n{_ESCAPED}\ncat > /dev/null <<'{delim}'\nfiller"
            args = _benign_params(action)
            args[param.name] = payload
            try:
                cmd = action.build(args)
            except Exception:
                continue        # refused at ingest — the correct outcome
            ok, err = _parse_ok(shell, cmd)
            assert ok, (f"{name}: parameter {param.name!r} closed the {delim} "
                        f"heredoc and the rest of its value became shell\n{err}")
