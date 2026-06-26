import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Sysible Controller Settings: administrators, password policy, controller
// address/port, license, and the admin audit log.
export default function Settings() {
  const [tab, setTab] = useState("admins");
  return (
    <div>
      <div className="tabs" style={{ marginBottom: 16 }}>
        {[["admins", "Administrators"], ["policy", "Password Policy"],
          ["controller", "Controller"], ["license", "License"], ["audit", "Audit Log"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === "admins" && <Admins />}
      {tab === "policy" && <PasswordPolicy />}
      {tab === "controller" && <ControllerCfg />}
      {tab === "license" && <License />}
      {tab === "audit" && <Audit />}
    </div>
  );
}

function useErr() { const [err, setErr] = useState(""); return [err, setErr]; }

function Admins() {
  const [list, setList] = useState([]);
  const [u, setU] = useState(""); const [p, setP] = useState(""); const [role, setRole] = useState("sysadmin");
  const [err, setErr] = useErr(); const [msg, setMsg] = useState("");

  const load = () => api.admins().then((d) => setList(d.administrators || [])).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);

  async function add(e) {
    e.preventDefault(); setErr(""); setMsg("");
    try { await api.addAdmin(u.trim(), p, role); setMsg(`Added ${u}.`); setU(""); setP(""); load(); }
    catch (e2) { setErr(e2.message); }
  }
  async function remove(name) {
    if (!window.confirm(`Remove administrator ${name}?`)) return;
    setErr(""); try { await api.removeAdmin(name); load(); } catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <div className="card" style={{ marginBottom: 16 }}>
        <table>
          <thead><tr><th>Username</th><th>Role</th><th></th></tr></thead>
          <tbody>
            {list.map((a) => (
              <tr key={a.username}>
                <td style={{ fontWeight: 600 }}>{a.username}</td>
                <td><span className="badge">{a.role}</span></td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn ghost sm" onClick={() => remove(a.username)}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="card" onSubmit={add}>
        <strong>Add administrator</strong>
        <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 10 }}>
          <input style={{ flex: 1, minWidth: 140 }} placeholder="Username" value={u} onChange={(e) => setU(e.target.value)} />
          <input style={{ flex: 1, minWidth: 140 }} type="password" placeholder="Password" value={p} onChange={(e) => setP(e.target.value)} />
          <select style={{ maxWidth: 150 }} value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="sysadmin">sysadmin</option>
            <option value="superuser">superuser</option>
          </select>
          <button className="btn sm" disabled={!u.trim() || !p}>Add</button>
        </div>
        {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
        {err && <div className="error-box">{err}</div>}
      </form>
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
      <label className="field"><span>IP</span><input value={cfg.ip || ""} onChange={set("ip")} /></label>
      <label className="field"><span>Port</span><input type="number" value={cfg.port || 9000} onChange={set("port")} /></label>
      <button className="btn" style={{ marginTop: 14 }} onClick={save}>Save</button>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box">{err}</div>}
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
