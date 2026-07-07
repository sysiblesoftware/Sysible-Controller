#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Provisions an isolated virtualenv with the Python runtime + test dependencies
# so the API test-suite in tests/ (pytest) can import the apps and run during a
# web session.
set -euo pipefail

# Only needed in the remote (web) environment; a local machine already has its
# own setup. Exit quietly otherwise.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$PROJECT_DIR"

VENV="$PROJECT_DIR/.venv"

echo "[session-start] provisioning virtualenv + test dependencies..."
# A venv isolates us from distro-managed packages (which pip can't upgrade to the
# pinned versions) and is idempotent + captured by the container-state cache.
# `python3 -m venv` is a no-op refresh if .venv already exists.
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# Runtime deps (fastapi, pydantic, psutil, paramiko, cryptography, ...) are needed
# because the tests import the real apps; the dev deps add pytest + httpx.
"$VENV/bin/python" -m pip install --quiet -r requirements.txt -r requirements-dev.txt

# Put the venv first on PATH for the session so `pytest`/`python` resolve to it,
# and make the repo root importable regardless of cwd.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export PATH=\"$VENV/bin:\$PATH\""
    echo "export PYTHONPATH=\"$PROJECT_DIR\""
  } >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] done — run the API tests with:  pytest"
