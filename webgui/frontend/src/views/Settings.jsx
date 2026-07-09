import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Settings: administrators, password policy, controller
// address/port, license, and the admin audit log.
export default function Settings({ initialTab }) {
  const [tab, setTab] = useState(initialTab || "admins");
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
                  <button className="btn ghost sm danger" onClick={() => remove(a.username)}>Remove</button>
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
  const [err, setErr] = useErr(); const [msg, setMsg] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { api.passwordPolicy().then(setPol).catch((e) => setErr(e.message)); }, []);
  if (!pol) return <div className="empty"><span className="spin" /></div>;
  // The backend policy is pam_pwquality-shaped: { minlen, dcredit, ucredit,
  // lcredit, ocredit }. A NEGATIVE credit means "require at least one of that
  // class"; 0 means no requirement. (The old form bound to min_length/require_*,
  // keys the backend model doesn't have — so it loaded defaults and silently
  // dropped every save. This mirrors generatePassword() and backend/policy.py.)
  const CREDS = [["ucredit", "Require uppercase"], ["lcredit", "Require lowercase"],
                 ["dcredit", "Require digit"], ["ocredit", "Require symbol"]];
  async function save() { setErr(""); setMsg(""); setBusy(true); try { await api.setPasswordPolicy(pol); setMsg("Saved."); } catch (e) { setErr(e.message); } finally { setBusy(false); } }
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      <label className="field"><span>Minimum length</span>
        <input type="number" value={pol.minlen ?? 12}
               onChange={(e) => setPol({ ...pol, minlen: Number(e.target.value) })} /></label>
      {CREDS.map(([k, label]) => (
        <div className="checkrow" key={k}>
          <input id={k} type="checkbox" checked={(pol[k] ?? 0) < 0}
                 onChange={(e) => setPol({ ...pol, [k]: e.target.checked ? -1 : 0 })} />
          <label htmlFor={k}>{label}</label>
        </div>
      ))}
      <button className="btn" style={{ marginTop: 14 }} disabled={busy} onClick={save}>{busy ? <span className="spin" /> : "Save policy"}</button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box" role="alert">{err}</div>}
    </div>
  );
}

