import React, { useState } from "react";
import { api } from "../api.js";
import Logo from "../Logo.jsx";

// First-run create-administrator screen. Shown (before the Login form) only when
// the controller reports setup_required — i.e. a fresh database with no admin yet.
// The operator picks their own username + password here; on success the backend
// creates the first superuser and logs them straight in (no temp password).

function Brand() {
  return (
    <div className="brand brand-login">
      <Logo size={40} />
      <h1>Sysible Controller</h1>
    </div>
  );
}

export default function Setup({ onDone }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr("");
    if (password !== confirm) {
      setErr("The passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const r = await api.setup(username.trim(), password);
      onDone(r.username, r.role, r.must_enroll_mfa);
    } catch (e2) {
      setErr(e2.message || "Could not create the administrator.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <Brand />
        <p className="muted" style={{ marginTop: 4 }}>
          Welcome. Create the first administrator account for this controller.
        </p>
        <label className="field">
          <span>Administrator username</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
          />
        </label>
        <label className="field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
        </label>
        <label className="field">
          <span>Confirm password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </label>

        {err && <div className="error-box" role="alert">{err}</div>}

        <button
          className="btn full"
          style={{ marginTop: 18 }}
          disabled={busy || !username || !password || !confirm}
        >
          {busy ? <span className="spin" /> : "Create administrator"}
        </button>
      </form>
    </div>
  );
}
