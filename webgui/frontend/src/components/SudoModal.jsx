import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const ALL = "__all__";
const ALL_LABEL = "All hosts (fleet default)";

export default function SudoModal({ onClose }) {
  const [scope, setScope] = useState(ALL);
  const [hosts, setHosts] = useState([]);
  const [stored, setStored] = useState([]);
  const [encAvail, setEncAvail] = useState(true);
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const [s, h] = await Promise.all([api.sudoStatus(), api.hosts()]);
      setStored(s.scopes || []);
      setEncAvail(s.encryption_available);
      setHosts(h.hosts || []);
    } catch (e) { setErr(e.message); }
  }
  useEffect(() => { refresh(); }, []);

  const isStored = stored.includes(scope);

  async function save() {
    setErr(""); setMsg("");
    if (pw !== confirm) { setErr("Passwords don't match."); return; }
    if (!pw) { setErr("Enter a password."); return; }
    setBusy(true);
    try {
      await api.setSudo(pw, scope);
      setMsg("Sudo password saved (encrypted on the controller).");
      setPw(""); setConfirm("");
      refresh();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function clearStored() {
    setErr(""); setMsg("");
    setBusy(true);
    try {
      await api.clearSudo(scope);
      setMsg("Stored password cleared for this scope.");
      refresh();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <h3>My Sudo Password</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Used to elevate commands on hosts whose sudo requires a password. It's
          encrypted at rest on the controller and never leaves it except to the
          target host when elevating.
        </p>

        <label className="field">
          <span>Applies to</span>
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value={ALL}>{ALL_LABEL}</option>
            {hosts.map((h) => (
              <option key={h.id} value={h.label}>{h.label}</option>
            ))}
          </select>
        </label>

        <div style={{ margin: "10px 0", fontSize: 13 }}>
          {isStored
            ? <span className="ok-text">✓ A sudo password is currently stored for this scope.</span>
            : <span className="faint">No sudo password stored for this scope yet.</span>}
        </div>

        {!encAvail && (
          <div className="error-box">Encryption isn't available on the controller —
            a password can't be stored safely. Install the `cryptography` package.</div>
        )}

        <label className="field">
          <span>New password</span>
          <input type={show ? "text" : "password"} value={pw}
                 onChange={(e) => setPw(e.target.value)} placeholder="your sudo password" />
        </label>
        <label className="field">
          <span>Confirm</span>
          <input type={show ? "text" : "password"} value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} />
        </label>
        <div className="checkrow">
          <input id="showpw" type="checkbox" checked={show} onChange={(e) => setShow(e.target.checked)} />
          <label htmlFor="showpw">Show</label>
        </div>

        {msg && <div style={{ marginTop: 10 }} className="ok-text">{msg}</div>}
        {err && <div className="error-box">{err}</div>}

        <div className="spread" style={{ marginTop: 18 }}>
          <button className="btn ghost" disabled={busy || !isStored} onClick={clearStored}>
            Clear stored
          </button>
          <div className="row">
            <button className="btn ghost" onClick={onClose}>Close</button>
            <button className="btn" disabled={busy || !encAvail} onClick={save}>
              {busy ? <span className="spin" /> : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
