import React, { useState } from "react";
import { api } from "../api.js";
import Logo from "../Logo.jsx";

// The Sysible Controller mark (crisp inline SVG) paired with the wordmark —
// compact lockup (logo beside a two-line name), matching the SLEP sign-in.
function Brand() {
  return (
    <div className="brand brand-login">
      <Logo size={40} />
      <div className="brand-name">Sysible<br />Controller</div>
    </div>
  );
}

export default function Login({ onLoggedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const r = await api.login(username.trim(), password);
      onLoggedIn(r.username, r.role, r.must_change_password);
    } catch (e2) {
      setErr(e2.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <Brand />
        <p className="muted" style={{ marginTop: 4 }}>
          Sign in with your controller administrator account.
        </p>

        <label className="field">
          <span>Username</span>
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
            autoComplete="current-password"
          />
        </label>

        {err && <div className="error-box" role="alert">{err}</div>}

        <button
          className="btn full"
          style={{ marginTop: 18 }}
          disabled={busy || !username || !password}
        >
          {busy ? <span className="spin" /> : "Sign in"}
        </button>
      </form>
    </div>
  );
}
