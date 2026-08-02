"""
Fleet alerting for the web console: evaluate a small set of rules against the
health/patch/posture sweeps and notify via email and/or webhook when a host
crosses a threshold (fire once), and again when it clears (resolve).

Config + firing-state live in run/webgui_alerts.json. The SMTP password is
encrypted at rest with the same Fernet key sudo_store uses (never stored clear,
never returned to the client). Rule evaluation and senders are here; the
evaluator thread lives in webgui/server.py (it owns the sweeps).
"""
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import threading as _threading
from webgui import sudo_store  # reuse its Fernet key for the SMTP secret
from webgui._jsonstore import atomic_write_json

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "webgui_alerts.json"
_LOCK = _threading.Lock()

# rule key -> (label, has_threshold, default_threshold)
RULES = {
    "host_offline":    ("Host offline", False, None),
    "disk_critical":   ("Disk usage ≥ threshold%", True, 90),
    "mem_high":        ("Memory usage ≥ threshold%", True, 90),
    "load_high":       ("Load average (1m) ≥ threshold", True, 8),
    "failed_units":    ("Failed systemd units", False, None),
    "oom_events":      ("OOM (out-of-memory) kills", False, None),
    "updates_pending": ("Pending updates ≥ threshold", True, 1),
    "security_updates": ("Security updates pending", False, None),
    "reboot_required": ("Reboot required", False, None),
    "cert_expiring":   ("TLS cert expiring < threshold days", True, 30),
    "firewall_disabled": ("Firewall disabled", False, None),
    "mac_not_enforcing": ("SELinux/AppArmor not enforcing", False, None),
    "ssh_root_login":  ("SSH root login enabled", False, None),
    "time_unsynced":   ("Clock not synchronized", False, None),
}
_DEFAULT_ON = ("host_offline", "disk_critical", "failed_units")


def _default_config():
    return {
        "channels": {
            "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "use_tls": True,
                      "username": "", "password_enc": "", "from_addr": "", "to_addrs": ""},
            "webhook": {"enabled": False, "url": ""},
        },
        "rules": {k: {"enabled": k in _DEFAULT_ON, "threshold": d} for k, (_l, _t, d) in RULES.items()},
        # Operator-defined regex checks: run `command` on each host, alert when
        # the output matches (mode "present") or fails to match (mode "absent").
        "custom_rules": [],   # [{id, name, command, regex, mode, enabled}]
        "state": {},          # "host_id|rule" -> True while firing
    }