function ControllerCfg() {
  const [cfg, setCfg] = useState(null);
  const [ver, setVer] = useState(null);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { api.controllerConfig().then(setCfg).catch((e) => setErr(e.message)); }, []);
  useEffect(() => { api.controllerVersion().then(setVer).catch(() => {}); }, []);
  if (!cfg) return <div className="empty"><span className="spin" /></div>;
  const set = (k) => (e) => setCfg({ ...cfg, [k]: e.target.value });
  async function save() {
    setErr(""); setMsg(""); setBusy(true);
    try {
      const res = await api.setControllerConfig({ hostname: cfg.hostname || "", ip: cfg.ip || "",
        address_mode: cfg.address_mode || "hostname", port: Number(cfg.port) || 9000 });
      let m = "Saved. Existing agents keep their current address until updated.";
      // The controller regenerates the self-signed cert when the address changes;
      // surface that so the admin knows to restart + redistribute trust.
      if (res && (res.cert_regenerated || res.cert_note)) m += " " + (res.cert_note || "");
      if (res && res.cert_warning) setErr(res.cert_warning);
      setMsg(m);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }
  return (
    <div className="card" style={{ maxWidth: 460 }}>
      {ver && (
        <div style={{ marginBottom: 12, fontSize: 12 }}>
          <div className="faint">
            Running from <code>{ver.running_dir}</code>
            {ver.commit_short ? <> @ <code>{ver.commit_short}</code></> : null}
            {ver.branch ? ` (${ver.branch})` : ""}{ver.dirty ? " · uncommitted changes" : ""}
          </div>
          {ver.restart_needed && (
            <div className="error-box" role="alert" style={{ marginTop: 6 }}>
              Code in this directory was updated but the controller hasn’t been restarted —
              restart <code>sysible-backend</code> to load it.
            </div>
          )}
        </div>
      )}
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
      <div className="row" style={{ marginTop: 14, gap: 8 }}>
        <button className="btn" disabled={busy} onClick={save}>{busy ? <span className="spin" /> : "Save"}</button>
        <button className="btn ghost" disabled={busy} title="Rebuild the self-signed TLS certificate so its SAN matches the current hostname/IP, then restart the backend to serve it. Use after changing the address."
          onClick={async () => {
            if (!window.confirm("Regenerate the self-signed TLS certificate for the current address and restart the controller backend? Agents must then trust the new certificate (redistribute trust.crt / re-download the agent bundle).")) return;
            setErr(""); setMsg(""); setBusy(true);
            try {
              const r = await api.regenerateSelfSignedCert();
              setMsg((r && r.message) || "Certificate regenerated; backend restarting.");
            } catch (e) { setErr(e.message); }
            finally { setBusy(false); }
          }}>Regenerate self-signed cert</button>
        <button className="btn ghost" disabled={busy}
          title="Re-mint a fresh agent bundle for the CURRENT controller address/port with a new single-use enrollment token, and download it. Use after changing the controller hostname/IP or regenerating the cert."
          onClick={async () => {
            setErr(""); setMsg(""); setBusy(true);
            try {
              const res = await fetch(api.agentBundleUrl(), { credentials: "include" });
              if (!res.ok) {
                let d = ""; try { d = (await res.json()).detail || ""; } catch { /* not JSON */ }
                throw new Error(d || `Bundle regeneration failed (HTTP ${res.status}).`);
              }
              const blob = await res.blob();
              const cd = res.headers.get("Content-Disposition") || "";
              const m = /filename="?([^"]+)"?/.exec(cd);
              const name = (m && m[1]) || "sysible-agent-bundle.zip";
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url; a.download = name; document.body.appendChild(a); a.click();
              a.remove(); URL.revokeObjectURL(url);
              setMsg("Agent bundle regenerated for the current controller address (a fresh single-use enrollment token was issued). Re-run it on your hosts to enroll or re-point them.");
            } catch (e) { setErr(e.message); }
            finally { setBusy(false); }
          }}>
          {busy ? <span className="spin" /> : "Regenerate agent bundle"}
        </button>
      </div>
      <p className="faint" style={{ marginTop: 6 }}>
        After changing the controller hostname/IP: Save, then <b>Regenerate self-signed cert</b> (reissues for the
        new address and restarts), then <b>Regenerate agent bundle</b> and re-run it on your hosts so they trust and reach the new address.
        (Every bundle is freshly built for the current address with a new single-use enrollment token.)
      </p>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box" role="alert">{err}</div>}
    </div>
  );
}

// A thin progress bar. `value`/`max` give a determinate fill; `indeterminate`
// shows a moving sweep when there's no measurable percentage.
function ProgressBar({ value = 0, max = 1, indeterminate = false, color }) {
  const pct = Math.max(0, Math.min(100, max ? (value / max) * 100 : 0));
  return (
    <div style={{ height: 8, borderRadius: 4, background: "var(--border)", overflow: "hidden", marginTop: 8 }}>
      <div className={indeterminate ? "prog-indef" : ""}
           style={{ width: indeterminate ? "40%" : pct + "%", height: "100%",
                    background: color || "var(--accent)",
                    transition: indeterminate ? "none" : "width .4s ease" }} />
    </div>
  );
}

