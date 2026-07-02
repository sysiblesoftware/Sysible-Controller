import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Fleet alerting config: email + webhook channels and a small set of rules.
// The controller's evaluator thread notifies on threshold crossings (fire-once)
// and again when a condition clears.
export default function Alerts() {
  const [cfg, setCfg] = useState(null);
  const [pw, setPw] = useState("");          // new SMTP password (blank = keep)
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => { api.alertsGet().then(setCfg).catch((e) => setErr(e.message)); }, []);
  if (!cfg) return <div className="empty"><span className="spin" /></div>;

  const email = cfg.channels.email, webhook = cfg.channels.webhook;
  const setEmail = (k, v) => setCfg({ ...cfg, channels: { ...cfg.channels, email: { ...email, [k]: v } } });
  const setWebhook = (k, v) => setCfg({ ...cfg, channels: { ...cfg.channels, webhook: { ...webhook, [k]: v } } });
  const setRule = (k, field, v) => setCfg({ ...cfg, rules: { ...cfg.rules, [k]: { ...cfg.rules[k], [field]: v } } });

  async function save() {
    setBusy("save"); setErr(""); setMsg("");
    const body = JSON.parse(JSON.stringify(cfg));
    if (pw) body.channels.email.password = pw;   // only send if changed
    try { const d = await api.alertsSet(body); setCfg(d); setPw(""); setMsg("Saved."); }
    catch (e) { setErr(e.message); } finally { setBusy(""); }
  }
  async function test() {
    setBusy("test"); setErr(""); setMsg("");
    try {
      const d = await api.alertsTest();
      setMsg("Test: " + Object.entries(d.results).map(([c, r]) => `${c} ${r.ok ? "ok" : "failed: " + r.detail}`).join(" · "));
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  return (
    <div style={{ maxWidth: 640 }}>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <label className="checkrow" style={{ margin: 0 }}>
          <input type="checkbox" checked={!!email.enabled} onChange={(e) => setEmail("enabled", e.target.checked)} />
          <strong>Email (SMTP)</strong></label>
        {email.enabled && (
          <div style={{ marginTop: 8 }}>
            <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
              <label className="field" style={{ flex: 2, minWidth: 180 }}><span>SMTP host</span>
                <input value={email.smtp_host || ""} onChange={(e) => setEmail("smtp_host", e.target.value)} placeholder="smtp.example.com" /></label>
              <label className="field" style={{ width: 90 }}><span>Port</span>
                <input type="number" value={email.smtp_port || 587} onChange={(e) => setEmail("smtp_port", Number(e.target.value))} /></label>
              <label className="checkrow" style={{ alignSelf: "flex-end" }}>
                <input type="checkbox" checked={!!email.use_tls} onChange={(e) => setEmail("use_tls", e.target.checked)} /><span>STARTTLS</span></label>
            </div>
            <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
              <label className="field" style={{ flex: 1, minWidth: 160 }}><span>Username (optional)</span>
                <input value={email.username || ""} onChange={(e) => setEmail("username", e.target.value)} /></label>
              <label className="field" style={{ flex: 1, minWidth: 160 }}><span>Password {email.has_password ? "(stored — leave blank to keep)" : ""}</span>
                <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={email.has_password ? "••••••" : ""} /></label>
            </div>
            <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
              <label className="field" style={{ flex: 1, minWidth: 160 }}><span>From address</span>
                <input value={email.from_addr || ""} onChange={(e) => setEmail("from_addr", e.target.value)} placeholder="sysible@example.com" /></label>
              <label className="field" style={{ flex: 2, minWidth: 200 }}><span>To (comma-separated)</span>
                <input value={email.to_addrs || ""} onChange={(e) => setEmail("to_addrs", e.target.value)} placeholder="ops@example.com, oncall@example.com" /></label>
            </div>
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <label className="checkrow" style={{ margin: 0 }}>
          <input type="checkbox" checked={!!webhook.enabled} onChange={(e) => setWebhook("enabled", e.target.checked)} />
          <strong>Webhook (Slack-compatible)</strong></label>
        {webhook.enabled && (
          <label className="field" style={{ marginTop: 8 }}><span>Webhook URL</span>
            <input value={webhook.url || ""} onChange={(e) => setWebhook("url", e.target.value)} placeholder="https://hooks.slack.com/services/…" /></label>
        )}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <strong>Rules</strong>
        <div style={{ marginTop: 8 }}>
          {Object.entries(cfg.rule_meta).map(([k, m]) => {
            const r = cfg.rules[k] || {};
            return (
              <div key={k} className="row" style={{ gap: 10, alignItems: "center", padding: "5px 0", borderTop: "1px solid var(--border)" }}>
                <label className="checkrow" style={{ margin: 0, flex: 1 }}>
                  <input type="checkbox" checked={!!r.enabled} onChange={(e) => setRule(k, "enabled", e.target.checked)} />
                  <span>{m.label}</span></label>
                {m.has_threshold && (
                  <input type="number" style={{ width: 80 }} value={r.threshold ?? 0}
                         onChange={(e) => setRule(k, "threshold", Number(e.target.value))} disabled={!r.enabled} />
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="row" style={{ gap: 8 }}>
        <button className="btn" disabled={busy} onClick={save}>{busy === "save" ? <span className="spin" /> : "Save"}</button>
        <button className="btn ghost" disabled={busy} onClick={test}>{busy === "test" ? <span className="spin" /> : "Send test alert"}</button>
      </div>
      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
        The controller evaluates rules every ~5 min. Security-update, reboot, and cert rules use the
        patch/posture sweeps, so they need a Dashboard visit or a scheduled scan to have data. The SMTP
        password is encrypted at rest and never returned to the browser.
      </p>
    </div>
  );
}
