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

from webgui import sudo_store  # reuse its Fernet key for the SMTP secret

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "webgui_alerts.json"

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
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _DATA_FILE.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(_DATA_FILE, 0o600)
    except OSError:
        pass


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


def set_config(new):
    cfg = _load()
    e = new.get("channels", {}).get("email", {})
    cur = cfg["channels"]["email"]
    for k in ("enabled", "smtp_host", "smtp_port", "use_tls", "username", "from_addr", "to_addrs"):
        if k in e:
            cur[k] = e[k]
    # Only replace the stored password when a new non-empty one is supplied.
    if e.get("password"):
        cur["password_enc"] = _encrypt(e["password"])
    w = new.get("channels", {}).get("webhook", {})
    cfg["channels"]["webhook"].update({k: w[k] for k in ("enabled", "url") if k in w})
    for k in RULES:
        if k in new.get("rules", {}):
            cfg["rules"][k].update({kk: new["rules"][k][kk]
                                    for kk in ("enabled", "threshold") if kk in new["rules"][k]})
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

    for h in hosts:
        name = h.get("host")
        if rules.get("host_offline", {}).get("enabled") and h.get("online") is False:
            add(h, "host_offline", f"{name} is offline")
        if rules.get("disk_critical", {}).get("enabled") and h.get("disk") is not None:
            thr = int(rules["disk_critical"].get("threshold", 90))
            if h["disk"] >= thr:
                add(h, "disk_critical", f"{name} disk at {h['disk']}% (≥ {thr}%)")
        if rules.get("mem_high", {}).get("enabled") and h.get("mem") is not None:
            thr = int(rules["mem_high"].get("threshold", 90))
            if h["mem"] >= thr:
                add(h, "mem_high", f"{name} memory at {h['mem']}% (≥ {thr}%)")
        if rules.get("load_high", {}).get("enabled") and h.get("load1") is not None:
            thr = float(rules["load_high"].get("threshold", 8))
            if h["load1"] >= thr:
                add(h, "load_high", f"{name} load {h['load1']} (≥ {thr})")
        if rules.get("failed_units", {}).get("enabled") and (h.get("failed") or 0) > 0:
            add(h, "failed_units", f"{name} has {h['failed']} failed unit(s)")
        if rules.get("oom_events", {}).get("enabled") and (h.get("oom") or 0) > 0:
            add(h, "oom_events", f"{name} had {h['oom']} OOM kill(s)")
        if rules.get("updates_pending", {}).get("enabled") and h.get("total") is not None:
            thr = int(rules["updates_pending"].get("threshold", 1))
            if (h["total"] or 0) >= thr:
                add(h, "updates_pending", f"{name} has {h['total']} pending update(s)")
        if rules.get("security_updates", {}).get("enabled") and (h.get("security") or 0) > 0:
            add(h, "security_updates", f"{name} has {h['security']} pending security update(s)")
        if rules.get("reboot_required", {}).get("enabled") and h.get("reboot"):
            add(h, "reboot_required", f"{name} requires a reboot")
        if rules.get("cert_expiring", {}).get("enabled") and h.get("cert_days") is not None:
            thr = int(rules["cert_expiring"].get("threshold", 30))
            if h["cert_days"] < thr:
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
    cfg["state"] = {k: True for k in now_keys}
    _save(cfg)
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


def _send_webhook(cfg, subject, body):
    w = cfg["channels"]["webhook"]
    if not w.get("enabled") or not w.get("url"):
        return False, "webhook not configured"
    from urllib.parse import urlparse
    if urlparse(w["url"]).scheme not in ("http", "https"):
        return False, "webhook URL must be http(s)"
    try:
        import urllib.request
        payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
        req = urllib.request.Request(w["url"], data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
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
