import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { confirmDialog } from "../confirmDialog.js";

// Migrate — same-identity controller migration (superuser). Enter a NEW controller
// address (IP/hostname + port), multi-select hosts from the fleet, and re-point each
// selected agent at the new controller. "Same identity" means the new controller
// shares this one's database (a restore/failover to a new box or IP), so each agent
// keeps its host_id/secret and just needs SYSIBLE_CONTROLLER rewritten + a service
// restart — exactly what agent_bundle.py's migrate_agent.sh does. The re-point task
// restarts the agent, so a host may not report the task result back — success shows
// as the host checking in again against the new controller.

function fmtSeen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

export default function Migrate() {
  const [agents, setAgents] = useState([]);
  const [checked, setChecked] = useState([]);
  const [host, setHost] = useState("");    // new controller IP/hostname
  const [port, setPort] = useState("9000"); // new controller port (agent-API default)
  const [scheme, setScheme] = useState("https");
  const [results, setResults] = useState(null); // per-host {host_id, hostname, ok, error?}
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");

  function load(surfaceErr = false) {
    api.agents()
      .then((d) => setAgents(d.agents || []))
      .catch((e) => { if (surfaceErr) setErr(e.message); });
  }
  useEffect(() => { load(true); }, []);

  const idOf = (a) => a.host_id || a.id;
  const toggle = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  const groups = useMemo(() => {
    const m = {};
    for (const a of agents) (m[a.environment || "Unassigned"] ||= []).push(a);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [agents]);

  // Full new-controller URL preview (matches the controller's normalization:
  // scheme://host:port, default https / 9000).
  const newUrl = `${scheme}://${host.trim() || "<controller>"}:${port || 9000}`;

  async function migrate() {
    setErr(""); setMsg(""); setResults(null);
    if (!host.trim()) { setErr("Enter the new controller address (IP or hostname)."); return; }
    if (checked.length === 0) { setErr("Check one or more hosts to migrate."); return; }
    const ok = await confirmDialog(
      `Migrate ${checked.length} agent(s) to ${newUrl}?\n\n` +
      "Each selected agent will be RE-POINTED at the new controller — its " +
      "SYSIBLE_CONTROLLER address is rewritten and the sysible-agent service is " +
      "restarted. Use this only for a SAME-IDENTITY migration: the new controller " +
      "shares this controller's database (a restore/failover to a new box or IP), so " +
      "each agent keeps its identity and re-attaches automatically.\n\n" +
      "Because the agent restarts as part of the task, it may not report the result " +
      "back here — success shows as the host checking in against the new controller. " +
      "Continue?",
      { okLabel: "Migrate", danger: true });
    if (!ok) return;

    setBusy(`Migrating ${checked.length} host(s)…`);
    try {
      const r = await api.migrateAgents({ new_url: newUrl, host_ids: [...checked] });
      setResults(r.results || []);
      const failed = (r.results || []).filter((x) => !x.ok).length;
      setMsg(`Dispatched re-point to ${r.dispatched} of ${checked.length} host(s)` +
        (failed ? `, ${failed} failed to enqueue.` : ".") +
        " Watch the fleet for hosts checking in against the new controller.");
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  const resultById = useMemo(() => {
    const m = {};
    for (const r of results || []) m[r.host_id] = r;
    return m;
  }, [results]);

  return (
    <div className="he-2col">
      <fieldset className="tool-group-box he-hosts">
        <legend>Fleet Hosts (grouped by environment)</legend>
        <div className="ctl-row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
          <button className="btn ghost sm" onClick={() => setChecked(agents.map(idOf))}>Select All</button>
          <button className="btn ghost sm" onClick={() => setChecked([])}>Deselect All</button>
          <button className="btn ghost sm" onClick={() => load()}>Refresh</button>
          <span className="he-checked">{checked.length} checked</span>
        </div>
        <div style={{ flex: 1, overflowY: "auto" }}>
          {agents.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled yet.</div>}
          {groups.map(([env, list]) => {
            const groupIds = list.map((a) => idOf(a));
            const allInGroup = groupIds.every((id) => checked.includes(id));
            return (
              <div className="env-group" key={env}>
                <div className="env-head">
                  <input
                    type="checkbox"
                    className="env-check"
                    title={`Select all in ${env}`}
                    checked={allInGroup}
                    onChange={() => setChecked((c) => allInGroup
                      ? c.filter((id) => !groupIds.includes(id))
                      : [...new Set([...c, ...groupIds])])}
                  />
                  {env}
                </div>
                {list.map((a) => {
                  const r = resultById[idOf(a)];
                  return (
                    <label className="host-row he-host" key={idOf(a)}>
                      <input type="checkbox" checked={checked.includes(idOf(a))} onChange={() => toggle(idOf(a))} />
                      <span className={"dot " + (a.online === false ? "bad" : a.online === true ? "ok" : "")}
                            title={a.online === false ? "Offline" : a.online === true ? "Online" : ""} />
                      <span className="he-host-body">
                        <span className="he-host-name">{a.hostname || a.host_id}
                          {a.is_controller && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--accent, #3d7dd8)", color: "#fff" }} title="This host is the Sysible controller itself">controller</span>}
                          {r && r.ok && <span className="badge green" style={{ marginLeft: 6, fontSize: 10 }} title="Re-point task queued">migrating</span>}
                          {r && !r.ok && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--red, #e05656)", color: "#fff" }} title={r.error || "Failed"}>failed</span>}
                        </span>
                        <span className="he-host-meta">{a.address || a.ip || "—"}
                          {a.last_seen != null ? ` · seen ${fmtSeen(a.last_seen)}` : ""}
                          {r && !r.ok && r.error ? ` · ${r.error}` : ""}</span>
                      </span>
                    </label>
                  );
                })}
              </div>
            );
          })}
        </div>
      </fieldset>

      <div className="he-actions">
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>New controller address</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            Enter the NEW controller's IP or hostname (and port). This is a
            <strong> same-identity</strong> migration: the new controller must share this
            controller's database (a restore/failover to a new box or IP). Each selected
            agent keeps its identity, has its <code>SYSIBLE_CONTROLLER</code> re-pointed,
            and restarts.
          </p>
          <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <select value={scheme} onChange={(e) => setScheme(e.target.value)} style={{ maxWidth: 110 }}>
              <option value="https">https</option>
              <option value="http">http</option>
            </select>
            <input placeholder="new controller IP or hostname" value={host}
                   onChange={(e) => setHost(e.target.value)} style={{ flex: 1, minWidth: 200 }} />
            <span className="faint">Port</span>
            <input type="number" value={port} onChange={(e) => setPort(e.target.value)} style={{ maxWidth: 110 }} />
          </div>
          <div className="cmd-preview" style={{ marginTop: 10, whiteSpace: "pre-wrap" }}>{newUrl}</div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn danger" onClick={migrate}
                    disabled={!!busy || checked.length === 0 || !host.trim()}>
              {busy ? <span className="spin" /> : `Migrate selected hosts (${checked.length})`}
            </button>
          </div>
          {busy && (
            <div className="ok-text" role="status" aria-live="polite"
                 style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
              <span className="spin" aria-hidden="true" />{busy}
            </div>
          )}
          {err && <div className="error-box" style={{ marginTop: 8 }}>{err}</div>}
          {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
        </fieldset>

        {results && (
          <fieldset className="tool-group-box"><legend>Results</legend>
            <table>
              <thead><tr><th>Host</th><th>Status</th></tr></thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.host_id}>
                    <td>{r.hostname || r.host_id}</td>
                    <td>{r.ok
                      ? <span className="ok-text">re-point queued</span>
                      : <span style={{ color: "var(--red, #e05656)" }}>{r.error || "failed"}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="faint" style={{ marginTop: 10 }}>
              A queued re-point restarts the agent, so it may not report the task result
              back. Confirm success by watching for each host checking in against the new
              controller.
            </p>
          </fieldset>
        )}
      </div>
    </div>
  );
}
