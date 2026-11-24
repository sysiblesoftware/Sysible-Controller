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

// The VM picker. It lists the VMs that are actually on the hosts checked on the
// left, grouped by host, each one a checkbox.
//
// It used to be a dropdown fed from ONE host — the single selected one — and a
// free-text box the rest of the time. Two complaints, one cause:
//
//   "How do I select all VMs except 1?"   You could not. The field held one
//   name, or several typed by hand, or the word "all". Excluding a single VM
//   meant typing the names of every other one.
//
//   "The field box doesn't correlate to the host list on the left in any way."
//   It did not. With two or more hosts checked it stopped fetching anything and
//   became a text box, so the thing it was asking for (a VM) had no visible
//   relationship to the things that have VMs (the hosts).
//
// runTool already took a LIST of hosts and returned a row per host; this asked
// it for one and read results[0]. So the inventory was one argument away the
// whole time. Checking every VM and unchecking one is now the obvious gesture,
// and it produces the explicit list of the rest.
function RemoteSelect({ p, value, onChange, targets }) {
  const [byHost, setByHost] = useState({});     // host label -> VM names
  const [failed, setFailed] = useState({});     // host label -> why not
  const [loading, setLoading] = useState(false);
  const [manual, setManual] = useState(false);
  const loadedFor = useRef(null);
  const key = targets.join(",");

  async function load() {
    setLoading(true);
    try {
      const r = await api.runTool(p.source, targets, {});
      const ok = {}, bad = {};
      for (const row of r.results || []) {
        const lines = String(row.stdout || "")
          .split("\n").map((x) => x.trim()).filter(Boolean);
        // A host that answered with nothing has no VMs; one that could not be
        // asked is a different thing and says so rather than reading as empty.
        if (lines.length) ok[row.host] = lines;
        else if (!row.ok) bad[row.host] = row.error || row.stderr || "could not list VMs";
        else ok[row.host] = [];
      }
      setByHost(ok); setFailed(bad);
    } catch (e) {
      setByHost({}); setFailed({ "": e.message || "could not list VMs" });
    } finally { setLoading(false); }
  }

  useEffect(() => {
    if (!targets.length) { setByHost({}); setFailed({}); loadedFor.current = null; return; }
    if (loadedFor.current === key) return;
    loadedFor.current = key;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const hosts = Object.keys(byHost).sort();
  const every = [];
  for (const h of hosts) for (const v of byHost[h]) if (!every.includes(v)) every.push(v);
  const cur = value ?? "";
  const isAll = cur.trim().toLowerCase() === "all";
  const chosen = new Set(isAll ? every : cur.split(/[\s,]+/).filter(Boolean));

  // Everything known -> the word, not a list of 240 names: it is the shorter
  // command, and it still covers a host whose VMs could not be listed.
  function commit(next) {
    const arr = Array.from(next);
    if (every.length && arr.length === every.length && every.every((v) => next.has(v))) {
      onChange(p.name, "all");
    } else {
      onChange(p.name, arr.join(" "));
    }
  }
  // vm_info takes exactly ONE domain. Handing it "a b c" because the picker let
  // you tick three would quote the lot as a single name, so a field that cannot
  // hold a list does not offer one.
  const multi = p.multi === true;
  const toggle = (v) => {
    if (!multi) { onChange(p.name, chosen.has(v) ? "" : v); return; }
    const n = new Set(chosen);
    if (n.has(v)) n.delete(v); else n.add(v);
    commit(n);
  };

  // ---- nothing checked on the left: say that, and do not offer "all" --------
  if (!targets.length) {
    return (
      <>
        <input type="text" value={cur} placeholder={p.placeholder || p.help || ""}
               onChange={(e) => onChange(p.name, e.target.value)} />
        <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>
          Check one or more hosts on the left to choose from their VMs.
        </div>
      </>
    );
  }

  // ---- typing, or nothing to list -----------------------------------------
  if (manual || (!loading && !every.length)) {
    // SAY WHY. _VIRSH exits with "virsh (libvirt) is not installed on this
    // host", which is the whole answer -- those hosts are not hypervisors, so
    // there is nothing to pick and nothing to type either. The old wording
    // ("Could not list VMs on arch-01, arch-02, arch-03. Type a name instead.")
    // threw that away and then advised typing a name that would fail on every
    // one of them.
    const noHyp = Object.keys(failed).filter((h) =>
      /not installed|not found|no such file/i.test(failed[h] || ""));
    const otherFail = Object.keys(failed).filter((h) => !noHyp.includes(h));
    const why = loading ? "Reading the VMs on the selected host(s)…"
      : noHyp.length && !otherFail.length && !every.length
        ? `${noHyp.join(", ")} ${noHyp.length === 1 ? "is" : "are"} not running libvirt, `
          + `so ${noHyp.length === 1 ? "it has" : "they have"} no VMs. Check a host that does.`
        : otherFail.length
          ? `Could not list VMs on ${otherFail.join(", ")} (${failed[otherFail[0]]}).`
          : noHyp.length
            ? `Not running libvirt: ${noHyp.join(", ")}.`
            : every.length ? "" : "No VMs found on the selected host(s) — type a name if you know it.";
    return (
      <>
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          <input type="text" value={cur} style={{ flex: 1 }}
                 placeholder={p.placeholder || p.help || ""}
                 onChange={(e) => onChange(p.name, e.target.value)} />
          {loading && <span className="spin" />}
          {p.multi === true && !isAll && (
            <button type="button" className="btn ghost sm"
                    title="Act on every VM on each selected host"
                    onClick={() => onChange(p.name, "all")}>All VMs</button>
          )}
          {every.length > 0 && (
            <button type="button" className="btn ghost sm" title="Choose from the VMs on the checked hosts"
                    onClick={() => setManual(false)}>▾ list</button>
          )}
        </div>
        {isAll
          ? <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>
              Every VM on {targets.length === 1 ? "the selected host" : `all ${targets.length} selected hosts`}.
            </div>
          : why ? <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>{why}</div> : null}
      </>
    );
  }

  // ---- the list ------------------------------------------------------------
  const n = chosen.size;
  return (
    <>
      <div className="row" style={{ gap: 6, alignItems: "center", marginBottom: 4 }}>
        <span className="faint" style={{ fontSize: 11.5 }}>
          {n === 0 ? (multi ? "No VMs selected" : "No VM selected")
            : isAll ? `All ${every.length} VM(s)`
              : multi ? `${n} of ${every.length} VM(s)` : Array.from(chosen)[0]}
          {hosts.length > 1 ? ` on ${hosts.length} hosts` : ""}
        </span>
        <div className="spacer" style={{ flex: 1 }} />
        {multi && (
          <button type="button" className="btn ghost sm" onClick={() => onChange(p.name, "all")}
                  title="Every VM on every checked host">All</button>
        )}
        <button type="button" className="btn ghost sm" onClick={() => onChange(p.name, "")}
                title="Clear the selection">None</button>
        <button type="button" className="btn ghost sm" onClick={() => setManual(true)}
                title="Type a name instead">✎</button>
      </div>
      <div className="vm-pick">
        {hosts.map((h) => (
          <div key={h}>
            {hosts.length > 1 && (
              <div className="vm-pick-host">{h}{byHost[h].length ? "" : " — no VMs"}</div>
            )}
            {byHost[h].map((v) => (
              <label key={h + "/" + v}
                     className={"checkrow" + (chosen.has(v) ? " on" : "")}>
                <input type={multi ? "checkbox" : "radio"} name={multi ? undefined : "vmpick_" + p.name}
                       checked={chosen.has(v)} onChange={() => toggle(v)} />
                <span className="mono">{v}</span>
              </label>
            ))}
          </div>
        ))}
        {Object.entries(failed).map(([h, w]) => (
          <div key={"f" + h} className="vm-pick-note">
            {h || "a host"} — {/not installed|not found|no such file/i.test(w || "")
              ? "not running libvirt" : w}
          </div>
        ))}
      </div>
    </>
  );
}
