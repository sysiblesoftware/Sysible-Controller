import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import HostTree from "../components/HostTree.jsx";

// Environmental Policies — baseline password / lockout / sudo / umask policy
// for managed hosts. Edit & save the controller-side defaults, then push the
// selected policies to the checked hosts (runs the policy_* catalog actions).
// Mirrors the desktop's two-part page (defaults editor + push section).
export default function EnvironmentalPolicies({ hosts = [], onRefreshHosts }) {
  const [pol, setPol] = useState(null);
  const [targets, setTargets] = useState([]);
  const [push, setPush] = useState({ password: true, lockout: true, sudo: true, umask: true });
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(""); const [results, setResults] = useState([]);

  useEffect(() => { api.envPolicy().then((p) => setPol(normalize(p || {}))).catch((e) => setErr(e.message)); }, []);
  if (err && !pol) return <div className="error-box">{err}</div>;
  if (!pol) return <div className="empty"><span className="spin" /></div>;

  const set = (k, v) => setPol((p) => ({ ...p, [k]: v }));

  async function saveDefaults() {
    setBusy("save"); setErr(""); setMsg("");
    try { await api.setEnvPolicy(denormalize(pol)); setMsg("Policy defaults saved on the controller."); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function pushToHosts() {
    if (targets.length === 0) { setErr("Check one or more hosts to push to."); return; }
    setBusy("push"); setErr(""); setMsg(""); setResults([]);
    const jobs = [];
    if (push.password) jobs.push(["policy_pwquality", {
      minlen: pol.minlen, retry: pol.retry, dcredit: pol.require_digit ? -1 : 0,
      ucredit: pol.require_upper ? -1 : 0, lcredit: pol.require_lower ? -1 : 0, ocredit: pol.require_symbol ? -1 : 0 }, "Password quality"]);
    if (push.lockout) jobs.push(["policy_lockout", { deny: pol.deny, unlock_time: pol.unlock_time }, "Account lockout"]);
    if (push.sudo) jobs.push(["policy_sudo", { timestamp_timeout: pol.sudo_timeout, require_password: pol.sudo_require_password, group: "" }, "Sudo policy"]);
    if (push.umask) jobs.push(["policy_umask", { value: pol.umask }, "Umask"]);
    try {
      for (const [action, params, label] of jobs) {
        const r = await api.runTool(action, targets, params);
        setResults((prev) => [{ label, ...r, at: Date.now() }, ...prev]);
      }
      setMsg(`Pushed ${jobs.length} policy group(s) to ${targets.length} host(s).`);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  return (
    <div className="three-pane" style={{ gridTemplateColumns: "220px 1fr 340px" }}>
      <HostTree hosts={hosts} value={targets} onChange={setTargets} onRefresh={onRefreshHosts}
                footer="Check the hosts to push the selected policies to." />

      <div style={{ overflowY: "auto", paddingRight: 4, maxWidth: 560 }}>
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Password Quality</legend>
          <div className="group-fields">
            <div className="group-field"><label className="field"><span>Minimum length</span>
              <input type="number" value={pol.minlen} onChange={(e) => set("minlen", +e.target.value)} /></label></div>
            <div className="group-field"><label className="field"><span>Retry attempts</span>
              <input type="number" value={pol.retry} onChange={(e) => set("retry", +e.target.value)} /></label></div>
          </div>
          {[["require_upper", "Require uppercase"], ["require_lower", "Require lowercase"],
            ["require_digit", "Require digit"], ["require_symbol", "Require symbol"]].map(([k, l]) => (
            <div className="checkrow" key={k}><input id={k} type="checkbox" checked={!!pol[k]} onChange={(e) => set(k, e.target.checked)} /><label htmlFor={k}>{l}</label></div>
          ))}
        </fieldset>

        <fieldset className="tool-group-box"><legend>Account Lockout</legend>
          <div className="group-fields">
            <div className="group-field"><label className="field"><span>Failed attempts before lock</span>
              <input type="number" value={pol.deny} onChange={(e) => set("deny", +e.target.value)} /></label></div>
            <div className="group-field"><label className="field"><span>Unlock time (s, 0 = manual)</span>
              <input type="number" value={pol.unlock_time} onChange={(e) => set("unlock_time", +e.target.value)} /></label></div>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Sudo</legend>
          <label className="field"><span>Session timeout (minutes)</span>
            <input type="number" value={pol.sudo_timeout} onChange={(e) => set("sudo_timeout", +e.target.value)} /></label>
          <div className="checkrow"><input id="sreq" type="checkbox" checked={!!pol.sudo_require_password} onChange={(e) => set("sudo_require_password", e.target.checked)} /><label htmlFor="sreq">Require password for sudo</label></div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Umask</legend>
          <label className="field"><span>Default umask (octal, e.g. 027)</span>
            <input value={pol.umask} onChange={(e) => set("umask", e.target.value)} style={{ maxWidth: 140 }} /></label>
        </fieldset>

        <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 4 }}>
          <button className="btn" disabled={busy === "save"} onClick={saveDefaults}>{busy === "save" ? <span className="spin" /> : "Save Policy Defaults"}</button>
        </div>

        <fieldset className="tool-group-box" style={{ marginTop: 14 }}><legend>Push to Checked Hosts</legend>
          <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
            {[["password", "Password Quality"], ["lockout", "Lockout"], ["sudo", "Sudo"], ["umask", "Umask"]].map(([k, l]) => (
              <label className="checkrow" key={k} style={{ margin: 0 }}><input type="checkbox" checked={push[k]} onChange={(e) => setPush({ ...push, [k]: e.target.checked })} />{l}</label>
            ))}
          </div>
          <button className="btn" style={{ marginTop: 12 }} disabled={busy === "push" || targets.length === 0}
                  onClick={pushToHosts}>{busy === "push" ? <span className="spin" /> : `Push Selected Policies to ${targets.length} Host(s)`}</button>
        </fieldset>

        {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
        {err && <div className="error-box">{err}</div>}
      </div>

      <div className="tool-results-col" style={{ borderLeft: "1px solid var(--border)", paddingLeft: 16, display: "flex", flexDirection: "column" }}>
        <div className="results-head"><strong>Results</strong>
          <button className="btn ghost sm" disabled={!results.length} onClick={() => setResults([])}>Clear All</button></div>
        <div style={{ flex: 1, overflowY: "auto", maxHeight: "70vh" }}>
          {results.length === 0 ? <div className="empty" style={{ padding: 24 }}>Push policies to see per-host output.</div>
            : results.map((res, i) => (
              <div className="result" key={res.at + "-" + i}>
                <div className="rh"><strong>{res.label}</strong></div>
                {res.results.map((r, j) => (
                  <div key={j} style={{ borderTop: "1px solid var(--border)" }}>
                    <div className="rh"><span className={"dot " + (r.ok ? "ok" : "bad")} /><span>{r.host}</span>{r.code != null && <span className="faint">exit {r.code}</span>}</div>
                    {(r.stdout || r.stderr) && <pre>{r.stdout}{r.stderr}</pre>}
                  </div>
                ))}
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}

// Map the stored credit-based policy <-> friendly require-checkboxes.
function normalize(p) {
  return {
    minlen: p.minlen ?? 12, retry: p.retry ?? 3,
    require_upper: (p.ucredit ?? -1) < 0, require_lower: (p.lcredit ?? -1) < 0,
    require_digit: (p.dcredit ?? -1) < 0, require_symbol: (p.ocredit ?? 0) < 0,
    deny: p.deny ?? 5, unlock_time: p.unlock_time ?? 900,
    sudo_timeout: p.sudo_timeout ?? 15, sudo_require_password: p.sudo_require_password ?? true,
    umask: p.umask ?? "027",
  };
}
function denormalize(p) {
  return {
    minlen: p.minlen, retry: p.retry,
    ucredit: p.require_upper ? -1 : 0, lcredit: p.require_lower ? -1 : 0,
    dcredit: p.require_digit ? -1 : 0, ocredit: p.require_symbol ? -1 : 0,
    deny: p.deny, unlock_time: p.unlock_time,
    sudo_timeout: p.sudo_timeout, sudo_require_password: p.sudo_require_password, umask: p.umask,
  };
}
