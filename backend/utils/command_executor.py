import subprocess


def run_command(command, input_text=None):
    """Run a command given as an argument list (never a shell string) and
    capture its output. `input_text`, if given, is piped to stdin - used
    e.g. for `chpasswd` rather than passing secrets as argv."""

    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True
    )

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode
    }