// Software updates (Settings → Controller): update the controller in place, then
// push the agent to every managed host over its existing check-in. Superuser-only
// (the whole Settings page is). Each button confirms, then shows live progress:
// the controller as a reconnect indicator (it restarts the console mid-update),
// the agents as an "N of M on the new version" bar fed by agent_version reports.
function SoftwareUpdate() {
  const [confirm, setConfirm] = useState(null); // null | "controller" | "agents"
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");
  // ctrl: null | "restarting" | "success" | "failed" | "unconfirmed"
  const [ctrl, setCtrl] = useState(null);
  const [ctrlMsg, setCtrlMsg] = useState("");
  const [restart, setRestart] = useState(null);  // null | "restarting" | "back" | "failed"
  const [agents, setAgents] = useState(null);   // null | {total, updated, ver, done, timedOut}
  const [ctrlLog, setCtrlLog] = useState("");   // live self-update output
  const [agentRows, setAgentRows] = useState(null); // per-host update status list
  const [avail, setAvail] = useState(null);     // null | {controller, agents} from /update-status
  const [checking, setChecking] = useState(false);
  const pollRef = useRef(null);
  const agentPollRef = useRef(null);
  const logRef = useRef(null);
  // Coordinate "Update controller + agents": the controller update confirms
  // success while agents are still applying on their own poll. agentsDoneRef
  // flips true when the agent rollout finishes (or times out); ctrlWaitingRef
  // marks that the controller finished and is HOLDING the sign-out until then.
  const agentsDoneRef = useRef(false);
  const ctrlWaitingRef = useRef(false);
  useEffect(() => () => { [pollRef, agentPollRef].forEach((r) => r.current && clearInterval(r.current)); }, []);
  // Keep the console pinned to the newest output.
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [ctrlLog]);

  const logoutReload = useCallback(async () => {
    try { await api.logout(); } catch { /* ignore */ }
    setTimeout(() => window.location.reload(), 900);
  }, []);

  // Check whether the controller (git-behind) or any agent (build-hash mismatch)
  // has an update available. Does a live git fetch on the controller, so it's an
  // on-demand check (auto once on mount, then via the Re-check button).
  const checkUpdates = useCallback(async () => {
    setChecking(true);
    try { setAvail(await api.updateStatus()); } catch { /* leave last */ }
    finally { setChecking(false); }
  }, []);
  useEffect(() => { checkUpdates(); }, [checkUpdates]);

  async function startController({ keepAgents = false } = {}) {
    setBusy(true); setErr(""); setMsg(""); setConfirm(null); setCtrlMsg(""); setCtrlLog("");
    ctrlWaitingRef.current = false;
    if (!keepAgents) { setAgents(null); setAgentRows(null); agentsDoneRef.current = true; }
    // Baseline: only a status record written at/after we kicked this off counts
    // as THIS run's outcome (the updater writes run/last_update.json). Small
    // margin for clock skew between browser and server.
    const startTs = Math.floor(Date.now() / 1000) - 5;
    try {
      const r = await api.controllerUpdate();
      setMsg(r?.message || "Controller update started.");
      setCtrl("restarting");
      const t0 = Date.now();
      const finish = async (state, detail) => {
        clearInterval(pollRef.current);
        setCtrl(state); setCtrlMsg(detail || "");
        if (state === "success") {
          // Real success: the update ran and the code changed. A controller
          // update ends your session. In "controller + agents" mode, HOLD the
          // sign-out until the agent rollout finishes (or times out), so the
          // operator can watch it complete instead of being kicked to the login
          // screen mid-rollout. Otherwise sign out now.
          if (keepAgents && !agentsDoneRef.current) {
            ctrlWaitingRef.current = true;
            setCtrlMsg((detail ? detail + " " : "") + "Waiting for the agent rollout to finish before signing you out…");
            return;
          }
          await logoutReload();
        }
        // On failure/unconfirmed we deliberately DO NOT sign out — keep the
        // session so the operator can read the reason and retry.
      };
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        // Stream the update's command output (git pull → rsync → rebuild →
        // restart). Unreachable during the brief restart window — keep the last
        // content and resume when it's back.
        try { const lg = await api.controllerUpdateLog(); if (lg && typeof lg.log === "string") setCtrlLog(lg.log); } catch { /* keep last */ }
        // The updater records the true outcome. Read it whenever the backend is
        // reachable (on a failed update the console never restarts, so this is
        // reachable the whole time; on success it's briefly down then returns
        // "success"). This replaces guessing from the restart.
        let rec = null;
        try { rec = await api.controllerUpdateStatus(); } catch { rec = null; }
        if (rec && typeof rec.ts === "number" && rec.ts >= startTs) {
          if (rec.status === "failed") {
            return finish("failed", rec.message || "The update failed on the controller host.");
          }
          if (rec.status === "success") {
            return finish("success", rec.version ? `Updated to ${rec.version}.` : (rec.message || ""));
          }
          // status "running" → keep waiting.
        }
        // Fallback: if we could never confirm (e.g. updating FROM a build that
        // predates this status file), stop guessing after 3 min and say so.
        if (Date.now() - t0 > 180000) {
          return finish("unconfirmed",
            "Couldn't confirm the update from the console. Check the server's update log " +
            "(journalctl -u sysible-self-update). If the version changed, it succeeded.");
        }
      }, 2500);
    } catch (e) { setErr(e.message); setCtrl(null); }
    finally { setBusy(false); }
  }

  // Kick the agent update + N-of-M polling on its OWN interval (so it can run
  // alongside the controller update). Doesn't reset controller state.
  async function kickAgents() {
    agentsDoneRef.current = false;
    const r = await api.updateAgents();
    const ver = r?.version, total = r?.queued || 0;
    setMsg(r?.message || `Agent update queued for ${total} host(s).`);
    if (!ver || !total) {
      // Nothing to roll out — don't make a pending controller sign-out wait.
      agentsDoneRef.current = true;
      if (ctrlWaitingRef.current) { ctrlWaitingRef.current = false; logoutReload(); }
      return;
    }
    setAgents({ total, updated: 0, ver, done: false, timedOut: false });
    const t0 = Date.now();
    if (agentPollRef.current) clearInterval(agentPollRef.current);
    const tick = async () => {
      try {
        const d = await api.agents();
        const list = (d.agents || []).map((a) => ({
          host: a.hostname || a.host_id, env: a.environment || "",
          updated: a.agent_version === ver,
        })).sort((x, y) => (x.updated - y.updated) || x.host.localeCompare(y.host));
        setAgentRows(list);
        const updated = list.filter((a) => a.updated).length;
        const done = updated >= total;
        const timedOut = !done && Date.now() - t0 > 240000;
        setAgents({ total, updated, ver, done, timedOut });
        if (done || timedOut) {
          clearInterval(agentPollRef.current);
          agentsDoneRef.current = true;
          // If the controller update already finished and is holding the
          // sign-out for us, release it now.
          if (ctrlWaitingRef.current) { ctrlWaitingRef.current = false; logoutReload(); }
        }
      } catch { /* transient — keep polling */ }
    };
    agentPollRef.current = setInterval(tick, 4000);
    tick();
  }

  async function startAgents() {
    setBusy(true); setErr(""); setMsg(""); setConfirm(null); setCtrl(null); setCtrlLog(""); setAgentRows(null); setAgents(null);
    try { await kickAgents(); } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  // Combined: kick the agents first (their progress bar starts), then the
  // controller update runs alongside it. The controller restart signs you out;
  // agents keep applying in the background and finish on their own.
  async function startBoth() {
    setBusy(true); setErr(""); setMsg(""); setConfirm(null); setCtrl(null); setCtrlLog(""); setAgents(null); setAgentRows(null);
    try {
      await kickAgents();
      await startController({ keepAgents: true });
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  // Restart the controller backend only (no update). The backend bounces while
  // this web console stays up, so the session survives — we just poll a
  // backend-backed endpoint until it answers again, then report "back".
  async function restartController() {
    setConfirm(null); setErr(""); setMsg(""); setRestart("restarting");
    try {
      await api.controllerRestart();
    } catch {
      // The restart tears down the in-flight request path, so a network error
      // here is expected, not a failure — fall through to polling.
    }
    const deadline = Date.now() + 60000;
    const poll = async () => {
      try {
        await api.controllerUpdateStatus();   // proxied to the backend: resolves once it's up
        setRestart("back");
        setTimeout(() => setRestart((r) => (r === "back" ? null : r)), 6000);
      } catch {
        if (Date.now() < deadline) setTimeout(poll, 2000);
        else setRestart("failed");
      }
    };
    setTimeout(poll, 3000);   // give systemd a moment to bounce it before polling
  }

  async function downloadLog() {
    try {
      const d = await api.controllerUpdateLog(0);   // 0 = full log
      const text = (d && d.log) || "";
      if (!text.trim()) { setErr("No update log yet — run a controller update first."); return; }
      const blob = new Blob([text], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `sysible-update-${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setErr(e.message); }
  }

  const Button = ({ which, label, start }) => (
    confirm === which ? (
      <>
        <button className="btn" onClick={start} disabled={busy}>
          {busy ? <span className="spin" /> : `Yes, ${label.toLowerCase()}`}
        </button>
        <button className="btn ghost" onClick={() => setConfirm(null)} disabled={busy}>Cancel</button>
      </>
    ) : (
      <button className="btn" onClick={() => { setErr(""); setMsg(""); setConfirm(which); }}>{label}</button>
    )
  );

  const showConsole = ctrl || ctrlLog;
  return (
    <div className="card" style={{ maxWidth: 680, marginTop: 16 }}>
      <strong>Software updates</strong>
      <p className="faint" style={{ marginTop: 8 }}>
        Update the controller in place (git pull → redeploy → restart) and push the current
        agent to every managed host. "Update controller + agents" does both — the agents'
        progress shows below while the controller updates. A controller update signs you out
        briefly; agents keep applying in the background.
      </p>

      <UpdatesAvailable avail={avail} checking={checking} onRecheck={checkUpdates} />

      <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: "wrap" }}>
        <Button which="both" label="Update controller + agents" start={startBoth} />
        <Button which="controller" label="Update controller only" start={startController} />
        <Button which="agents" label="Update agents only" start={startAgents} />
      </div>
      {ctrl === "restarting" && (
        <div style={{ marginTop: 8 }}>
          <div className="faint" style={{ fontSize: 12 }}>Updating the controller… confirming the result (this can take 30–90 s).</div>
          <ProgressBar indeterminate />
        </div>
      )}
      {ctrl === "success" && (
        <div style={{ marginTop: 8 }}>
          <div className="ok-text" style={{ fontSize: 13 }}>✓ Controller updated{ctrlMsg ? ` — ${ctrlMsg}` : ""}
            {!ctrlWaitingRef.current && " Signing you out…"}</div>
        </div>
      )}
      {ctrl === "failed" && (
        <div style={{ marginTop: 8 }}>
          <div className="error-box" style={{ fontSize: 13 }}>✗ Update failed — {ctrlMsg}</div>
          <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
            Nothing was changed and you're still signed in. Fix the cause on the controller host, then try again.
          </div>
        </div>
      )}
      {ctrl === "unconfirmed" && (
        <div style={{ marginTop: 8 }}>
          <div className="faint" style={{ fontSize: 13 }}>⚠ {ctrlMsg}</div>
          <button className="btn sm" style={{ marginTop: 6 }}
                  onClick={async () => { try { await api.logout(); } catch { /* ignore */ } window.location.reload(); }}>
            Sign in again
          </button>
        </div>
      )}
      {showConsole && (
        <div style={{ marginTop: 8 }}>
          <div className="spread" style={{ marginBottom: 4 }}>
            <span className="faint" style={{ fontSize: 11 }}>Update output (run/last_update.log)</span>
            <button className="btn ghost sm" onClick={downloadLog}>Download log</button>
          </div>
          <pre ref={logRef} style={{ margin: 0, maxHeight: 240, overflow: "auto", background: "#0d1117",
                 color: "#c9d1d9", border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px",
                 fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12, lineHeight: 1.5,
                 whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {ctrlLog || "Waiting for the controller to start the update…"}
          </pre>
        </div>
      )}

      {agents && (
        <div style={{ marginTop: 12 }}>
          <div className="spread" style={{ fontSize: 12 }}>
            <span className="faint">
              {agents.done ? "✓ all agents updated"
                : agents.timedOut ? "still updating — offline hosts apply on reconnect"
                : "Updating agents…"}
            </span>
            <span className="faint">{agents.updated} / {agents.total}</span>
          </div>
          <ProgressBar value={agents.updated} max={agents.total}
                       color={agents.done ? "#4ec07a" : undefined} />
        </div>
      )}
      {agentRows && agentRows.length > 0 && (
        <div style={{ marginTop: 8, maxHeight: 220, overflow: "auto", border: "1px solid var(--border)",
                      borderRadius: 8, padding: "6px 8px" }}>
          {agentRows.map((a) => (
            <div key={a.host} className="spread" style={{ fontSize: 12, padding: "3px 0" }}>
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="dot" style={{ background: a.updated ? "#4ec07a" : "#e0a83a" }} />
                {a.host}{a.env ? <span className="faint" style={{ fontSize: 11 }}>{a.env}</span> : null}
              </span>
              <span className="faint">{a.updated ? "updated ✓" : "applying on next check-in…"}</span>
            </div>
          ))}
        </div>
      )}

      <div className="row" style={{ marginTop: 12, gap: 8, alignItems: "center" }}>
        <button className="btn ghost sm" onClick={downloadLog}>Download last update log</button>
        <span className="faint" style={{ fontSize: 11 }}>the most recent controller update's full output</span>
      </div>
      <p className="faint" style={{ marginTop: 10, marginBottom: 0 }}>
        Tip: update the controller first, then update agents so hosts report the latest metrics.
      </p>

      <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
        <div className="spread" style={{ alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 13 }}>Restart controller</strong>
            <div className="faint" style={{ fontSize: 12 }}>
              Bounce the backend service without updating — no shell needed. The console stays up;
              actions blip for a few seconds while it comes back.
            </div>
          </div>
          {restart !== "restarting" && (
            <div className="row" style={{ gap: 8 }}>
              <Button which="restart" label="Restart controller" start={restartController} />
            </div>
          )}
        </div>
        {restart === "restarting" && (
          <div style={{ marginTop: 8 }}>
            <div className="faint" style={{ fontSize: 12 }}>Restarting the controller… reconnecting.</div>
            <ProgressBar indeterminate />
          </div>
        )}
        {restart === "back" && <div className="ok-text" style={{ marginTop: 8, fontSize: 13 }}>✓ Controller is back.</div>}
        {restart === "failed" && (
          <div className="error-box" style={{ marginTop: 8, fontSize: 13 }}>
            The controller didn't answer within 60&nbsp;s. Check it on the host:
            <span className="mono"> systemctl status sysible-backend</span>.
          </div>
        )}
      </div>

      {msg && <div className="ok-text" style={{ marginTop: 10 }}>{msg}</div>}
      {err && <div className="error-box" style={{ marginTop: 10 }}>{err}</div>}
    </div>
  );
}

// "Updates available" banner at the top of Software updates: a compact read-out
// of whether the controller is behind its git remote and how many agents are on
// an older build than the controller ships. Purely informational — the action
// buttons below actually apply the updates.
function UpdatesAvailable({ avail, checking, onRecheck }) {
  const c = avail?.controller || {};
  const a = avail?.agents || {};
  const ctrlBehind = c.checked && c.available;
  const agentsBehind = (a.outdated_count || 0) > 0;
  const anything = ctrlBehind || agentsBehind;
  // Colour the strip: green when confirmed all-current, amber when something's
  // behind, neutral while we couldn't fully check.
  const known = avail && (c.checked || a.current_version);
  const bg = anything ? "rgba(224,168,58,0.10)" : known ? "rgba(78,192,122,0.10)" : "var(--panel-2, rgba(255,255,255,0.02))";
  const border = anything ? "#e0a83a" : known ? "#4ec07a" : "var(--border)";

  return (
    <div style={{ marginTop: 12, marginBottom: 4, background: bg, border: `1px solid ${border}`,
                  borderRadius: 8, padding: "8px 12px" }}>
      <div className="spread" style={{ alignItems: "center" }}>
        <strong style={{ fontSize: 13 }}>
          {avail == null ? "Checking for updates…"
            : anything ? "Updates available"
            : known ? "✓ Up to date" : "Update status"}
        </strong>
        <button className="btn ghost sm" onClick={onRecheck} disabled={checking}>
          {checking ? <span className="spin" /> : "Re-check"}
        </button>
      </div>
      {avail != null && (
        <div style={{ fontSize: 12, marginTop: 6, display: "grid", gap: 3 }}>
          {/* Controller */}
          {c.checked ? (
            c.available
              ? <span><span className="dot" style={{ background: "#e0a83a", marginRight: 6 }} />
                  A controller update is available{c.branch ? ` on ${c.branch}` : ""}
                  {c.current && c.latest ? <span className="faint"> ({c.current} → {c.latest})</span> : null}</span>
              : <span><span className="dot" style={{ background: "#4ec07a", marginRight: 6 }} />
                  Controller is current{c.current ? <span className="faint"> ({c.current})</span> : null}</span>
          ) : (
            <span className="faint"><span className="dot" style={{ background: "var(--text-faint)", marginRight: 6 }} />
              Controller: couldn't check{c.reason ? ` — ${c.reason}` : ""}
              {c.current ? ` (at ${c.current})` : ""}</span>
          )}
          {/* Agents */}
          {agentsBehind
            ? <span><span className="dot" style={{ background: "#e0a83a", marginRight: 6 }} />
                <strong>{a.outdated_count}</strong> of {a.total} agent{a.total === 1 ? "" : "s"} on an older build</span>
            : <span><span className="dot" style={{ background: "#4ec07a", marginRight: 6 }} />
                {a.total ? `All ${a.total} agent${a.total === 1 ? "" : "s"} on the current build` : "No agents enrolled"}</span>}
          {a.unknown_count ? (
            <span className="faint" style={{ marginLeft: 18 }}>
              {a.unknown_count} host{a.unknown_count === 1 ? " hasn't" : "s haven't"} reported a version yet</span>
          ) : null}
        </div>
      )}
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
    e.preventDefault();
    // Enrolled agents PIN the controller's current certificate. Replacing it and
    // restarting will break TLS verification for every already-enrolled agent
    // until the new trust bundle is redistributed to them out-of-band. Make the
    // operator acknowledge that before proceeding — this is otherwise a silent
    // fleet-wide outage.
    if (!window.confirm(
      "Replacing the controller certificate will break the connection for ALL " +
      "already-enrolled agents until you redistribute the new trust bundle to them.\n\n" +
      "After installing, download the Trust Certificate and push it to every host " +
      "(e.g. re-run the agent bundle, or copy it to the pinned cert path), then restart " +
      "the controller.\n\nContinue?")) return;
    setBusy(true); setErr(""); setMsg("");
    try { await api.installCertificate(cert, key, chain); setMsg("Certificate installed. Download the Trust Certificate above and redistribute it to every agent host, then restart the controller for it to take effect."); load(); }
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
        <div className="error-box" style={{ marginTop: 8 }} role="note">
          ⚠ Enrolled agents pin the current certificate. After replacing it, download the
          Trust Certificate and redistribute it to every host, then restart the controller —
          otherwise agents will fail to connect until they have the new bundle.
        </div>
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
