"""Shared green/red completion banner for the System Administration tools'
per-host result tabs.

Most fleet actions (systemctl start, policy pushes, firewall rules, ...)
succeed silently, which made result tabs look empty/broken. This renders a
prominent banner - green when the action succeeded, red when it failed -
above the command output, so the outcome is obvious at a glance. Success is
judged by the exit code where one is available, otherwise by whether
anything went to stderr with no stdout.
"""
import html

from client.theme import STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR


def result_failed(data) -> bool:
    code = data.get("code")
    if code is not None:
        return code != 0
    return bool(data.get("stderr")) and not data.get("stdout")


def result_html(data, ok_label: str = "Done", fail_label: str = "Failed") -> str:
    """HTML (banner + <pre> body) for a result dict with keys
    stdout / stderr / code. `ok_label` / `fail_label` name the action."""
    failed = result_failed(data)
    code = data.get("code")
    bg = STATUS_ERROR_COLOR if failed else STATUS_SUCCESS_COLOR
    headline = f"✗ {fail_label}" if failed else f"✓ {ok_label}"
    if code is not None:
        headline += f" (exit {code})"

    banner = (
        f'<div style="background-color:{bg}; color:#ffffff; font-weight:bold; '
        f'padding:5px 10px; border-radius:4px; margin:0 0 6px 0;">'
        f'{html.escape(headline)}</div>'
    )

    text = data.get("stdout") or ""
    if data.get("stderr"):
        text += f"\n\n--- stderr ---\n{data['stderr']}"
    if not text.strip():
        text = "(no output - command succeeded silently)"

    body = (
        f'<pre style="font-family:monospace; white-space:pre-wrap; margin:0;">'
        f'{html.escape(text)}</pre>'
    )
    return banner + body
