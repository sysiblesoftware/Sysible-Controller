import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Environmental Policies: the baseline password/lockout/sudo/umask policy for
// managed hosts, edited here and pushed out (mirrors the desktop tool).
export default function EnvironmentalPolicies() {
  const [pol, setPol] = useState(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.envPolicy().then((p) => setPol(p || {})).catch((e) => setErr(e.message));
  }, []);

  if (err) return <div className="error-box">{err}</div>;
  if (!pol) return <div className="empty"><span className="spin" /></div>;

  const num = (k) => (e) => setPol({ ...pol, [k]: e.target.value === "" ? "" : Number(e.target.value) });
  const text = (k) => (e) => setPol({ ...pol, [k]: e.target.value });

  async function save() {
    setBusy(true); setErr(""); setMsg("");
    try { await api.setEnvPolicy(pol); setMsg("Baseline policy saved."); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div style={{ maxWidth: 560, overflowY: "auto" }}>
      <p className="muted">Baseline password, lockout, sudo, and umask policy applied to accounts on managed hosts.</p>

      <fieldset className="tool-group-box"><legend>Password</legend>
        <label className="field"><span>Minimum length</span>
          <input type="number" value={pol.minlen ?? 12} onChange={num("minlen")} /></label>
        <div className="group-fields" style={{ marginTop: 10 }}>
          {["dcredit", "ucredit", "lcredit", "ocredit", "retry"].map((k) => (
            <div className="group-field" key={k}>
              <label className="field"><span>{k}</span>
                <input type="number" value={pol[k] ?? 0} onChange={num(k)} /></label>
            </div>
          ))}
        </div>
      </fieldset>

      <fieldset className="tool-group-box"><legend>Lockout</legend>
        <div className="group-fields">
          <div className="group-field"><label className="field"><span>deny (failed attempts)</span>
            <input type="number" value={pol.deny ?? 5} onChange={num("deny")} /></label></div>
          <div className="group-field"><label className="field"><span>unlock_time (s)</span>
            <input type="number" value={pol.unlock_time ?? 900} onChange={num("unlock_time")} /></label></div>
        </div>
      </fieldset>

      <fieldset className="tool-group-box"><legend>umask</legend>
        <label className="field"><span>Default umask</span>
          <input value={pol.umask ?? "027"} onChange={text("umask")} style={{ maxWidth: 140 }} /></label>
      </fieldset>

      <button className="btn" disabled={busy} onClick={save}>
        {busy ? <span className="spin" /> : "Save & Push Baseline"}
      </button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
    </div>
  );
}