def _load():
    try:
        cfg = json.loads(_DATA_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(cfg, dict):
        return _default_config()   # valid JSON but wrong shape -> don't AttributeError below
    base = _default_config()
    base["channels"]["email"].update(cfg.get("channels", {}).get("email", {}))
    base["channels"]["webhook"].update(cfg.get("channels", {}).get("webhook", {}))
    for k in RULES:
        if k in cfg.get("rules", {}):
            base["rules"][k].update(cfg["rules"][k])
    base["custom_rules"] = cfg.get("custom_rules", [])
    base["state"] = cfg.get("state", {})
    return base


def _save(cfg):
    atomic_write_json(_DATA_FILE, cfg)


def _encrypt(pw):
    key = sudo_store._get_key()
    if not key or not pw:
        return ""
    from cryptography.fernet import Fernet
    return Fernet(key).encrypt(pw.encode()).decode()


def _decrypt(token):
    key = sudo_store._get_key()
    if not key or not token:
        return ""
    try:
        from cryptography.fernet import Fernet, InvalidToken
        return Fernet(key).decrypt(token.encode()).decode()
    except Exception:
        return ""


def get_config_redacted():
    """Config for the UI: password replaced by a has_password flag."""
    cfg = _load()
    email = dict(cfg["channels"]["email"])
    email["has_password"] = bool(email.pop("password_enc", ""))
    return {"channels": {"email": email, "webhook": cfg["channels"]["webhook"]},
            "rules": cfg["rules"], "custom_rules": cfg.get("custom_rules", []),
            "rule_meta": {k: {"label": l, "has_threshold": t} for k, (l, t, _d) in RULES.items()}}


def _sanitize_custom(rules):
    """Validate operator custom rules; assign ids, drop invalid regexes."""
    import re
    import uuid
    out = []
    for r in (rules or []):
        cmd = (r.get("command") or "").strip()
        rx = (r.get("regex") or "").strip()
        if not cmd or not rx:
            continue
        try:
            re.compile(rx)
        except re.error:
            continue
        out.append({
            "id": r.get("id") or uuid.uuid4().hex[:12],
            "name": (r.get("name") or "custom check").strip(),
            "command": cmd, "regex": rx,
            "mode": "absent" if r.get("mode") == "absent" else "present",
            "enabled": bool(r.get("enabled", True)),
        })
    return out


def norm_custom_rules(rules):
    """Order-independent normalized view of the custom-rule set, for change
    detection (so the BFF can allow a sysadmin to re-save the form unchanged but
    require a superuser to add/edit/enable/disable a command rule)."""
    return sorted(
        (r.get("command", ""), r.get("regex", ""),
         "absent" if r.get("mode") == "absent" else "present",
         (r.get("name") or "").strip(), bool(r.get("enabled", True)))
        for r in (rules or []))


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def set_config(new):
    # Harden against a hostile/malformed body: a non-dict channels/rules/email
    # would otherwise AttributeError/TypeError below and 500 instead of being
    # ignored. Each nested container is coerced to {} if it isn't a dict.
    new = _as_dict(new)
    channels = _as_dict(new.get("channels"))
    e = _as_dict(channels.get("email"))
    w = _as_dict(channels.get("webhook"))
    rules_in = _as_dict(new.get("rules"))
    with _LOCK:
        cfg = _load()
        cur = cfg["channels"]["email"]
        for k in ("enabled", "smtp_host", "smtp_port", "use_tls", "username", "from_addr", "to_addrs"):
            if k in e:
                cur[k] = e[k]
        # Only replace the stored password when a new non-empty one is supplied.
        if e.get("password"):
            cur["password_enc"] = _encrypt(e["password"])
        cfg["channels"]["webhook"].update({k: w[k] for k in ("enabled", "url") if k in w})
        for k in RULES:
            r = _as_dict(rules_in.get(k))
            cfg["rules"][k].update({kk: r[kk] for kk in ("enabled", "threshold") if kk in r})
        if "custom_rules" in new:
            cfg["custom_rules"] = _sanitize_custom(new["custom_rules"])
        _save(cfg)
    return get_config_redacted()


# ---- evaluation ----------------------------------------------------------
def evaluate_rules(cfg, hosts):
    """Given the merged per-host view (list of dicts with any of: online, disk,
    failed, security, reboot, cert_days) return the currently-firing conditions:
    list of {key, host_id, host, rule, message}."""
    rules = cfg["rules"]
    firing = []

    def add(host, rule, msg):
        firing.append({"key": f"{host.get('id')}|{rule}", "host_id": host.get("id"),
                       "host": host.get("host"), "rule": rule, "message": msg})

    def thr(rule, default, cast=int):
        # A rule may carry a null/blank/garbage threshold (e.g. the field was
        # cleared in the UI, stored as JSON null). `.get("threshold", default)`
        # returns None when the key EXISTS with value null, and int(None) raised
        # — crashing the whole evaluation cycle and silently halting every alert.
        # Coerce None/blank/uncastable back to the safe default.
        v = (rules.get(rule) or {}).get("threshold", default)
        if v is None or v == "":
            v = default
        try:
            return cast(v)
        except (TypeError, ValueError):
            return cast(default)

    for h in hosts:
        name = h.get("host")
        if rules.get("host_offline", {}).get("enabled") and h.get("online") is False:
            add(h, "host_offline", f"{name} is offline")
        if rules.get("disk_critical", {}).get("enabled") and h.get("disk") is not None:
            t = thr("disk_critical", 90)
            if h["disk"] >= t:
                add(h, "disk_critical", f"{name} disk at {h['disk']}% (≥ {t}%)")
        if rules.get("mem_high", {}).get("enabled") and h.get("mem") is not None:
            t = thr("mem_high", 90)
            if h["mem"] >= t:
                add(h, "mem_high", f"{name} memory at {h['mem']}% (≥ {t}%)")
        if rules.get("load_high", {}).get("enabled") and h.get("load1") is not None:
            t = float(thr("load_high", 8, float))
            if h["load1"] >= t:
                add(h, "load_high", f"{name} load {h['load1']} (≥ {t})")
        if rules.get("failed_units", {}).get("enabled") and (h.get("failed") or 0) > 0:
            add(h, "failed_units", f"{name} has {h['failed']} failed unit(s)")
        if rules.get("oom_events", {}).get("enabled") and (h.get("oom") or 0) > 0:
            add(h, "oom_events", f"{name} had {h['oom']} OOM kill(s)")
        if rules.get("updates_pending", {}).get("enabled") and h.get("total") is not None:
            t = thr("updates_pending", 1)
            if (h["total"] or 0) >= t:
                add(h, "updates_pending", f"{name} has {h['total']} pending update(s)")
        if rules.get("security_updates", {}).get("enabled") and (h.get("security") or 0) > 0:
            add(h, "security_updates", f"{name} has {h['security']} pending security update(s)")
        if rules.get("reboot_required", {}).get("enabled") and h.get("reboot"):
            add(h, "reboot_required", f"{name} requires a reboot")
        if rules.get("cert_expiring", {}).get("enabled") and h.get("cert_days") is not None:
            t = thr("cert_expiring", 30)
            if h["cert_days"] < t:
                add(h, "cert_expiring", f"{name} TLS cert expires in {h['cert_days']} day(s)")
        for flag, label in (("firewall_disabled", "firewall disabled"),
                            ("mac_not_enforcing", "SELinux/AppArmor not enforcing"),
                            ("ssh_root_login", "SSH root login enabled"),
                            ("time_unsynced", "clock not synchronized")):
            if rules.get(flag, {}).get("enabled") and h.get(flag) is True:
                add(h, flag, f"{name}: {label}")
    return firing


def custom_match(rule, output):
    """For a custom rule, return the 'why' string if it should fire against this
    command output, else None. mode 'present' fires when the regex matches;
    'absent' fires when it does NOT (a health check that must return something)."""
    import re
    try:
        rx = re.compile(rule.get("regex", ""))
    except re.error:
        return None
    m = rx.search(output or "")
    if rule.get("mode") == "absent":
        return "(expected pattern not found)" if not m else None
    return (m.group(0)[:200] if m else None)


def diff_state(cfg, firing):
    """Compare firing to stored state; return (newly_firing, resolved) and
    persist the new state. Fire-once semantics."""
    prev = set(cfg.get("state", {}).keys())
    now = {f["key"]: f for f in firing}
    now_keys = set(now.keys())
    newly = [now[k] for k in (now_keys - prev)]
    resolved = [{"key": k, "message": k.split("|", 1)[-1]} for k in (prev - now_keys)]
    # Persist only the fire-once `state` onto a FRESH copy inside the lock, so the
    # background alerts loop can't clobber a concurrent operator rule/SMTP edit
    # (it used to save its whole stale cfg).
    with _LOCK:
        fresh = _load()
        fresh["state"] = {k: True for k in now_keys}
        _save(fresh)
    return newly, resolved


# ---- senders -------------------------------------------------------------
def _send_email(cfg, subject, body):
    e = cfg["channels"]["email"]
    if not e.get("enabled") or not e.get("smtp_host") or not e.get("to_addrs"):
        return False, "email not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e.get("from_addr") or (e.get("username") or "sysible@localhost")
    msg["To"] = e["to_addrs"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(e["smtp_host"], int(e.get("smtp_port") or 587), timeout=15) as s:
            if e.get("use_tls"):
                s.starttls(context=ssl.create_default_context())
            pw = _decrypt(e.get("password_enc", ""))
            if e.get("username") and pw:
                s.login(e["username"], pw)
            s.send_message(msg)
        return True, "sent"
    except Exception as ex:
        return False, str(ex)


def _webhook_url_safe(url):
    """SSRF guard: allow only http(s) URLs whose host resolves entirely to
    public addresses. Blocks loopback/private/link-local (incl. the
    169.254.169.254 cloud-metadata address), reserved, multicast and
    unspecified targets so an operator can't point the controller at internal
    services. Returns (ok, reason, pinned_ip).

    The resolved public IP is returned so the caller can CONNECT to that exact
    address rather than re-resolving the hostname — otherwise a hostile DNS server
    could answer the check with a public IP and the real request with an internal
    one (DNS rebinding). Pinning here closes that gap."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, "webhook URL must be http(s)", None
    host = p.hostname
    if not host:
        return False, "invalid webhook URL", None
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as ex:
        return False, f"cannot resolve webhook host: {ex}", None
    pinned = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Normalize an IPv4-mapped IPv6 address (::ffff:127.0.0.1) to its IPv4 form
        # before the range checks: Python's ipaddress does NOT flag the mapped form
        # as loopback/private, so without this a resolver returning a mapped address
        # could smuggle an internal target past the guard.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False, "webhook host resolves to a non-public address (blocked)", None
        if pinned is None:
            pinned = info[4][0]
    return True, "ok", pinned


def _send_webhook(cfg, subject, body):
    import http.client
    import socket
    import ssl
    from urllib.parse import urlparse
    w = cfg["channels"]["webhook"]
    if not w.get("enabled") or not w.get("url"):
        return False, "webhook not configured"
    ok, why, pinned_ip = _webhook_url_safe(w["url"])
    if not ok:
        return False, why

    # Connect to the VERIFIED public IP (pinned_ip), not by re-resolving the host —
    # this is what closes the DNS-rebinding window. SNI and cert verification still
    # use the original hostname (a mismatched cert fails, as it should).
    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((pinned_ip, self.port), self.timeout)

    try:
        p = urlparse(w["url"])
        port = p.port or (443 if p.scheme == "https" else 80)
        payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        if p.scheme == "https":
            conn = _PinnedHTTPSConnection(p.hostname, port=port, timeout=15,
                                          context=ssl.create_default_context())
        else:
            conn = _PinnedHTTPConnection(p.hostname, port=port, timeout=15)
        try:
            conn.request("POST", path, body=payload,
                         headers={"Content-Type": "application/json", "Host": p.hostname})
            conn.getresponse().read()
        finally:
            conn.close()
        return True, "sent"
    except Exception as ex:
        return False, str(ex)


def notify(cfg, subject, body):
    """Send to every enabled channel; return per-channel results."""
    out = {}
    if cfg["channels"]["email"].get("enabled"):
        out["email"] = _send_email(cfg, subject, body)
    if cfg["channels"]["webhook"].get("enabled"):
        out["webhook"] = _send_webhook(cfg, subject, body)
    return out


def load():
    return _load()
