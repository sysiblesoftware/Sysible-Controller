import React, { useState } from "react";
import { api } from "../api.js";

// The full Sysible Controller logo lockup, with a graceful fallback to the "S"
// monogram + wordmark if the image can't load. The PNG carries a near-white
// baked background (not transparent), so it blends into the light-theme card and
// reads as a framed logo tile on the dark-theme card — visible in BOTH themes,
// unlike a plain white-on-transparent mark that would vanish on the light card.
function Brand() {
  return (
    <div className="brand brand-login">
      <img
        className="brand-logo"
        src="/sysible_logo.png"
        alt="Sysible Controller"
        onError={(e) => {
          e.currentTarget.style.display = "none";
          const fb = e.currentTarget.nextElementSibling;
          if (fb) fb.style.display = "flex";
        }}
      />
      <div className="brand-fallback" style={{ display: "none" }}>
        <div className="brand-mark">S</div>
        <h1>Sysible Controller</h1>
      </div>
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
