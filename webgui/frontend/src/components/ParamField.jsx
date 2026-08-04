import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Renders one action parameter from the /api/tools catalog: text / number /
// password / select / checkbox / select-remote — the types the registry uses.
// `targets` is the set of checked hosts; only select-remote needs it (to fetch
// its option list from a single host).
export default function ParamField({ p, value, onChange, targets = [] }) {
  if (p.type === "checkbox") {
    return (
      <div className="checkrow">
        <input id={"p_" + p.name} type="checkbox" checked={Boolean(value)}
               onChange={(e) => onChange(p.name, e.target.checked)} />
        <label htmlFor={"p_" + p.name}>{p.label}</label>
      </div>
    );
  }
  return (
    <label className="field">
      <span>
        {p.label}{p.required ? " *" : ""}
        {p.help ? <span className="faint"> — {p.help}</span> : null}
      </span>
      {p.type === "select" ? (
        <select value={value ?? ""} onChange={(e) => onChange(p.name, e.target.value)}>
          {(p.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : p.type === "select-remote" ? (
        <RemoteSelect p={p} value={value} onChange={onChange} targets={targets} />
      ) : (
        <input
          type={p.type === "password" ? "password" : p.type === "number" ? "number" : "text"}
          value={value ?? ""}
          placeholder={p.placeholder || p.help || ""}
          onChange={(e) => onChange(p.name, e.target.value)}
        />
      )}
    </label>
  );
}

// A dropdown whose options are fetched live from ONE selected host by running a
// hidden helper action (p.source) and splitting its stdout into lines. Used for
// the VM-name picker: check a host, and its recognized VMs appear as a menu.
// Falls back to a free-text input when the list can't be used — no host / many
// hosts checked, the fetch failed, nothing was returned, or the operator wants
// to type a value the list doesn't contain (e.g. several names, or 'all').
function RemoteSelect({ p, value, onChange, targets }) {
  const single = targets.length === 1 ? targets[0] : null;
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [manual, setManual] = useState(false);
  const loadedFor = useRef(null);

  async function load(host) {
    setLoading(true); setError("");
    try {
      const r = await api.runTool(p.source, [host], {});
      const row = (r.results || [])[0] || {};
      const lines = String(row.stdout || "")
        .split("\n").map((s) => s.trim()).filter(Boolean);
      // A failed run (agent offline, virsh missing) yields an error, not a list.
      if (!row.ok && !lines.length) setError(row.stderr || row.error || "Could not list VMs.");
      setOptions(lines);
    } catch (e) {
      setError(e.message || "Could not list VMs."); setOptions([]);
    } finally { setLoading(false); }
  }

  // (Re)load whenever the single selected host changes; clear when the target
  // set is no longer a single host.
  useEffect(() => {
    if (single && loadedFor.current !== single) { loadedFor.current = single; load(single); }
    if (!single) { loadedFor.current = null; setOptions([]); setError(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [single]);

  const cur = value ?? "";
  // Force manual entry when we can't offer a usable list, or the current value
  // isn't one of the fetched names (typed name / multiple / 'all').
  const listUsable = Boolean(single) && options.length > 0;
  const valueInList = cur === "" || options.includes(cur);
  const useManual = manual || !listUsable || !valueInList;

  if (useManual) {
    // Explain WHY this is a text box (not a menu) so the picker is discoverable
    // rather than looking like a plain field. The menu needs exactly one host.
    let hint = "";
    if (targets.length === 0) hint = "Check one host on the left to pick from its VMs.";
    else if (targets.length > 1) hint = `Pick a VM per host isn't possible for ${targets.length} hosts — type a name, or 'all'.`;
    else if (loading) hint = "Loading VMs on the selected host…";
    else if (error) hint = `Couldn't list VMs (${error}). Type a name instead.`;
    else if (single && options.length === 0) hint = "No VMs found on the selected host — type a name if you know it.";
    return (
      <>
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          <input
            type="text" value={cur} style={{ flex: 1 }}
            placeholder={p.placeholder || p.help || ""}
            onChange={(e) => onChange(p.name, e.target.value)}
          />
          {loading && <span className="spin" />}
          {listUsable && (
            <button type="button" className="btn ghost sm"
                    title="Choose from the VMs on the selected host"
                    onClick={() => { setManual(false); if (!valueInList) onChange(p.name, ""); }}>
              ▾ list
            </button>
          )}
        </div>
        {hint && <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>{hint}</div>}
      </>
    );
  }

  return (
    <div className="row" style={{ gap: 6, alignItems: "center" }}>
      <select value={cur} style={{ flex: 1 }}
              onChange={(e) => onChange(p.name, e.target.value)} disabled={loading}>
        <option value="">{loading ? "Loading VMs…" : "— choose a VM —"}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      <button type="button" className="btn ghost sm" title="Refresh the VM list"
              onClick={() => single && load(single)} disabled={loading}>⟳</button>
      <button type="button" className="btn ghost sm"
              title="Type a name instead (e.g. several names, or 'all')"
              onClick={() => setManual(true)}>✎</button>
      {error && <span className="faint" title={error} style={{ fontSize: 11 }}>⚠</span>}
    </div>
  );
}
