"""No secret a tool form accepts may be stored in the activity log in cleartext.

The agent refuses to log command text at all, precisely because a command can
carry a password. The controller does store it — accountability is the point of
the feed — so log_activity() scrubs it on the way in. That scrub was a denylist
keyed on a NAME sitting next to the value: `--password x`, `TOKEN=x`,
`Authorization: Bearer x`.

Several tools pass the secret as a bare positional argument, so there is no
name to key on:

    SUSEConnect -r <registration code> -e you@example.com
    "$PRO" attach <Ubuntu Pro token>
    subscription-manager register --org acme --activationkey <key>
    printf '%s' <AD password> > "$cred"          (realm join, kinit)
    printf 'username=%s\npassword=%s\n' usr <pw> (CIFS mount)

Every one of those was written to activity_log verbatim and served by
GET /api/activity, whose guard (require_activity_viewer) deliberately ADMITS
the read-only `auditor` role — the role that exists to read that feed.

So this does not test the patterns. It drives a sentinel through every
password-typed parameter of every registered action, builds the real command,
runs the real redactor, and fails if the sentinel survives anywhere. A builder
added later that puts a secret on a command line fails here instead of being
discovered in a log.

RESIDUAL, deliberately not fixed here: these values also ride on the managed
host's own command line, where any local user on THAT host can see them in
`ps`. cmd_set_password avoids it (chpasswd reads the hash from a heredoc on
stdin) but SUSEConnect, `pro attach` and subscription-manager take the secret
as an argument and offer no stdin form, so removing it would mean changing how
registration works on three distro families. The at-rest and auditor-visible
half is what is closed here.
"""
import pytest

from backend import db
from webgui import actions

# Imported for the benign-value machinery, which already knows how to satisfy
# every validator in the registry. Keeping one copy means a new strict builder
# is taught about once.
from tests.test_no_parameter_escapes_a_heredoc import _benign_params

# (value, needle) — the needle is what must be present in the raw command and
# absent from the redacted one. They differ because shlex.quote rewrites a value
# containing a single quote into 'ab'"'"'cd', so the value itself is no longer a
# substring of the command and searching for it would prove nothing.
SENTINELS = [
    ("S3cretValue123", "S3cretValue123"),
    # Quoted into ONE word: a redactor that stops at the first space leaks the tail.
    ("pass phrase with spaces", "phrase with spaces"),
    # Shell metacharacters, so the value is quoted but still findable.
    ('we$(touch x)`"', "we$(touch x)"),
    # The awkward form: quoting splices this into three adjacent chunks, and a
    # token pattern that matches only the first one leaves the rest readable.
    ("ab'cd-TAIL9", "cd-TAIL9"),
]


def _secret_params():
    out = []
    for name, action in sorted(actions._ACTIONS.items()):
        for param in action.params:
            if param.type == "password":
                out.append((name, param.name))
    return out


_SECRET_PARAMS = _secret_params()

# Pinned by name. A secret-bearing field that stops being type="password" drops
# silently out of the sweep above AND out of the browser's masking, so it is
# worth failing on.
_EXPECTED = {
    ("dir_join_ad", "password"),            # realm join, AD account
    ("dir_krb_kinit", "password"),          # kinit
    ("fs_mount_cifs", "password"),          # CIFS credentials file
    ("sub_pro_attach", "token"),            # Ubuntu Pro
    ("sub_register_all", "activationkey"),  # RHSM
    ("sub_register_all", "password"),       # RHSM
    ("sub_register_all", "pro_token"),      # Ubuntu Pro
    ("sub_register_all", "suse_regcode"),   # SUSE SCC
    ("sub_rhsm_register", "activationkey"),
    ("sub_suse_register", "regcode"),
    ("user_create", "password"),
    ("user_set_password", "password"),
}


# Some builders branch on WHICH credential they were given, so the secret under
# test only reaches the command when the alternatives are blank. sub_register_all
# prefers an RHSM activation key over a username+password, so the password branch
# is unreachable until the key is cleared.
_ISOLATE = {
    ("sub_register_all", "password"): {"activationkey": "", "username": "rhsmuser"},
}


def test_the_sweep_covers_every_secret_field():
    assert set(_SECRET_PARAMS) == _EXPECTED


@pytest.mark.parametrize("sentinel,needle", SENTINELS, ids=[n for _, n in SENTINELS])
@pytest.mark.parametrize("action_name,param_name", _SECRET_PARAMS,
                         ids=[f"{a}.{p}" for a, p in _SECRET_PARAMS])
def test_no_secret_survives_redaction(action_name, param_name, sentinel, needle):
    action = actions._ACTIONS[action_name]
    args = _benign_params(action)
    args.update(_ISOLATE.get((action_name, param_name), {}))
    args[param_name] = sentinel
    command = action.build(args)
    if needle not in command:
        # The two local-account builders hash the password on this side and send
        # only the crypt hash, so the secret genuinely never reaches the command.
        # Anything else means this case proves nothing and should not pass quietly.
        assert action_name in ("user_create", "user_set_password"), (
            f"{action_name}.{param_name}: {needle!r} never reached the command, so "
            "this case proves nothing — add an _ISOLATE entry for it")
        return
    redacted = db._redact_secrets(command)
    assert needle not in redacted, (
        f"{action_name}.{param_name} is stored in cleartext:\n"
        + "\n".join(l for l in redacted.splitlines() if needle in l))


def test_an_audit_trail_that_says_nothing_is_not_an_audit_trail():
    """The scrub has to stay conservative. Redacting the whole command would
    pass every test above and destroy the feed's only reason to exist."""
    cmd = ("subscription-manager register --org acme-corp "
           "--activationkey SEKRIT --auto-attach 2>&1")
    out = db._redact_secrets(cmd)
    assert "SEKRIT" not in out
    for keep in ("subscription-manager", "register", "--org", "acme-corp",
                 "--activationkey", "--auto-attach"):
        assert keep in out, f"redaction ate {keep!r}: {out}"


def test_the_positional_patterns_do_not_fire_on_unrelated_text():
    for cmd in ("echo 'the pro attachment is in /tmp'",
                "ls -l /usr/share/doc/SUSEConnect",
                "printf '%s' hello > /tmp/greeting",
                "grep -r attach /etc"):
        assert db._redact_secrets(cmd) == cmd, cmd


def test_a_redacted_command_still_hashes_into_the_audit_chain():
    """log_activity stores the REDACTED string and chains that, so the row is
    tamper-evident over what is actually kept, not over a secret nobody can
    read back."""
    import inspect
    src = inspect.getsource(db.log_activity)
    assert "_redact_secrets(command" in src
    assert "desc, cmd," in src or "cmd," in src
