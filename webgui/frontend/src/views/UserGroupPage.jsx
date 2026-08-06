import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import HostResults from "../components/HostResults.jsx";

// User & Group Administration. Host tree with live sync (left), the fleet's
// distinct accounts with coverage (middle), and the Create/Account/Password/
// Groups/Reports/Policies tabs (right).
//
// The middle pane is keyed on the DISTINCT account (not the host), so it stays
// bounded — a few hundred usernames — even across thousands of hosts. Presence is
// shown as a coverage count with a per-environment drill-down instead of an
// unusable inline list of host names.

// Cryptographically-secure uniform integer in [0, n) via rejection sampling over
// crypto.getRandomValues — Math.random() is NOT a CSPRNG (V8 uses xorshift128+),
// which is inappropriate for a value applied to real OS-account passwords.
function _randInt(n) {
  const g = (typeof window !== "undefined" && window.crypto) || globalThis.crypto;
  const limit = Math.floor(0x100000000 / n) * n; // largest multiple of n ≤ 2^32
  const buf = new Uint32Array(1);
  let x;
  do { g.getRandomValues(buf); x = buf[0]; } while (x >= limit);
  return x % n;
}

function genPassword(len = 16) {
  const lower = "abcdefghijkmnpqrstuvwxyz", upper = "ABCDEFGHJKLMNPQRSTUVWXYZ",
    dig = "23456789", sym = "!@#$%^&*()-_=+";
  const all = lower + upper + dig + sym;
  const pick = (s) => s[_randInt(s.length)];
  const out = [pick(lower), pick(upper), pick(dig), pick(sym)];
  for (let i = out.length; i < len; i++) out.push(pick(all));
  // Unbiased Fisher-Yates shuffle (the Array.sort random comparator is both biased
  // and, like the picks above, must not depend on Math.random for a secret).
  for (let i = out.length - 1; i > 0; i--) {
    const j = _randInt(i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out.join("");
}

// A password entry field masked by default (so a set/reset password is not left
// shoulder-surfable on screen), with an explicit Show/Hide toggle and the
// existing Generate button. `extra` renders alongside (e.g. a Set button).
function PasswordField({ value, onChange, extra }) {
  const [show, setShow] = useState(false);
  return (
    <div className="row">
      <input style={{ flex: 1 }} type={show ? "text" : "password"} autoComplete="new-password"
             value={value} onChange={onChange} />
      <button className="btn ghost sm" type="button" aria-pressed={show}
              onClick={() => setShow((s) => !s)}>{show ? "Hide" : "Show"}</button>
      <button className="btn ghost sm" type="button" onClick={() => onChange({ target: { value: genPassword() } })}>Generate</button>
      {extra}
    </div>
  );
}

const LockIcon = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 1a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2h-1V6a5 5 0 0 0-5-5Zm3 8H9V6a3 3 0 0 1 6 0Z" />
  </svg>
);

export default function UserGroupPage({ initialTab } = {}) {
  const [hosts, setHosts] = useState([]);
  const [checked, setChecked] = useState([]);
  const [collapsed, setCollapsed] = useState({});
  const [hostData, setHostData] = useState({});   // id -> {users,groups,sessions}
  const [viewHost, setViewHost] = useState(null);  // id whose users are shown
  const [syncMsg, setSyncMsg] = useState("Check one or more hosts, then Sync to pull live user data.");
  const [syncing, setSyncing] = useState(false);
  const [userQuery, setUserQuery] = useState("");
  const [filter, setFilter] = useState("all");     // all|people|priv|locked|partial|system
  const [drill, setDrill] = useState(null);        // account name whose coverage drill-down is open
  const [selUser, setSelUser] = useState(null);
  const [tab, setTab] = useState(initialTab || "create");
  const [results, setResults] = useState(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");

  function loadHosts() { api.hosts().then((d) => setHosts(d.hosts || [])).catch((e) => setErr(e.message)); }
  useEffect(loadHosts, []);

  const groups = useMemo(() => {
    const m = {};
    for (const h of hosts) (m[h.environment || "Unassigned"] ||= []).push(h);
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

  // Hosts we have synced data for — prefer the checked set, fall back to all synced.
  const syncedIds = useMemo(() => {
    const have = Object.keys(hostData);
    const c = checked.filter((id) => hostData[id]);
    return c.length ? c : have;
  }, [hostData, checked]);
  const labelOf = (id) => (hosts.find((h) => h.id === id)?.label || id);
  const hostEnvOf = useMemo(
    () => Object.fromEntries(hosts.map((h) => [h.id, h.environment || "Unassigned"])), [hosts]);

  // username -> Set(hostId) it exists on, across syncedIds.
  const present = useMemo(() => {
    const p = {};
    for (const id of syncedIds)
      for (const u of (hostData[id]?.users || [])) (p[u.username] || (p[u.username] = new Set())).add(id);
    return p;
  }, [hostData, syncedIds]);

  // user object (for the detail panels) from any synced host that has it.
  function userObj(name) {
    for (const id of syncedIds) {
      const u = (hostData[id]?.users || []).find((x) => x.username === name);
      if (u) return u;
    }
    return null;
  }
  const selUserObj = selUser ? userObj(selUser) : null;

  // One entry per DISTINCT account across the synced hosts, with fleet coverage
  // and a per-environment breakdown. Bounded by account count, not host count.
  const accounts = useMemo(() => {
    const total = syncedIds.length;
    return Object.keys(present).sort().map((name) => {
      const on = present[name];
      const obj = userObj(name) || {};
      const env = {};
      for (const id of syncedIds) {
        const e = hostEnvOf[id] || "Unassigned";
        (env[e] || (env[e] = { on: 0, of: 0 }));
        env[e].of++; if (on.has(id)) env[e].on++;
      }
      const active = syncedIds.some((id) => (hostData[id]?.sessions || []).some((s) => s.username === name));
      return {
        name, obj, active, env,
        cov: on.size, total,
        partial: total > 0 && on.size < total,
        system: (obj.uid ?? 1000) < 1000,
        priv: !!obj.sudo, locked: !!obj.locked,
        missing: syncedIds.filter((id) => !on.has(id)),
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [present, syncedIds, hostData, hostEnvOf]);

  const ft = userQuery.trim().toLowerCase();
  const shown = accounts
    .filter((a) => !ft || a.name.toLowerCase().includes(ft))
    .filter((a) => filter === "all" ? true
      : filter === "people" ? !a.system
      : filter === "priv" ? a.priv
      : filter === "locked" ? a.locked
      : filter === "partial" ? a.partial
      : filter === "system" ? a.system : true);
  const summary = {
    total: accounts.length,
    priv: accounts.filter((a) => a.priv).length,
    locked: accounts.filter((a) => a.locked).length,
    partial: accounts.filter((a) => a.partial).length,
    system: accounts.filter((a) => a.system).length,
  };
  const gFull = shown.filter((a) => !a.system && !a.partial);
  const gPartial = shown.filter((a) => !a.system && a.partial);
  const gSystem = shown.filter((a) => a.system);

  async function run(action, targets, params, label, confirmMsg) {
    if (targets.length === 0) { setErr("No target host(s) for this action."); return; }
    if (confirmMsg && !window.confirm(`${confirmMsg} on ${targets.length} host${targets.length === 1 ? "" : "s"}?`)) return;
    setRunning(true); setErr(""); setResults(null);
    try {
      const r = await api.runTool(action, targets, params);
      setResults({ label: label || action, ...r });
      if (action !== "user_audit_privileged" && action !== "group_members") {
        const toSync = [...new Set([...(targets || []), ...(viewHost ? [viewHost] : [])])];
        for (const id of toSync) {
          try { const s = await api.usersSync(id); setHostData((d) => ({ ...d, [id]: s.data || {} })); }
          catch { /* ignore */ }
        }
        if (targets[0] && !targets.includes(viewHost)) setViewHost(targets[0]);
      }
    } catch (e) { setErr(e.message); }
    finally { setRunning(false); }
  }

  // Per-user actions target the synced hosts where that user exists.
  const selTargets = selUser && present[selUser] ? [...present[selUser]] : syncedIds;
  const allHostIds = hosts.map((h) => h.id);
  const statusRows = selUser ? syncedIds.map((id) => {
    const u = (hostData[id]?.users || []).find((x) => x.username === selUser);
    return { host: labelOf(id), present: !!u, shell: u?.shell, sudo: u?.sudo, locked: u?.locked };
  }) : [];

  function pickUser(name) { setSelUser(name); setTab("account"); }

  function AccountRow({ a }) {
    const initial = (a.name[0] || "?").toUpperCase();
    const isRoot = a.name === "root" || Number(a.obj.uid) === 0;  // uid may be a string
    const mcls = a.locked ? "locked"
      : isRoot ? "root"                   // superuser → royal blue (brand accent)
      : a.priv ? "priv"                   // sudo (non-root) → amber
      : a.system ? "sys"                  // system/service accounts → green
      : "human";                          // regular login users → neutral slate
    const open = drill === a.name;
    return (
      <React.Fragment>
        <div className={"ug-urow" + (selUser === a.name ? " sel" : "") + (a.locked ? " locked" : "")}
             onClick={() => pickUser(a.name)}>
          <div className={"ug-mono " + mcls}>{a.locked ? <LockIcon /> : initial}</div>
          <div className="ug-main">
            <div className="ug-name">{a.name}</div>
            <div className="ug-sub">uid {a.obj.uid ?? "—"} · {a.obj.shell || "—"}</div>
          </div>
          <div className="ug-right">
            <div className="ug-chips">
              {a.priv && <span className="ug-chip sudo">sudo</span>}
              {a.locked && <span className="ug-chip locked">locked</span>}
              {a.system && <span className="ug-chip sys">system</span>}
              {a.active && <span className="ug-sess"><span className="ug-pulse" />active</span>}
            </div>
            <div className={"ug-cov" + (a.partial ? " part" : "")}
                 title={`Present on ${a.cov} of ${a.total} synced host(s)`}
                 onClick={a.total > 1 ? (e) => { e.stopPropagation(); setDrill(open ? null : a.name); } : undefined}>
              <span className="ug-covnum">{a.cov.toLocaleString()}/{a.total.toLocaleString()}</span>
              {a.total > 1 && <span className="ug-disc">{open ? "▾" : "▸"}</span>}
            </div>
          </div>
        </div>
        {open && a.total > 1 && (
          <div className="ug-drill" onClick={(e) => e.stopPropagation()}>
            <div className="ug-drill-h">{a.name} — host coverage</div>
            <div className="ug-drill-sub">
              Present on {a.cov.toLocaleString()} · missing on {(a.total - a.cov).toLocaleString()} host(s). By environment:
            </div>
            {Object.entries(a.env).sort(([x], [y]) => x.localeCompare(y)).map(([e, c]) => {
              const pct = c.of ? Math.round(c.on / c.of * 100) : 0;
              return (
                <div className="ug-envrow" key={e}>
                  <span className="en">{e}</span>
                  <span className={"ug-covbar" + (c.on < c.of ? " part" : "")}><i style={{ width: pct + "%" }} /></span>
                  <span className="num">{c.on}/{c.of}{c.on === c.of ? " ✓" : ""}</span>
                </div>
              );
            })}
            {a.missing.length > 0 && (
              <div className="ug-drill-act">
                <button className="btn sm" disabled={running}
                        onClick={() => run("user_create", a.missing,
                          { username: a.name, shell: a.obj.shell || "/bin/bash" },
                          `Create ${a.name} on ${a.missing.length} missing host(s)`)}>
                  Create “{a.name}” on the {a.missing.length} missing host(s)
                </button>
              </div>
            )}
          </div>
        )}
      </React.Fragment>
    );
  }

  return (
    <div className="three-pane ug-3col">
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
            const groupIds = list.map((h) => h.id);
            const allInGroup = groupIds.every((id) => checked.includes(id));
            return (
              <div className="env-group" key={env}>
                <div className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: open }))}>
                  <input
                    type="checkbox"
                    className="env-check"
                    title={`Select all in ${env}`}
                    checked={allInGroup}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => {
                      e.stopPropagation();
                      setChecked((c) => allInGroup
                        ? c.filter((id) => !groupIds.includes(id))
                        : [...new Set([...c, ...groupIds])]);
                    }}
                  />
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

      {/* MIDDLE: fleet accounts — coverage-based, scales to thousands of hosts */}
      <div className="ug-mid">
        <div className="ug-mid-h">
          {syncedIds.length === 0 ? "Accounts (no host synced)"
            : syncedIds.length === 1 ? `Accounts on ${labelOf(syncedIds[0])}`
            : `Fleet accounts · ${syncedIds.length.toLocaleString()} synced hosts`}
        </div>
        <input placeholder="Search accounts…" value={userQuery} onChange={(e) => setUserQuery(e.target.value)} />

        {syncedIds.length === 0 && (
          <div className="faint" style={{ padding: 8 }}>Check hosts and Sync to load accounts.</div>
        )}

        {syncedIds.length > 0 && (
          <>
            <div className="ug-tiles">
              {/* Each tile is a shortcut to its filter — clicking narrows the list
                  below (and toggles back to All when clicked while already active),
                  mirroring the filter chips. */}
              {[["all", "Accounts", summary.total.toLocaleString(), ""],
                ["priv", "Privileged", summary.priv, "warn"],
                ...(summary.locked > 0 ? [["locked", "Locked", summary.locked, "bad"]] : []),
                ...(syncedIds.length > 1 ? [["partial", "Partial", summary.partial, "warn"]] : []),
              ].map(([k, l, n, cls]) => (
                <div key={k} role="button" tabIndex={0}
                     className={"ug-tile" + (cls ? " " + cls : "") + (filter === k ? " on" : "")}
                     title={filter === k ? `Showing ${l} — click to clear` : `Show ${l}`}
                     onClick={() => setFilter(filter === k ? "all" : k)}
                     onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setFilter(filter === k ? "all" : k); } }}>
                  <div className="n">{n}</div><div className="l">{l}</div>
                </div>
              ))}
            </div>

            <div className="ug-filters">
              {[["all", "All", summary.total], ["people", "People", summary.total - summary.system],
                ["priv", "Privileged", summary.priv], ["locked", "Locked", summary.locked],
                ["partial", "Partial", summary.partial], ["system", "System", summary.system]].map(([k, l, c]) => (
                <span key={k} className={"ug-fchip" + (filter === k ? " on" : "")} onClick={() => setFilter(k)}>
                  {l} <span className="c">{c}</span></span>
              ))}
            </div>

            <div className="ug-list">
              {filter === "all" ? (
                <>
                  {gFull.length > 0 && <div className="ug-grp">On every synced host <span className="n">{gFull.length}</span></div>}
                  {gFull.map((a) => <AccountRow a={a} key={a.name} />)}
                  {gPartial.length > 0 && <div className="ug-grp warn">Partial coverage <span className="n">{gPartial.length}</span></div>}
                  {gPartial.map((a) => <AccountRow a={a} key={a.name} />)}
                  {gSystem.length > 0 && <div className="ug-grp">System accounts <span className="n">{gSystem.length}</span></div>}
                  {gSystem.map((a) => <AccountRow a={a} key={a.name} />)}
                </>
              ) : shown.map((a) => <AccountRow a={a} key={a.name} />)}
              {shown.length === 0 && <div className="faint" style={{ padding: 8 }}>No accounts match.</div>}
            </div>

            <div className="ug-foot">{shown.length.toLocaleString()} of {accounts.length.toLocaleString()} accounts</div>
          </>
        )}
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
            <HostResults rows={results.results} />
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
        <PasswordField value={pw} onChange={(e) => setPw(e.target.value)} />
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
        <button className="btn sm" disabled={running} onClick={() => run("user_set_sudo", targets, { username: u, enable: !user.sudo }, `Toggle sudo for ${u}`, `${user.sudo ? "Remove sudo from" : "Grant sudo to"} "${u}"`)}>
          {user.sudo ? "Remove Sudo" : "Grant Sudo"}</button>
        <button className="btn sm" disabled={running} onClick={() => run("user_kill_sessions", targets, { username: u }, `Kill sessions for ${u}`, `Kill all active sessions for "${u}"`)}>Kill Sessions</button>
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
        <PasswordField value={pw} onChange={(e) => setPw(e.target.value)}
          extra={<button className="btn sm" disabled={running || !pw} onClick={() => run("user_set_password", targets, { username: user, password: pw }, `Set password for ${user}`)}>Set</button>} />
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
        <button className="btn sm danger" disabled={running || !g || checked.length === 0} onClick={() => run("group_delete", checked, { name: g }, `Delete group ${g}`, `Delete the group "${g}"`)}>Delete Group</button>
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
