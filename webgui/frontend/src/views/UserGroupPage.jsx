import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

// Faithful rebuild of the desktop User & Group Administration page: host tree
// with checkboxes + live sync (left), users-on-selected-host (middle), and
// Create/Account/Password/Groups/Reports/Policies tabs (right).

function genPassword(len = 16) {
  const lower = "abcdefghijkmnpqrstuvwxyz", upper = "ABCDEFGHJKLMNPQRSTUVWXYZ",
    dig = "23456789", sym = "!@#$%^&*()-_=+";
  const all = lower + upper + dig + sym;
  const pick = (s) => s[Math.floor(Math.random() * s.length)];
  let out = [pick(lower), pick(upper), pick(dig), pick(sym)];
  for (let i = out.length; i < len; i++) out.push(pick(all));
  return out.sort(() => Math.random() - 0.5).join("");
}

export default function UserGroupPage({ initialTab } = {}) {
  const [hosts, setHosts] = useState([]);
  const [checked, setChecked] = useState([]);
  const [collapsed, setCollapsed] = useState({});
  const [hostData, setHostData] = useState({});   // id -> {users,groups,sessions}
  const [viewHost, setViewHost] = useState(null);  // id whose users are shown
  const [syncMsg, setSyncMsg] = useState("Check one or more hosts, then Sync to pull live user data.");
  const [syncing, setSyncing] = useState(false);
  const [userQuery, setUserQuery] = useState("");
  const [selUser, setSelUser] = useState(null);
  const [tab, setTab] = useState(initialTab || "create");
  const [results, setResults] = useState(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");

  function loadHosts() { api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message)); }
  useEffect(loadHosts, []);

  const groups = useMemo(() => {
    const m = {};
    for (const h of hosts) (m[h.environment || "Ungrouped"] ||= []).push(h);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [hosts]);

  const toggleCheck = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  async function syncChecked() {
    if (checked.length === 0) { setSyncMsg("Check one or more hosts to sync."); return; }
    setSyncing(true); setErr(""); setSyncMsg(`Syncing ${checked.length} host(s)…`);
    let ok = 0;
    for (const id of checked) {
      try { const r = await api.usersSync(id); setHostData((d) => ({ ...d, [id]: r.data || {} })); ok++; }
      catch (e) { setErr(`${id}: ${e.message}`); }
    }
    if (!viewHost && checked[0]) setViewHost(checked[0]);
    setSyncMsg(`Synced ${ok}/${checked.length} host(s).`);
    setSyncing(false);
  }

  // Hosts we have synced data for — prefer the checked set, fall back to all
  // synced. The middle list is the UNION across these hosts so we can flag
  // users that exist on only some of them ("host mismatch").
  const syncedIds = useMemo(() => {
    const have = Object.keys(hostData);
    const c = checked.filter((id) => hostData[id]);
    return c.length ? c : have;
  }, [hostData, checked]);
  const labelOf = (id) => (hosts.find((h) => h.id === id)?.label || id);

  // username -> Set(hostId) it exists on, across syncedIds.
  const present = useMemo(() => {
    const p = {};
    for (const id of syncedIds)
      for (const u of (hostData[id]?.users || [])) (p[u.username] || (p[u.username] = new Set())).add(id);
    return p;
  }, [hostData, syncedIds]);

  const ft = userQuery.trim().toLowerCase();
  const showU = (name) => !ft || name.toLowerCase().includes(ft);
  const names = Object.keys(present).filter(showU).sort();
  const consistent = names.filter((n) => present[n].size === syncedIds.length);
  const partial = names.filter((n) => present[n].size !== syncedIds.length);

  // user object (for the detail panels) from any synced host that has it.
  function userObj(name) {
    for (const id of syncedIds) {
      const u = (hostData[id]?.users || []).find((x) => x.username === name);
      if (u) return u;
    }
    return null;
  }
  const selUserObj = selUser ? userObj(selUser) : null;

  async function run(action, targets, params, label) {
    if (targets.length === 0) { setErr("No target host(s) for this action."); return; }
    setRunning(true); setErr(""); setResults(null);
    try {
      const r = await api.runTool(action, targets, params);
      setResults({ label: label || action, ...r });
      // Re-sync the hosts we acted on (not just the viewed one) so the live
      // user/group lists reflect the change — Create User in particular runs
      // on the *checked* hosts, which may differ from the viewed host.
      if (action !== "user_audit_privileged" && action !== "group_members") {
        const toSync = [...new Set([...(targets || []), ...(viewHost ? [viewHost] : [])])];
        for (const id of toSync) {
          try { const s = await api.usersSync(id); setHostData((d) => ({ ...d, [id]: s.data || {} })); }
          catch { /* ignore */ }
        }
        // Make sure the operator is viewing a host the change actually
        // happened on (e.g. Create User runs on the checked hosts) so the
        // refreshed user list shows the result.
        if (targets[0] && !targets.includes(viewHost)) setViewHost(targets[0]);
      }
    } catch (e) { setErr(e.message); }
    finally { setRunning(false); }
  }

  // Per-user actions target the synced hosts where that user exists (so a
  // Lock/Delete applies everywhere it's present); fall back to all synced.
  const selTargets = selUser && present[selUser] ? [...present[selUser]] : syncedIds;
  const allHostIds = hosts.map((h) => h.id);
  // Selected user's status on each synced host (for "View Status by Host").
  const statusRows = selUser ? syncedIds.map((id) => {
    const u = (hostData[id]?.users || []).find((x) => x.username === selUser);
    return { host: labelOf(id), present: !!u, shell: u?.shell, sudo: u?.sudo, locked: u?.locked };
  }) : [];

  function renderUserRow(name, note) {
    const u = userObj(name);
    return (
      <div key={name} className="host-row" style={{ paddingLeft: 4, cursor: "pointer", alignItems: "flex-start",
             background: selUser === name ? "var(--row-hover)" : "" }}
           onClick={() => { setSelUser(name); setTab("account"); }}>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <span>
            {name}
            {u && u.sudo ? <span className="badge amber" style={{ fontSize: 10, marginLeft: 6 }}>sudo</span> : null}
            {u && u.locked ? <span className="badge" style={{ fontSize: 10, marginLeft: 6 }}>locked</span> : null}
          </span>
          {note && <span className="meta" style={{ fontSize: 11 }}>{note}</span>}
        </div>
      </div>
    );
  }

  return (
    <div className="three-pane" style={{ gridTemplateColumns: "240px 230px 1fr" }}>
      {/* LEFT: hosts */}
      <div className="host-pane">
        <strong style={{ fontSize: 13 }}>Target Hosts</strong>
        <div className="ctl-row" style={{ marginTop: 8 }}>
          <button className="btn ghost sm" onClick={loadHosts}>Refresh</button>
          <button className="btn ghost sm" onClick={() => setChecked(hosts.map((h) => h.id))}>Select All</button>
          <button className="btn ghost sm" onClick={() => setChecked([])}>Deselect All</button>
        </div>
        <button className="btn sm" style={{ margin: "4px 0", width: "100%" }}
                disabled={syncing} onClick={syncChecked}>
          {syncing ? <span className="spin" /> : "Sync Checked Hosts"}
        </button>
        <div className="ctl-row">
          <button className="btn ghost sm" onClick={() =>
            setCollapsed(Object.fromEntries(groups.map(([e]) => [e, true])))}>Collapse</button>
          <button className="btn ghost sm" onClick={() => setCollapsed({})}>Expand</button>
        </div>
        <div className="host-tree">
          {groups.map(([env, list]) => {
            const open = !collapsed[env];
            return (
              <div className="env-group" key={env}>
                <div className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}>
                  {open ? "▾" : "▸"} {env}
                </div>
                {open && list.map((h) => (
                  <div className={"host-row" + (viewHost === h.id ? " active" : "")} key={h.id}
                       style={{ background: viewHost === h.id ? "var(--row-hover)" : "" }}>
                    <input type="checkbox" checked={checked.includes(h.id)} onChange={() => toggleCheck(h.id)} />
                    <span style={{ cursor: "pointer" }} onClick={() => { setViewHost(h.id); setSelUser(null); }}>{h.label}</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>{syncMsg}</div>
      </div>

      {/* MIDDLE: users — union across synced hosts, with host-mismatch grouping */}
      <div style={{ borderRight: "1px solid var(--border)", paddingRight: 12, display: "flex", flexDirection: "column" }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>
          {syncedIds.length === 0 ? "Users (no host synced)"
            : syncedIds.length === 1 ? `Users on ${labelOf(syncedIds[0])}`
            : `Users across ${syncedIds.length} synced hosts`}
        </div>
        <input placeholder="Search users…" value={userQuery} onChange={(e) => setUserQuery(e.target.value)} />
        <div style={{ flex: 1, overflowY: "auto", marginTop: 10 }}>
          {syncedIds.length === 0 && <div className="faint" style={{ padding: 8 }}>Check hosts and Sync to load users.</div>}
          {syncedIds.length === 1 && names.map((n) => renderUserRow(n))}
          {syncedIds.length >= 2 && (
            <>
              {consistent.length > 0 && (
                <div className="section-title" style={{ marginTop: 0 }}>On all {syncedIds.length} hosts ({consistent.length})</div>
              )}
              {consistent.map((n) => renderUserRow(n))}
              {partial.length > 0 && (
                <div className="section-title" style={{ color: "var(--amber)" }}>
                  Host mismatch — on some hosts only ({partial.length})
                </div>
              )}
              {partial.map((n) => {
                const on = [...present[n]].map(labelOf);
                const missing = syncedIds.filter((id) => !present[n].has(id)).map(labelOf);
                return renderUserRow(n, `on ${on.join(", ")} — missing on ${missing.join(", ")}`);
              })}
            </>
          )}
        </div>
      </div>

      {/* RIGHT: tabs */}
      <div style={{ overflowY: "auto" }}>
        <div className="tabs">
          {[["create", "Create User"], ["account", "Account"], ["password", "Password"],
            ["groups", "Groups"], ["reports", "Reports"], ["policies", "Policies"]].map(([k, l]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>

        {tab === "create" && <CreateUser checked={checked} run={run} running={running} />}
        {tab === "account" && <Account user={selUserObj} targets={selTargets} run={run} running={running}
          statusRows={statusRows} allHostIds={allHostIds} />}
        {tab === "password" && <Password user={selUser} targets={selTargets} run={run} running={running} />}
        {tab === "groups" && <Groups user={selUser} checked={checked} viewTargets={selTargets} run={run} running={running} />}
        {tab === "reports" && <Reports targets={selTargets} run={run} running={running} />}
        {tab === "policies" && <Policies checked={checked} run={run} running={running} />}

        {err && <div className="error-box">{err}</div>}
        {results && (
          <div className="result" style={{ marginTop: 14 }}>
            <div className="rh"><strong>{results.label}</strong></div>
            {results.results.map((r, i) => (
              <div key={i} style={{ borderTop: i ? "1px solid var(--border)" : "none" }}>
                <div className="rh"><span className={"dot " + (r.ok ? "ok" : "bad")} /><span>{r.host}</span>
                  {r.code != null && <span className="faint">exit {r.code}</span>}</div>
                {(r.stdout || r.stderr) && <pre>{r.stdout}{r.stderr}</pre>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, ...p }) {
  return <label className="field"><span>{label}</span><input {...p} /></label>;
}

function CreateUser({ checked, run, running }) {
  const [u, setU] = useState(""); const [pw, setPw] = useState(""); const [sh, setSh] = useState("/bin/bash");
  return (
    <div>
      <h3 style={{ margin: "0 0 4px" }}>Create User</h3>
      <div className="muted">Creates the account on every currently checked host ({checked.length}).</div>
      <Field label="Username" value={u} onChange={(e) => setU(e.target.value)} />
      <label className="field"><span>Password (optional)</span>
        <div className="row"><input style={{ flex: 1 }} type="text" value={pw} onChange={(e) => setPw(e.target.value)} />
          <button className="btn ghost sm" type="button" onClick={() => setPw(genPassword())}>Generate</button></div>
      </label>
      <Field label="Shell" value={sh} onChange={(e) => setSh(e.target.value)} />
      <button className="btn" style={{ marginTop: 14 }} disabled={running || !u.trim() || checked.length === 0}
              onClick={() => run("user_create", checked, { username: u, password: pw, shell: sh }, `Create user ${u}`)}>
        {running ? <span className="spin" /> : `Create on ${checked.length} host(s)`}
      </button>
    </div>
  );
}

function Account({ user, targets, run, running, statusRows = [], allHostIds = [] }) {
  const [shell, setShell] = useState(""); const [comment, setComment] = useState("");
  const [showStatus, setShowStatus] = useState(false);
  useEffect(() => { setShell(user?.shell || ""); setComment(""); setShowStatus(false); }, [user]);
  if (!user) return <div className="empty">Select a user from the list to manage their account.</div>;
  const u = user.username;
  return (
    <div>
      <h3 style={{ margin: "0 0 8px" }}>{u}</h3>
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="muted mono" style={{ fontSize: 12.5 }}>
          uid {user.uid} · gid {user.gid} · home {user.home}<br />
          shell {user.shell}<br />
          groups: {(user.groups || []).join(", ") || "—"}<br />
          sudo: {user.sudo ? "yes" : "no"} · {user.locked ? "locked" : "active"}
        </div>
      </div>
      <label className="field"><span>Login shell</span>
        <div className="row"><input style={{ flex: 1 }} value={shell} onChange={(e) => setShell(e.target.value)} />
          <button className="btn sm" disabled={running || !shell} onClick={() => run("user_set_shell", targets, { username: u, shell }, `Set shell for ${u}`)}>Set Shell</button></div>
      </label>
      <label className="field"><span>Full name (GECOS)</span>
        <div className="row"><input style={{ flex: 1 }} value={comment} onChange={(e) => setComment(e.target.value)} />
          <button className="btn sm" disabled={running || !comment} onClick={() => run("user_set_comment", targets, { username: u, comment }, `Set full name for ${u}`)}>Set Full Name</button></div>
      </label>
      <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 14 }}>
        <button className="btn sm ghost" onClick={() => setShowStatus((v) => !v)}>View Status by Host</button>
        <button className="btn sm" disabled={running} onClick={() => run("user_lock", targets, { username: u }, `Lock ${u}`)}>Lock</button>
        <button className="btn sm" disabled={running} onClick={() => run("user_unlock", targets, { username: u }, `Unlock ${u}`)}>Unlock</button>
        <button className="btn sm" disabled={running} onClick={() => run("user_set_sudo", targets, { username: u, enable: !user.sudo }, `Toggle sudo for ${u}`)}>
          {user.sudo ? "Remove Sudo" : "Grant Sudo"}</button>
        <button className="btn sm" disabled={running} onClick={() => run("user_kill_sessions", targets, { username: u }, `Kill sessions for ${u}`)}>Kill Sessions</button>
        <button className="btn sm danger" disabled={running} onClick={() => window.confirm(`Delete user ${u} on ${targets.length} host(s)?`) && run("user_delete", targets, { username: u }, `Delete ${u}`)}>Delete User</button>
        <button className="btn sm danger" disabled={running || allHostIds.length === 0}
                onClick={() => window.confirm(`Terminate ${u} — remove from ALL ${allHostIds.length} managed hosts? This cannot be undone.`)
                  && run("user_delete", allHostIds, { username: u }, `Terminate user ${u} (all hosts)`)}>
          Terminate User (All Hosts)</button>
      </div>

      {showStatus && (
        <div className="card" style={{ marginTop: 12 }}>
          <strong>Status by host — {u}</strong>
          {statusRows.length === 0 ? <div className="faint" style={{ marginTop: 6 }}>Sync hosts to see per-host status.</div> : (
            <table style={{ marginTop: 8 }}>
              <thead><tr><th>Host</th><th>Present</th><th>Shell</th><th>Sudo</th><th>State</th></tr></thead>
              <tbody>
                {statusRows.map((r, i) => (
                  <tr key={i}>
                    <td>{r.host}</td>
                    <td>{r.present ? <span className="ok-text">yes</span> : <span className="faint">no</span>}</td>
                    <td className="faint">{r.present ? r.shell : "—"}</td>
                    <td>{r.present ? (r.sudo ? "yes" : "no") : "—"}</td>
                    <td>{r.present ? (r.locked ? "locked" : "active") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

function Password({ user, targets, run, running }) {
  const [pw, setPw] = useState("");
  const [mx, setMx] = useState(""); const [mn, setMn] = useState(""); const [wn, setWn] = useState("");
  const [exp, setExp] = useState("");
  if (!user) return <div className="empty">Select a user to manage their password.</div>;
  return (
    <div>
      <h3 style={{ margin: "0 0 8px" }}>Password — {user}</h3>
      <label className="field"><span>New password</span>
        <div className="row"><input style={{ flex: 1 }} type="text" value={pw} onChange={(e) => setPw(e.target.value)} />
          <button className="btn ghost sm" type="button" onClick={() => setPw(genPassword())}>Generate</button>
          <button className="btn sm" disabled={running || !pw} onClick={() => run("user_set_password", targets, { username: user, password: pw }, `Set password for ${user}`)}>Set</button></div>
      </label>
      <button className="btn sm ghost" style={{ marginTop: 10 }} disabled={running}
              onClick={() => run("user_force_reset", targets, { username: user }, `Force reset for ${user}`)}>Force password reset at next login</button>
      <div className="section-title">Password aging</div>
      <div className="row" style={{ gap: 8 }}>
        <Field label="Max days" type="number" value={mx} onChange={(e) => setMx(e.target.value)} />
        <Field label="Min days" type="number" value={mn} onChange={(e) => setMn(e.target.value)} />
        <Field label="Warn days" type="number" value={wn} onChange={(e) => setWn(e.target.value)} />
      </div>
      <button className="btn sm" style={{ marginTop: 8 }} disabled={running}
              onClick={() => run("user_set_aging", targets, { username: user, max_days: mx, min_days: mn, warn_days: wn }, `Set aging for ${user}`)}>Apply aging</button>
      <div className="section-title">Account expiration</div>
      <div className="row" style={{ gap: 8 }}>
        <input type="date" value={exp} onChange={(e) => setExp(e.target.value)} style={{ maxWidth: 200 }} />
        <button className="btn sm" disabled={running} onClick={() => run("user_set_expiration", targets, { username: user, expire_date: exp }, `Set expiration for ${user}`)}>Set Expiration</button>
      </div>
    </div>
  );
}

function Groups({ user, checked, viewTargets, run, running }) {
  const [g, setG] = useState("");
  return (
    <div>
      <h3 style={{ margin: "0 0 8px" }}>Groups</h3>
      <Field label="Group name" value={g} onChange={(e) => setG(e.target.value)} />
      <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 10 }}>
        <button className="btn sm" disabled={running || !g || checked.length === 0} onClick={() => run("group_create", checked, { name: g }, `Create group ${g}`)}>Create Group (checked hosts)</button>
        <button className="btn sm danger" disabled={running || !g || checked.length === 0} onClick={() => run("group_delete", checked, { name: g }, `Delete group ${g}`)}>Delete Group</button>
      </div>
      <div className="section-title">Membership for {user || "(select a user)"}</div>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button className="btn sm" disabled={running || !g || !user} onClick={() => run("group_add_user", viewTargets, { group: g, username: user }, `Add ${user} to ${g}`)}>Add User to Group</button>
        <button className="btn sm" disabled={running || !g || !user} onClick={() => run("group_remove_user", viewTargets, { group: g, username: user }, `Remove ${user} from ${g}`)}>Remove User from Group</button>
        <button className="btn sm ghost" disabled={running || viewTargets.length === 0} onClick={() => run("group_members", viewTargets, {}, "Groups & members")}>List Groups & Members</button>
      </div>
    </div>
  );
}

function Reports({ targets, run, running }) {
  return (
    <div>
      <h3 style={{ margin: "0 0 8px" }}>Reports</h3>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button className="btn sm" disabled={running || targets.length === 0} onClick={() => run("user_audit_privileged", targets, {}, "Privileged user audit")}>Audit Privileged Users</button>
        <button className="btn sm" disabled={running || targets.length === 0} onClick={() => run("group_members", targets, {}, "Groups & members")}>Groups & Members</button>
      </div>
      {targets.length === 0 && <div className="faint" style={{ marginTop: 8 }}>Select a host on the left first.</div>}
    </div>
  );
}

function Policies({ checked, run, running }) {
  const [pq, setPq] = useState({ minlen: 12, retry: 3, dcredit: -1, ucredit: -1, lcredit: -1, ocredit: 0 });
  const [lo, setLo] = useState({ deny: 5, unlock_time: 900 });
  const [umask, setUmask] = useState("027");
  const set = (o, s) => (k) => (e) => s({ ...o, [k]: e.target.value });
  return (
    <div>
      <h3 style={{ margin: "0 0 4px" }}>Policies</h3>
      <div className="muted">Applied to every checked host ({checked.length}).</div>
      <div className="section-title">Password quality</div>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        {["minlen", "retry", "dcredit", "ucredit", "lcredit", "ocredit"].map((k) => (
          <Field key={k} label={k} type="number" value={pq[k]} onChange={set(pq, setPq)(k)} />
        ))}
      </div>
      <button className="btn sm" style={{ marginTop: 8 }} disabled={running || !checked.length}
              onClick={() => run("policy_pwquality", checked, pq, "Set password quality")}>Apply Password Quality</button>
      <div className="section-title">Account lockout</div>
      <div className="row" style={{ gap: 8 }}>
        <Field label="deny" type="number" value={lo.deny} onChange={set(lo, setLo)("deny")} />
        <Field label="unlock_time" type="number" value={lo.unlock_time} onChange={set(lo, setLo)("unlock_time")} />
        <button className="btn sm" style={{ alignSelf: "flex-end" }} disabled={running || !checked.length}
                onClick={() => run("policy_lockout", checked, lo, "Set lockout policy")}>Apply</button>
      </div>
      <div className="section-title">umask</div>
      <div className="row" style={{ gap: 8 }}>
        <input style={{ maxWidth: 120 }} value={umask} onChange={(e) => setUmask(e.target.value)} />
        <button className="btn sm" disabled={running || !checked.length}
                onClick={() => run("policy_umask", checked, { value: umask }, "Set umask")}>Apply umask</button>
      </div>
    </div>
  );
}
