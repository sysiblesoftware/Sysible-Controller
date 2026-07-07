import React, { useState } from "react";
import { api } from "../api.js";

// Shown when the account is flagged must_change_password (a fresh admin, or one
// reset by a superuser / `sysible_controller reset-admin`). It BLOCKS the app —
// the temporary password can't be used past first login until it's rotated.
export default function ForcePasswordChange({ username, onDone, onLogout }) {
  const [current, setCurrent] = useState("");
  const [np, setNp] = useState("");
  const [np2, setNp2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr("");
    if (np !== np2) { setErr("New passwords don't match."); return; }
    if (!np) { setErr("Choose a new password."); return; }
    setBusy(true);
    try {
      // Keep the same username (pass "" so the server keeps it); just rotate the
      // password. On success the server clears the forced-change flag.
      await api.changeMyCredentials(current, "", np);
      onDone();
    } catch (e2) {
      setErr(e2.message || "Could not change password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="brand">
          <div className="brand-mark">S</div>
          <h1>Set a new password</h1>
        </div>
        <p className="muted" style={{ marginTop: 4 }}>
          Your account <strong>{username}</strong> is using a temporary password.
          Choose a new one to continue.
        </p>

        <label className="field">
          <span>Current (temporary) password</span>
          <input type="password" value={current} autoFocus autoComplete="current-password"
                 onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label className="field">
          <span>New password</span>
          <input type="password" value={np} autoComplete="new-password"
                 onChange={(e) => setNp(e.target.value)} />
        </label>
        <label className="field">
          <span>Confirm new password</span>
          <input type="password" value={np2} autoComplete="new-password"
                 onChange={(e) => setNp2(e.target.value)} />
        </label>

        {err && <div className="error-box" role="alert">{err}</div>}

        <button className="btn full" style={{ marginTop: 18 }}
                disabled={busy || !current || !np || !np2}>
          {busy ? <span className="spin" /> : "Set password & continue"}
        </button>
        <button type="button" className="btn ghost full" style={{ marginTop: 10 }}
                onClick={onLogout} disabled={busy}>
          Sign out
        </button>
      </form>
    </div>
  );
}
