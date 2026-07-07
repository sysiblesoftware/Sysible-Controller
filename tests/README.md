# API test-suite

Exhaustive request-level tests for the Sysible Controller API, driving the real
FastAPI apps through Starlette's `TestClient` (no network, no live controller)
against a throwaway SQLite DB + JSON side-stores created per test.

## Running

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                     # from the repo root
pytest tests/test_authentication.py -v
```

Each test gets a clean database and clean in-memory state (`conftest.py`'s
autouse `_isolate` fixture), so order never matters and tests can run in
parallel-safe isolation.

## What's covered

| File | Focus |
|------|-------|
| `test_authentication.py` | Controller API key (missing / wrong / empty / near-miss), admin login tokens (missing / invalid / expired / deleted-admin), `/admin/login` password checks, per-host agent secret (wrong secret, unknown host, **cross-host spoofing**). |
| `test_permissions.py` | RBAC: sysadmin & auditor refused superuser-only actions; auditor is read-only (can read the activity feed, cannot dispatch/exec/open-terminal/revoke — including the `log:false` bypass regression guard); positive operator controls; last-superuser delete guard. |
| `test_input_validation.py` | Malformed/truncated/non-object JSON, missing required fields, wrong data types, agent-channel **size caps** (token / measurements / result / command), and **Unicode** round-tripping (CJK, emoji, combining marks). |
| `test_injection.py` | **SQL injection** payloads proven inert (target table survives, payload stored as a literal), no login auth-bypass; **XSS** payloads stored as data and returned as `application/json` (never reflected as HTML, never stripped). |
| `test_idempotency_and_rate.py` | Duplicate/replayed requests (enroll-token replay refused on live/revoked host, idempotent heartbeat, non-idempotent task dispatch, duplicate admin/environment rejected) and the web-console **login throttle** (429 after N failures, reset on success). |

## Fixtures (`conftest.py`)

- `controller` / `bff` — TestClients for the controller API and the web console.
- `make_admin(username, role)` — creates an admin of any role and returns a live token.
- `superuser_headers` / `sysadmin_headers` / `auditor_headers` — ready-to-use auth headers.
- `enroll_token()` / `agent()` / `ssh_host()` — seed enrollment tokens, agents, and SSH host records.
- `SQLI_PAYLOADS` / `XSS_PAYLOADS` / `UNICODE_PAYLOADS` — reusable hostile-input corpora.

## Notes

- The suite exercises both the controller (`backend/app.py`, port 9000 in prod)
  and the web BFF (`webgui/server.py`, port 8800). Endpoints under the
  `remote_router` are mounted at `/remote/...` on the controller.
- These are request/behaviour tests. They assert that bad input is rejected with
  a 4xx (never a 500 / stack trace) and that hostile input is stored/compared as
  inert data — they are not a substitute for output-encoding at render time in
  the frontend.
