import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Settings: administrators, password policy, controller
// address/port, license, and the admin audit log.
export default function Settings() {
  const [tab, setTab] = useState("admins");
  return (
    <div>
      <div className="tabs" style={{ marginBottom: 16 }}>
        {[["admins", "Administrators"], ["me", "My Account"], ["policy", "Password Policy"],
          ["controller", "Controller"], ["tls", "TLS / Certificates"], ["license", "License"], ["audit", "Audit Log"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === "admins" && <Admins />}
      {tab === "me" && <MyAccount />}
      {tab === "policy" && <PasswordPolicy />}
      {tab === "controller" && <><ControllerCfg /><SoftwareUpdate /></>}
      {tab === "tls" && <Tls />}
      {tab === "license" && <License />}
      {tab === "audit" && <Audit />}
    </div>
  );
}

function useErr() { const [err, setErr] = useState(""); return [err, setErr]; }

// Strong random password: crypto RNG over an unambiguous charset (no 0/O/1/l/I).
// Generate a password that ALWAYS satisfies the admin policy: one guaranteed
// char from each required class, padded to at least the policy's minlen, then
// shuffled — mirroring backend/policy.py so the value never fails the policy
// check the Add/Reset request runs. Without a policy it guarantees all four
// classes (the default admin policy), which is always safe.
function generatePassword(policy) {
  const p = policy || {};
  const lower = "abcdefghijkmnpqrstuvwxyz";   // no ambiguous l
  const upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";   // no ambiguous I, O
  const digits = "23456789";                   // no ambiguous 0, 1
  const symbols = "!@#$%^&*-_=+";
  const pool = lower + upper + digits + symbols;
  const rnd = (n) => crypto.getRandomValues(new Uint32Array(1))[0] % n;
  const pick = (set) => set[rnd(set.length)];

  const required = [];
  if ((p.lcredit ?? -1) < 0) required.push(lower);
  if ((p.ucredit ?? -1) < 0) required.push(upper);
  if ((p.dcredit ?? -1) < 0) required.push(digits);
  if ((p.ocredit ?? -1) < 0) required.push(symbols);

  const length = Math.max(16, p.minlen || 12);
  const chars = required.map(pick);
  while (chars.length < length) chars.push(pick(pool));
  for (let i = chars.length - 1; i > 0; i--) {   // Fisher–Yates shuffle
    const j = rnd(i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

function Admins() {
  const [list, setList] = useState([]);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");
  const [modal, setModal] = useState(null);   // { mode: "add"|"reset", username }

  const load = () => api.admins().then((d) => setList(d.administrators || [])).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);

  async function remove(name) {
    if (!window.confirm(`Remove administrator ${name}? This cannot be undone.`)) return;
    setErr(""); setMsg("");
    try { await api.removeAdmin(name); setMsg(`Removed ${name}.`); load(); } catch (e) { setErr(e.message); }
  }

  async function toggleSudo(a) {
    setErr(""); setMsg("");
    const next = !a.sudo_connect;
    try {
      await api.setAdminSudoConnect(a.username, next);
      setMsg(`Sudo on Connect ${next ? "enabled" : "disabled"} for ${a.username}.`
        + (next ? " They must re-log in for it to apply." : ""));
      load();
    } catch (e) { setErr(e.message); }
  }

  async function setRole(a, role) {
    if (role === a.role) return;
    setErr(""); setMsg("");
    try {
      await api.setAdminRole(a.username, role);
      setMsg(`${a.username} is now ${role}. They must re-log in for the new role to fully apply.`);
      load();
    } catch (e) { setErr(e.message); load(); }
  }

  function onDone(text) { setModal(null); setMsg(text); setErr(""); load(); }

  return (
    <div>
      <div className="spread" style={{ marginBottom: 12 }}>
        <strong>Administrators</strong>
        <button className="btn sm" onClick={() => { setMsg(""); setErr(""); setModal({ mode: "add" }); }}>+ Add Administrator</button>
      </div>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <table>
          <thead><tr><th>Username</th><th>Role</th><th>Sudo on Connect</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
          <tbody>
            {list.length === 0 && <tr><td colSpan={4} className="faint" style={{ padding: 16 }}>No administrators.</td></tr>}
            {list.map((a) => (
              <tr key={a.username}>
                <td style={{ fontWeight: 600 }}>{a.username}</td>
                <td>
                  <select value={a.role} onChange={(e) => setRole(a, e.target.value)}
                          title="Promote or demote this administrator (they re-log in for it to fully apply)">
                    <option value="superuser">superuser</option>
                    <option value="sysadmin">sysadmin</option>
                    <option value="auditor">auditor</option>
                  </select>
                </td>
                <td>{a.sudo_connect
                  ? <span className="ok-text">Yes</span>
                  : <span className="faint">No</span>}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn ghost sm" style={{ marginRight: 6 }}
                          onClick={() => toggleSudo(a)}
                          title="Grant or revoke this account's Sysible Connect 'Send sudo password' button">
                    {a.sudo_connect ? "Revoke sudo" : "Grant sudo"}</button>
                  <button className="btn ghost sm" style={{ marginRight: 6 }}
                          onClick={() => { setMsg(""); setErr(""); setModal({ mode: "reset", username: a.username }); }}>
                    Reset password…</button>
                  <button className="btn ghost sm" onClick={() => remove(a.username)}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {msg && <div className="ok-text" style={{ marginTop: 10 }}>{msg}</div>}
      {err && <div className="error-box">{err}</div>}

      {modal && <AdminModal mode={modal.mode} username={modal.username}
                            onClose={() => setModal(null)} onDone={onDone} />}
    </div>
  );
}

// Shared dialog for creating an administrator and resetting one's password.
// Both flows offer a one-click strong-password generator with show/copy, so an
// "initial password" is always easy to produce and hand off.
function AdminModal({ mode, username: fixedUser, onClose, onDone }) {
  const isAdd = mode === "add";
  const [username, setUsername] = useState(fixedUser || "");
  const [role, setRole] = useState("sysadmin");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function gen() {
    let policy = null;
    try { policy = await api.passwordPolicy(); } catch { /* fall back to all-classes default */ }
    const p = generatePassword(policy);
    setPw(p); setConfirm(p); setShow(true); setErr("");
  }
  function copy() { navigator.clipboard?.writeText(pw).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }); }

  async function submit() {
    if (isAdd && !username.trim()) { setErr("Username is required."); return; }
    if (!pw) { setErr("Enter or generate a password."); return; }
    if (pw !== confirm) { setErr("Passwords don't match."); return; }
    setBusy(true); setErr("");
    try {
      if (isAdd) { await api.addAdmin(username.trim(), pw, role); onDone(`Added ${username.trim()}.`); }
      else { await api.resetAdminPassword(fixedUser, pw); onDone(`Password reset for ${fixedUser}. They must change it at next login.`); }
    } catch (e) { setErr(e.message); setBusy(false); }
  }

  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ maxWidth: 440 }}>
        <h3>{isAdd ? "Add Administrator" : `Reset password — ${fixedUser}`}</h3>

        {isAdd && (
          <>
            <label className="field"><span>Username</span>
              <input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} placeholder="e.g. alice" /></label>
            <label className="field"><span>Role</span>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="sysadmin">Sysadmin — manages the fleet</option>
                <option value="superuser">Superuser — also manages administrators</option>
                <option value="auditor">Auditor — read-only (dashboard, performance, activity)</option>
              </select></label>
          </>
        )}

        <label className="field"><span>{isAdd ? "Initial password" : "New password"}</span>
          <div className="row" style={{ gap: 8 }}>
            <input style={{ flex: 1 }} type={show ? "text" : "password"} value={pw}
                   onChange={(e) => setPw(e.target.value)} placeholder="Type or generate" />
            <button type="button" className="btn ghost sm" onClick={() => setShow((s) => !s)} title={show ? "Hide" : "Show"}>{show ? "Hide" : "Show"}</button>
            <button type="button" className="btn ghost sm" onClick={copy} disabled={!pw} title="Copy">{copied ? "Copied ✓" : "Copy"}</button>
          </div>
        </label>
        <label className="field"><span>Confirm password</span>
          <input type={show ? "text" : "password"} value={confirm}
                 onChange={(e) => setConfirm(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") submit(); }} placeholder="Re-enter" /></label>

        <button type="button" className="btn ghost sm" style={{ marginTop: 8 }} onClick={gen}>⚄ Generate strong password</button>

        <p className="faint" style={{ marginTop: 12, marginBottom: 0 }}>
          {isAdd
            ? "The new administrator must change this password at first login. Copy it now — it isn't shown again."
            : "The administrator must change this password at next login. Copy it now to hand it off."}
        </p>

        {err && <div className="error-box">{err}</div>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" disabled={busy} onClick={submit}>
            {busy ? <span className="spin" /> : (isAdd ? "Create Administrator" : "Reset Password")}</button>
        </div>
      </div>
    </div>
  );
}

function PasswordPolicy() {
  const [pol, setPol] = useState(null);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");
  useEffect(() => { api.passwordPolicy().then(setPol).catch((e) => setErr(e.message)); }, []);
  if (!pol) return <div className="empty"><span className="spin" /></div>;
  const set = (k) => (e) => setPol({ ...pol, [k]: e.target.type === "checkbox" ? e.target.checked : Number(e.target.value) });
  async function save() { setErr(""); setMsg(""); try { await api.setPasswordPolicy(pol); setMsg("Saved."); } catch (e) { setErr(e.message); } }
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      <label className="field"><span>Minimum length</span>
        <input type="number" value={pol.min_length ?? 12} onChange={set("min_length")} /></label>
      {["require_upper", "require_lower", "require_digit", "require_symbol"].map((k) => (
        <div className="checkrow" key={k}>
          <input id={k} type="checkbox" checked={Boolean(pol[k])} onChange={set(k)} />
          <label htmlFor={k}>{k.replace("require_", "Require ")}</label>
        </div>
      ))}
      <button className="btn" style={{ marginTop: 14 }} onClick={save}>Save policy</button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}

function ControllerCfg() {
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");
  useEffect(() => { api.controllerConfig().then(setCfg).catch((e) => setErr(e.message)); }, []);
  if (!cfg) return <div className="empty"><span className="spin" /></div>;
  const set = (k) => (e) => setCfg({ ...cfg, [k]: e.target.value });
  async function save() {
    setErr(""); setMsg("");
    try {
      await api.setControllerConfig({ hostname: cfg.hostname || "", ip: cfg.ip || "",
        address_mode: cfg.address_mode || "hostname", port: Number(cfg.port) || 9000 });
      setMsg("Saved. Existing agents keep their current address until updated.");
    } catch (e) { setErr(e.message); }
  }
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      <label className="field"><span>Address mode</span>
        <select value={cfg.address_mode || "hostname"} onChange={set("address_mode")}>
          <option value="hostname">hostname</option><option value="ip">ip</option>
        </select></label>
      <label className="field"><span>Hostname</span><input value={cfg.hostname || ""} onChange={set("hostname")} /></label>
      <label className="field"><span>IP</span>
        <div className="row"><input style={{ flex: 1 }} value={cfg.ip || ""} onChange={set("ip")} />
          <button className="btn ghost sm" type="button" onClick={async () => {
            try { const d = await api.localIps(); const ip = (d.ips || [])[0];
              if (ip) setCfg((c) => ({ ...c, ip })); } catch (e) { setErr(e.message); } }}>Detect Local IPs</button></div>
      </label>
      <label className="field"><span>Port</span><input type="number" value={cfg.port || 9000} onChange={set("port")} /></label>
      <button className="btn" style={{ marginTop: 14 }} onClick={save}>Save</button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}

// Software updates (Settings → Controller): update the controller in place, then
// push the agent to every managed host over its existing check-in. Superuser-only
// (the whole Settings page is). Each button has a confirm step.
function SoftwareUpdate() {
  const [confirm, setConfirm] = useState(null); // null | "controller" | "agents"
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");

  async function run(which) {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = which === "controller" ? await api.controllerUpdate() : await api.updateAgents();
      setConfirm(null);
      setMsg(r?.message || (which === "controller" ? "Controller update started." : "Agent update queued."));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  const Button = ({ which, label }) => (
    confirm === which ? (
      <>
        <button className="btn" onClick={() => run(which)} disabled={busy}>
          {busy ? <span className="spin" /> : `Yes, ${label.toLowerCase()}`}
        </button>
        <button className="btn ghost" onClick={() => setConfirm(null)} disabled={busy}>Cancel</button>
      </>
    ) : (
      <button className="btn" onClick={() => { setErr(""); setMsg(""); setConfirm(which); }}>{label}</button>
    )
  );

  return (
    <div className="card" style={{ maxWidth: 460, marginTop: 16 }}>
      <strong>Software updates</strong>
      <p className="faint" style={{ marginTop: 8 }}>
        Update the controller in place (git pull → redeploy → restart), then push the
        current agent to every managed host over its existing check-in — no SSH or
        re-enrollment. Each restarts itself; a controller update signs you out briefly.
      </p>
      <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: "wrap" }}>
        <Button which="controller" label="Update controller" />
      </div>
      <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
        <Button which="agents" label="Update agents" />
      </div>
      <p className="faint" style={{ marginTop: 10, marginBottom: 0 }}>
        Tip: update the controller first, then update agents so hosts report the latest metrics.
      </p>
      {msg && <div className="ok-text" style={{ marginTop: 10 }}>{msg}</div>}
      {err && <div className="error-box" style={{ marginTop: 10 }}>{err}</div>}
    </div>
  );
}

function MyAccount() {
  const [cur, setCur] = useState(""); const [nu, setNu] = useState("");
  const [np, setNp] = useState(""); const [np2, setNp2] = useState("");
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");
  async function save() {
    setErr(""); setMsg("");
    if (np && np !== np2) { setErr("New passwords don't match."); return; }
    if (!cur) { setErr("Enter your current password to confirm."); return; }
    try { await api.changeMyCredentials(cur, nu.trim(), np); setMsg("Credentials updated."); setCur(""); setNp(""); setNp2(""); }
    catch (e) { setErr(e.message); }
  }
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      <strong>Change My Own Credentials</strong>
      <label className="field"><span>Current password</span><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} /></label>
      <label className="field"><span>New username (optional)</span><input value={nu} onChange={(e) => setNu(e.target.value)} /></label>
      <label className="field"><span>New password (optional)</span><input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label>
      <label className="field"><span>Confirm new password</span><input type="password" value={np2} onChange={(e) => setNp2(e.target.value)} /></label>
      <button className="btn" style={{ marginTop: 14 }} onClick={save}>Save My Credentials</button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}

function Tls() {
  const [info, setInfo] = useState(null);
  const [cert, setCert] = useState(null); const [key, setKey] = useState(null); const [chain, setChain] = useState(null);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState(""); const [busy, setBusy] = useState(false);
  function load() { api.tlsInfo().then(setInfo).catch((e) => setErr(e.message)); }
  useEffect(() => { load(); }, []);
  async function install(e) {
    e.preventDefault(); setBusy(true); setErr(""); setMsg("");
    try { await api.installCertificate(cert, key, chain); setMsg("Certificate installed. Restart the controller for it to take effect."); load(); }
    catch (e2) { setErr(e2.message); }
    finally { setBusy(false); }
  }
  return (
    <div style={{ maxWidth: 560 }}>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="spread"><strong>Current TLS certificate</strong><button className="btn ghost sm" onClick={load}>Refresh</button></div>
        <div className="muted mono" style={{ fontSize: 12.5, marginTop: 8 }}>
          {info ? <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(info, null, 2)}</pre> : "Loading…"}
        </div>
        <a className="btn sm ghost" style={{ marginTop: 10 }} href={api.trustCertUrl()}>Download Trust Certificate</a>
      </div>
      <form className="card" onSubmit={install}>
        <strong>Install Custom Certificate</strong>
        <p className="faint" style={{ marginTop: 4 }}>Upload a certificate + private key (and optional chain) to replace the self-signed cert.</p>
        <label className="field"><span>Certificate (.crt/.pem) *</span><input type="file" onChange={(e) => setCert(e.target.files[0] || null)} /></label>
        <label className="field"><span>Private key (.key) *</span><input type="file" onChange={(e) => setKey(e.target.files[0] || null)} /></label>
        <label className="field"><span>Chain (optional)</span><input type="file" onChange={(e) => setChain(e.target.files[0] || null)} /></label>
        <button className="btn" style={{ marginTop: 14 }} disabled={busy || !cert || !key}>{busy ? <span className="spin" /> : "Install Certificate"}</button>
        {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
        {err && <div className="error-box">{err}</div>}
      </form>
    </div>
  );
}

function License() {
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useErr();
  useEffect(() => { api.license().then(setCfg).catch((e) => setErr(e.message)); }, []);
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      <strong>License</strong>
      <div className="muted" style={{ marginTop: 8 }}>
        {cfg ? <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(cfg, null, 2)}</pre> : "Loading…"}
      </div>
      <div className="faint" style={{ marginTop: 8 }}>This is the Community edition. License entry applies to paid editions.</div>
      {err && <div className="error-box">{err}</div>}
    </div>
  );
}

function Audit() {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useErr();
  useEffect(() => { api.auditLog(200).then((d) => setRows(d.audit || [])).catch((e) => setErr(e.message)); }, []);
  if (err) return <div className="error-box">{err}</div>;
  if (rows.length === 0) return <div className="empty">No audit entries.</div>;
  return (
    <table>
      <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={r.id ?? i}>
            <td className="faint mono">{r.timestamp || r.time || ""}</td>
            <td>{r.actor || r.admin || ""}</td>
            <td>{r.action || r.event || ""}</td>
            <td>{r.target || r.detail || r.username || ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
