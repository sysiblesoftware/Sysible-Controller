import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { confirmDialog } from "../confirmDialog.js";

// Host Enrollment — agent bundle (download + curl), enrolled hosts grouped by
// environment with multi-select, environment assignment, sudo policy, AND the
// full Webserver Portal administration (status/port/credentials/sessions/files),
// since the portal exists to serve enrollment. One page, mirrors the desktop.

// Short names for the hosts-list "From" column; the full label is the title.
const SRC_SHORT = {
  own: "this list",
  server: "server /etc/hosts",
};

function fmtSeen(v) {
  if (v === null || v === undefined || v === "") return "—";
  let d;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v); if (n < 1e12) n *= 1000; d = new Date(n);
  } else d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}
const fmtTime = fmtSeen;

export default function HostEnrollment() {
  const [agents, setAgents] = useState([]);
  // Hosts the Controller knows ONLY over SSH -- a host record with no agent
  // reporting. This screen listed /agents alone, so a host enrolled over SSH
  // whose agent never checked in was absent from the one page whose job is to
  // list enrolled hosts, while the topology drew it, Flashback counted it and
  // the enroll form refused it as "already managed". Three screens, three
  // answers. /api/hosts is the Controller's OWN merge of both lists and marks
  // each entry's kind, so this takes the "ssh" ones rather than re-deriving a
  // match here and getting a fourth answer.
  const [sshOnly, setSshOnly] = useState([]);
  const [envs, setEnvs] = useState([]);
  const [sudoDefaults, setSudoDefaults] = useState({});
  const [edition, setEdition] = useState({});
  const [portal, setPortal] = useState({});
  const [cfg, setCfg] = useState({});
  const [checked, setChecked] = useState([]);
  const [enrollPaused, setEnrollPaused] = useState(false);
  const [collapsed, setCollapsed] = useState({});
  const [assignEnv, setAssignEnv] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");   // in-progress label for host actions
  const [copied, setCopied] = useState(false);
  const [copiedAnsible, setCopiedAnsible] = useState(false);
  const [curlUseIp, setCurlUseIp] = useState(false);   // curl one-liner: address by IP (no DNS) vs hostname
  // Push enrollment: the Controller reaches OUT to the host. Everything the
  // backend needs is here — name, address, an account and its password, used
  // once and never stored.
  // Bulk enrollment from a hosts file. `rows` is every line the Controller read
  // back, skipped ones included — a 40-line file that yields 12 candidates has
  // to be able to say where the other 28 went.
  const [bulkText, setBulkText] = useState("");
  const [bulkRows, setBulkRows] = useState(null);
  const [bulkPick, setBulkPick] = useState({});   // line -> true
  const [bulkName, setBulkName] = useState({});   // line -> edited name
  const [bulkCreds, setBulkCreds] = useState({ username: "root", password: "", environment: "" });
  const [bulkProgress, setBulkProgress] = useState(null);
  // What the last enrollment run actually did, host by host. The only record
  // of a failure used to be one red box holding every error at once, replaced
  // by the next thing that happened -- so "it did not work" had no detail to
  // go with it, and a 40-host run reported a number. Written as the run goes,
  // so a long one can be watched rather than waited out.
  const [runLog, setRunLog] = useState([]);
  const [otherWaysOpen, setOtherWaysOpen] = useState(false);
  const [copiedLog, setCopiedLog] = useState(false);
  const [serverHosts, setServerHosts] = useState(null);
  // The names this Controller can turn into addresses by itself.
  const [namesText, setNamesText] = useState("");
  const [namesSaved, setNamesSaved] = useState("");   // what is stored, to spot edits
  const [namesSources, setNamesSources] = useState(null);
  const [checkName, setCheckName] = useState("");
  const [checkResult, setCheckResult] = useState(null);
  const [namesEntries, setNamesEntries] = useState(null);
  const [addName, setAddName] = useState("");
  const [addAddr, setAddAddr] = useState("");
  // The names fold. Shut by default -- it was a whole second panel restating
  // the list above it -- and opened by the one thing it is for: a name that
  // will not resolve, or a hosts file this container can no longer see.
  const [namesFoldOpen, setNamesFoldOpen] = useState(false);
  const [portPort, setPortPort] = useState("");
  const [portalBusy, setPortalBusy] = useState("");
  const [tab, setTab] = useState("hosts");
  const [sudoEnv, setSudoEnv] = useState("");
  // Portal administration
  const [history, setHistory] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [uploads, setUploads] = useState([]);
  const [downloads, setDownloads] = useState([]);
  const [cur, setCur] = useState(""); const [nu, setNu] = useState("");
  const [np, setNp] = useState(""); const [np2, setNp2] = useState("");

  function loadPortal() {
    api.portalStatus().then((s) => {
      setPortal(s || {});
      const p = s && (s.configured_port || s.port);
      if (p) setPortPort(String(p));
    }).catch(() => {});
  }
  function loadHistory() { api.portalLoginHistory().then((d) => setHistory(d.history || [])).catch(() => {}); }
  function loadSessions() { api.portalSessions().then((d) => setSessions(d.sessions || [])).catch(() => {}); }
  function loadUploads() { api.portalUploads().then((d) => setUploads(d.files || [])).catch(() => {}); }
  function loadDownloads() { api.portalDownloads().then((d) => setDownloads(d.files || [])).catch(() => {}); }
  function load(surfaceErr = false) {
    // surfaceErr only on the first load: a transient timeout during the
    // post-action refresh shouldn't paint a red error over an action that
    // actually succeeded (you'd see the green "done" and a scary timeout at once).
    api.agents().then((d) => setAgents(d.agents || [])).catch((e) => { if (surfaceErr) setErr(e.message); });
    api.hosts()
      .then((d) => setSshOnly((Array.isArray(d?.hosts) ? d.hosts : [])
                              .filter((h) => h && h.kind === "ssh")))
      .catch(() => {});
    api.environments().then((d) => setEnvs(d.environments || [])).catch(() => {});
    api.envSudoDefaults().then((d) => setSudoDefaults(d || {})).catch(() => {});
    api.edition().then(setEdition).catch(() => {});
    api.controllerConfig().then((c) => setCfg(c || {})).catch(() => {});
    // Portal STATUS is lightweight and the "Enroll a Host" tab shows it, so keep
    // it here. The heavier portal admin data (login history, sessions, uploads,
    // downloads) is only shown on the Portal tab — loading it on every host
    // action piled ~4 extra controller calls onto each refresh, contending with
    // the essential ones on the single-process controller (which could then
    // read-timeout). It's loaded lazily when the Portal tab opens instead.
    loadPortal();
    api.enrollmentPause().then((d) => setEnrollPaused(!!(d && d.paused))).catch(() => {});
  }
  // The name list is only consulted when an address is left blank, but it has
  // to be LOADED up front: the point of showing it is that "the Controller
  // cannot resolve this name" and "it was never told any names" are the same
  // failure from the console and have different fixes.
  const loadNames = useCallback(async () => {
    try {
      const d = await api.names();
      setNamesText(d.text || "");
      setNamesSaved(d.text || "");
      // isArray, not a truthiness check. A setter given a FUNCTION is an
      // updater to React, and it calls it -- so a response whose `entries` is
      // anything but an array (Array.prototype.entries is a function, and an
      // array response has one) throws inside React and blanks the console.
      setNamesSources(Array.isArray(d.sources) ? d.sources : []);
      setNamesEntries(Array.isArray(d.entries) ? d.entries : []);
    } catch (e) {
      // Not fatal: enrollment by address works without any of this.
      setNamesSources([]);
      setNamesEntries([]);
    }
  }, []);

  function applyNames(d) {
    if (Array.isArray(d?.entries)) setNamesEntries(d.entries);
    if (Array.isArray(d?.sources)) setNamesSources(d.sources);
    // The answer below the list was true of the list BEFORE this change.
    setCheckResult(null);
  }

  async function addEntry() {
    setBusy("add-name"); setErr(""); setMsg("");
    try {
      applyNames(await api.addName(addName.trim(), addAddr.trim()));
      setAddName(""); setAddAddr("");
      // The text box behind the disclosure is now out of date; refresh it so
      // "Replace list" cannot quietly put back what was just removed.
      loadNames();
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function removeEntry(name) {
    setBusy("rm-" + name); setErr(""); setMsg("");
    try { applyNames(await api.removeName(name)); loadNames(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function runCheck() {
    setBusy("check"); setErr(""); setCheckResult(null);
    try { setCheckResult(await api.checkName(checkName.trim())); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  async function saveNames() {
    setBusy("names"); setErr(""); setMsg("");
    try {
      const d = await api.saveNames(namesText);
      setNamesSaved(namesText);
      applyNames(d);
      setMsg(d.names === 1
        ? "1 name saved. You can now enroll it without an address."
        : `${d.names} names saved. You can now enroll them without an address.`);
    } catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  }

  useEffect(() => { load(true); }, []);
  useEffect(() => { loadNames(); }, [loadNames]);
  useEffect(() => {
    if (tab === "portal") { loadPortal(); loadHistory(); loadSessions(); loadUploads(); loadDownloads(); }
  }, [tab]);

  async function portalAct(key, fn, okMsg) {
    setPortalBusy(key); setErr(""); setMsg("");
    try { await fn(); if (okMsg) setMsg(okMsg); loadPortal(); }
    catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function saveCreds() {
    setErr(""); setMsg("");
    if (np !== np2) { setErr("New portal passwords don't match."); return; }
    setPortalBusy("creds");
    try {
      await api.portalSetCreds(nu.trim(), np, cur);
      setMsg("Portal credentials saved."); setCur(""); setNu(""); setNp(""); setNp2("");
      loadPortal(); loadHistory(); loadSessions();
    } catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function removeCreds() {
    if (!(await confirmDialog("Remove portal login access? Nobody can log in until new credentials are saved."))) return;
    setPortalBusy("remove"); setErr(""); setMsg("");
    try { await api.portalRemoveCreds(cur); setMsg("Portal login access removed."); setCur(""); loadPortal(); }
    catch (e) { setErr(e.message); }
    finally { setPortalBusy(""); }
  }
  async function revoke(s) {
    const id = s.id ?? s.session_id;
    try { await api.portalRevokeSession(id); loadSessions(); } catch (e) { setErr(e.message); }
  }
  async function stageDownload(e) {
    const file = e.target.files[0]; if (!file) return;
    setErr(""); try { await api.portalStageDownload(file); loadDownloads(); } catch (e2) { setErr(e2.message); }
    e.target.value = "";
  }
  const fname = (f) => (typeof f === "string" ? f : (f.name || f.filename || ""));

  // Every host this Controller considers enrolled, from both of its lists.
  // Agents first, so a host that has both keeps the agent row it has always
  // had; /api/hosts has already collapsed those duplicates and calls them
  // "merged", so nothing here is listed twice.
  const rows = useMemo(() => [
    ...agents,
    ...sshOnly.map((h) => ({
      ssh_only: true,
      // Prefixed, so it can never collide with a real host_id and can never be
      // posted to an agent route by accident.
      host_id: "ssh:" + (h.id || h.label),
      ssh_name: h.label || h.id,
      hostname: h.label || h.id,
      address: h.address || "",
      environment: h.environment || "",
      online: null,
    })),
  ], [agents, sshOnly]);

  const groups = useMemo(() => {
    const m = {};
    for (const a of rows) (m[a.environment || "Unassigned"] ||= []).push(a);
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [rows]);

  const idOf = (a) => a.host_id || a.id;
  const toggle = (id) => setChecked((c) => c.includes(id) ? c.filter((x) => x !== id) : [...c, id]);

  async function run(fn, okMsg, busyLabel) {
    setErr(""); setMsg(""); setBusy(busyLabel || "");
    try { await fn(); if (okMsg) setMsg(okMsg); }
    catch (e) { setErr(e.message); }
    // Refresh regardless of outcome: some actions (e.g. Disenroll) commit the
    // server-side change and THEN throw to carry a warning (agent teardown not
    // confirmed). Refreshing only on success left the just-removed hosts sitting
    // in the list, so a successful disenroll looked like it failed.
    finally { setBusy(""); load(); }
  }


  async function newEnvironment() {
    // The single create entry point: make an environment on its own — no hosts need
    // be checked. Once it exists, pick it in the dropdown and Set Environment to
    // assign checked hosts to it.
    const name = (window.prompt("New environment name:") || "").trim();
    if (!name) return;
    await run(async () => { await api.createEnvironment(name); setAssignEnv(name); },
      `Created environment '${name}'.`, "Creating environment…");
  }

  async function assignEnvironment() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    const env = assignEnv;
    await run(async () => { for (const id of checked) await api.setHostEnvironment(id, env); },
      `Assigned ${checked.length} host(s) to ${env || "(unassigned)"}.`);
  }

  async function removeEnvironment() {
    const env = assignEnv;
    if (!env) { setErr("Pick an environment from the dropdown to remove."); return; }
    // Don't orphan hosts: an environment with hosts still in it must be emptied
    // first (reassign them with Set Environment).
    const inUse = agents.filter((a) => (a.environment || "") === env).length;
    if (inUse > 0) {
      setErr(`Can't remove '${env}' — ${inUse} host(s) are still assigned to it. Reassign them (Set Environment) first.`);
      return;
    }
    if (!(await confirmDialog(`Remove the environment '${env}'? It has no hosts assigned.`))) return;
    await run(async () => { await api.deleteEnvironment(env); setAssignEnv(""); },
      `Removed environment '${env}'.`);
  }

  async function setSudo(required) {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    await run(async () => { for (const id of checked) await api.setHostSudo(id, required); },
      `Set ${checked.length} host(s) to ${required ? "password sudo" : "passwordless (NOPASSWD)"}.`);
  }

  // Run `op(id)` for every checked host with bounded concurrency, NEVER aborting
  // the whole batch when one host fails or hangs. Returns the failures so the
  // caller can report "did N of M". This is what makes bulk actions usable on a
  // pile of offline zombies — the old for-await loop stopped at the first error,
  // so one stuck host left the rest untouched (the "buttons don't work" report).
  async function bulkOp(ids, op, concurrency = 8) {
    const failures = [];
    let i = 0;
    const worker = async () => {
      while (i < ids.length) {
        const id = ids[i++];
        try { await op(id); }
        catch (e) { failures.push(`${id}: ${e?.message || e}`); }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, ids.length) }, worker));
    return failures;
  }

  function summarize(ids, failures, verb) {
    const done = ids.length - failures.length;
    if (!failures.length) return null;
    throw new Error(`${verb} ${done} of ${ids.length}. ${failures.length} failed:\n` +
      failures.slice(0, 12).join("\n") + (failures.length > 12 ? `\n…and ${failures.length - 12} more` : ""));
  }

  // Ask the Controller to read the pasted file. It enrolls nothing — every
  // candidate is posted back through the ordinary one-host route below, so a
  // bulk import gets the same duplicate-IP check, host limit and superuser gate
  // that typing one in by hand does.
  // What the Enroll button will act on. At component scope because the button
  // has to know how many there are to label itself, and a second copy of this
  // filter living next to the button is how the label and the action drift.
  // A mount frozen at container-creation time reads perfectly and serves the
  // file as it was then, forever. Saying nothing is how "I added it to the
  // hosts file and it is still not here" stays unanswerable.
  const staleServerHosts = useMemo(() => {
    const src = (namesSources || []).find((x) => x.key === "server");
    return src && src.present ? (src.stale_note || "") : "";
  }, [namesSources]);

  // Open it on the failure it answers, rather than leaving an operator to
  // find a closed fold while the error tells them to use what is inside it.
  useEffect(() => {
    if (staleServerHosts) setNamesFoldOpen(true);
  }, [staleServerHosts]);
  useEffect(() => {
    if (/resolve/i.test(err || "")) setNamesFoldOpen(true);
  }, [err]);

  const picked = useMemo(
    () => (bulkRows || []).filter((r) => r.enrollable && bulkPick[r.line]),
    [bulkRows, bulkPick]);

  async function loadServerHosts() {
    setErr(""); setMsg(""); setBusy("server-hosts");
    try {
      const d = await api.serverHosts();
      setServerHosts(d);
      if (!d.text || !d.text.trim()) {
        setErr(d.present
          ? `${d.path} is there but has nothing in it.`
          : `The Controller cannot read ${d.path}.` +
            (d.in_container
              ? " Mount the server's /etc into the Controller " +
                "(/etc:/host/etc:ro) and recreate it."
              : ""));
        return;
      }
      setBulkText(d.text);
      await previewHostsFile(d.text);
      // A mount frozen at container-creation time reads perfectly and serves
      // yesterday's file, so saying nothing here is how "I added it and it is
      // still not there" becomes unanswerable.
      if (d.stale_note) setErr(`That is ${d.path}, but ${d.stale_note}.`);
    } catch (e) {
      setErr(e.message || "Could not read the server's hosts file.");
    } finally { setBusy(""); }
  }

  async function previewHostsFile(text) {
    // The text, not the state: "use the server's hosts file" and the file
    // picker have it in hand and setState has not landed yet. Reading the
    // state here previewed the list as it was one edit ago.
    const body = text === undefined ? bulkText : text;
    setErr(""); setMsg(""); setBusy("bulk-read");
    try {
      const d = await api.hostsFilePreview(body);
      const rows = (d && d.rows) || [];
      setBulkRows(rows);
      const pick = {}, names = {};
      rows.forEach((r) => {
        names[r.line] = r.name;
        // Everything enrollable is ticked, EXCEPT a name that already exists:
        // that one needs a human to look at it before it overwrites a record.
        if (r.enrollable && !r.conflict) pick[r.line] = true;
      });
      setBulkPick(pick); setBulkName(names);
      if (!rows.some((r) => r.enrollable)) {
        setErr("Nothing in that list can be enrolled — see the reasons beside each line.");
      }
    } catch (e) {
      setErr(e.message || "Could not read that file.");
      setBulkRows(null);
    } finally { setBusy(""); }
  }

  async function enrollPicked() {
    if (!picked.length) { setErr("Tick at least one host."); return; }
    if (!bulkCreds.password) { setErr("Enter the SSH password to use."); return; }
    if (!(await confirmDialog(
      `Enroll ${picked.length} host(s)?\n\nThe Controller connects to each over SSH ` +
      `as "${bulkCreds.username}", installs its key, and enrolls the agent. The password ` +
      `is used once per host and is never stored.`))) return;

    setErr(""); setMsg(""); setBusy("bulk-enroll");
    const done = [], failed = [];
    setBulkProgress({ done: 0, total: picked.length, current: "" });
    setRunLog(picked.map((r) => ({
      name: (bulkName[r.line] || r.name).trim(),
      target: r.target || r.address || "",
      state: "waiting", detail: "", at: null,
    })));
    const mark = (i, patch) => setRunLog((l) =>
      l.map((e, n) => (n === i ? { ...e, ...patch } : e)));
    // Serial, not the concurrency bulkOp uses elsewhere. Each of these opens an
    // SSH session and runs a command on a different machine; eight at once is
    // eight passwords in flight and eight ways for a half-finished enrollment to
    // interleave in the activity log. Slower, and legible when one fails.
    for (let i = 0; i < picked.length; i++) {
      const r = picked[i];
      const name = (bulkName[r.line] || r.name).trim();
      setBulkProgress({ done: i, total: picked.length, current: name });
      mark(i, { state: "running", at: Date.now() });
      try {
        await api.enrollSsh({
          // The TARGET. A line that was just a name has no address, and sending
          // "" would enroll nothing -- the Controller resolves the name itself.
          name, ip: r.target || r.address,
          username: bulkCreds.username.trim(), password: bulkCreds.password,
          environment: bulkCreds.environment,
        });
        done.push(name);
        mark(i, { state: "ok", detail: "enrolled", at: Date.now() });
      } catch (e) {
        const why = e.message || "failed";
        failed.push(`${name}: ${why}`);
        mark(i, { state: "fail", detail: why, at: Date.now() });
      }
    }
    setBulkProgress(null);
    setBusy("");
    // Both halves, always. "Enrolled 11 of 12" with no word on the twelfth is
    // the report that sends someone hunting through the activity log.
    if (done.length) setMsg(`Enrolled ${done.length} of ${picked.length} host(s).`);
    if (failed.length) {
      setErr(`${failed.length} of ${picked.length} failed — each one, and why, ` +
             `is in the run log below.`);
    } else {
      // Only on a clean run: leaving the list up after a partial failure is how
      // someone re-runs it and re-enrolls the ones that already worked.
      setBulkRows(null); setBulkText("");
    }
    setBulkCreds((c) => ({ ...c, password: "" }));
    load();
  }

  async function disenrollChecked() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    // The SSH record and the agent are two records, and "Disenroll" removed
    // one of them. The controller's own cleanup only forgets an SSH record
    // whose hostname AND ip both match the agent -- which is right for the
    // auto-created shadow it was written for, and never matches the record an
    // operator's own SSH enrollment created under a name they chose. So a host
    // enrolled as "ubuntu" and disenrolled came back as "already managed as
    // 'ubuntu'" at the same address, with the blocking record on no screen and
    // no way to remove just that one.
    //
    // Matched on ADDRESS, against the Controller's own merged host list, which
    // has already decided what counts as one machine -- there is no new idea of
    // host identity here. Done from the console rather than widening the
    // agent-triggered deletion: this runs because an operator ticked a row and
    // confirmed, so none of it rests on what an agent says it is called.
    const addrsOf = (r) => [r.address, r.ip].filter(Boolean).map(String);
    const goneAddrs = new Set(
      rows.filter((r) => checked.includes(idOf(r))).flatMap(addrsOf));
    const strays = sshOnly.filter((h) => addrsOf(h).some((a) => goneAddrs.has(a)));
    if (!(await confirmDialog(`Disenroll ${checked.length} host(s)? The agent takes ITSELF off each host — service, installed files, pinned certificate and state — the next time it checks in, which is seconds for a host that is online and whenever it next starts for one that is not. Nothing is left behind and nothing has to be run on the host.${strays.length ? `\n\nThe SSH connection record${strays.length === 1 ? "" : "s"} for the same address (${strays.map((h) => h.label || h.id).join(", ")}) ${strays.length === 1 ? "is" : "are"} removed too, so the host can be enrolled again.` : ""}\n\nForce Delete does the same and also purges each host's enrollment token, so an agent that has already lost touch cannot come back on the old one.`))) return;
    const ids = [...checked];
    await run(async () => {
      const failures = await bulkOp(ids, (id) => api.removeHost(id));
      // 404 is the ordinary answer here: the controller's own cleanup got to
      // it first. Only a real failure is worth a word.
      for (const h of strays) {
        try { await api.forgetHost(h.label || h.id); } catch { /* already gone */ }
      }
      setChecked([]);
      summarize(ids, failures, "Disenrolled");
    }, `Disenrolled ${ids.length} host(s).`, `Disenrolling ${ids.length} host(s)…`);
  }

  async function forceDeleteChecked() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    if (!(await confirmDialog(
      `Force-delete ${checked.length} host(s) from the console?\n\n` +
      "Use this for ZOMBIE agents — a broken build that keeps heartbeating but " +
      "can't cleanly disenroll. This drops the controller record immediately " +
      "WITHOUT waiting for the agent to tear itself down, purges its enrollment " +
      "token, and locks out its secret on the next heartbeat.\n\n" +
      "The agent process may still be running on the host — stop it there with " +
      "disenroll_agent.sh (or kill its service) afterwards. Continue?"))) return;
    const ids = [...checked];
    await run(async () => {
      const failures = await bulkOp(ids, (id) => api.removeHost(id, true));
      setChecked([]);
      summarize(ids, failures, "Force-deleted");
    }, `Force-deleted ${ids.length} host(s) from the console.`,
      `Force-deleting ${ids.length} host(s)…`);
  }

  async function toggleEnrollPause() {
    const next = !enrollPaused;
    if (next && !(await confirmDialog(
      "Pause ALL new agent enrollment?\n\n" +
      "No new host can enroll until you resume — this is the emergency brake for a " +
      "runaway that re-enrolls faster than you can delete it. Existing agents keep " +
      "running normally. Remember to resume once the source host is fixed."))) return;
    await run(async () => {
      const d = await api.setEnrollmentPause(next);
      setEnrollPaused(!!(d && d.paused));
    }, next ? "New enrollment PAUSED. Clear the runaway, then resume." : "Enrollment resumed.",
      next ? "Pausing enrollment…" : "Resuming enrollment…");
  }

  async function revokeChecked() {
    if (checked.length === 0) { setErr("Check one or more hosts first."); return; }
    if (!(await confirmDialog(
      `Revoke ${checked.length} host(s)?\n\n` +
      "Each agent is locked out immediately — it can't heartbeat, poll, or report — " +
      "but the inventory record is KEPT (unlike Force Delete). Use this to stop a " +
      "runaway/compromised fleet in one click without losing the records; Restore " +
      "or Force-Delete them afterwards. Continue?"))) return;
    const ids = [...checked];
    await run(async () => {
      const failures = await bulkOp(ids, (id) => api.revokeHost(id));
      setChecked([]);
      summarize(ids, failures, "Revoked");
    }, `Revoked ${ids.length} host(s).`, `Revoking ${ids.length} host(s)…`);
  }

  async function forgetSshHost(a, e) {
    e.preventDefault(); e.stopPropagation();
    const name = a.ssh_name;
    if (!(await confirmDialog(
      `Forget ${name}?\n\nThis removes the Controller's SSH record for it. ` +
      `Nothing on the host itself is touched, and no agent is disenrolled — ` +
      `this is the record that was making the host count as managed.`))) return;
    await run(async () => { await api.forgetHost(name); },
              `Forgot the SSH record for ${name}.`, "forget-" + name);
  }

  async function revokeHost(a, e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    const name = a.hostname || a.host_id;
    if (!(await confirmDialog(`Revoke ${name}? Its agent is locked out — it can't heartbeat, poll, or report — until you re-enroll the host. Use this for a host you believe is compromised or tampered.`))) return;
    await run(async () => { await api.revokeHost(idOf(a)); }, `Revoked ${name}. Re-enroll the host to restore it.`,
      `Revoking ${name}…`);
    load();
  }

  async function resumeHost(a, e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    const name = a.hostname || a.host_id;
    await run(async () => { await api.resumeHost(idOf(a)); }, `Cleared the integrity quarantine on ${name}; it re-seals on the next heartbeat.`);
    load();
  }

  async function restoreHost(a, e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    const name = a.hostname || a.host_id;
    if (!(await confirmDialog(`Restore ${name}? Un-revoke it in place, keeping its existing agent secret so a still-installed agent resumes immediately — no re-enroll needed.`))) return;
    await run(async () => { await api.restoreHost(idOf(a)); }, `Restored ${name}; its agent resumes on the next heartbeat.`, `Restoring ${name}…`);
  }

  async function reissueHost(a, e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    const name = a.hostname || a.host_id;
    if (!(await confirmDialog(
      `Reissue enrollment for ${name}?\n\n` +
      "Re-enrolling an existing host (after a reinstall that wiped its agent secret) " +
      "is refused with an ordinary token, so a leaked token can't hijack a host. This " +
      "mints a single-use token bound to THIS host only. Run the agent with it on the " +
      "reinstalled machine to reclaim this same record. Continue?"))) return;
    await run(async () => {
      const r = await api.reissueToken(idOf(a));
      const tok = r && (r.token || (r.data && r.data.token));
      if (tok) {
        try { await navigator.clipboard.writeText(tok); } catch { /* clipboard optional */ }
        setMsg(`Reissue token for ${name} (copied): ${tok} — run the agent on the reinstalled host with this token to reclaim its record. Single-use; expires per policy.`);
      }
    }, "", `Reissuing ${name}…`);
  }

  // curl one-liner (built from portal status + controller config).
  // The controller may be addressed by hostname OR IP. If the console is configured
  // with a hostname, a target host with no DNS for it can't resolve it — so when an IP
  // is also known, offer a toggle to build the command against the IP instead (curl
  // uses -k, so the self-signed cert not covering the IP doesn't matter).
  const curlHostName = cfg.hostname || (cfg.address_mode !== "ip" ? cfg.address : "") || "";
  const curlIp = cfg.ip || "";
  const curlHost = (curlUseIp && curlIp) ? curlIp
    : (curlHostName || curlIp || "<this machine's address>");
  const curlPort = portPort || portal.configured_port || portal.port || 8090;
  const curlUser = portal.credentials_configured ? portal.username : "<username>";
  const curlCmd =
    `curl -k -sS -f -u '${curlUser}:<password>' -o sysible-agent-bundle.zip ` +
    `"https://${curlHost}:${curlPort}/cli/bundle" ` +
    `&& unzip -o sysible-agent-bundle.zip -d sysible-agent-bundle ` +
    `&& cd sysible-agent-bundle && chmod +x run_agent.sh && sudo ./run_agent.sh`;

  // Mass enrollment: the same portal-authenticated headless install, wrapped as an
  // Ansible ad-hoc shell command so an operator can enroll a whole inventory group in
  // one shot. `--become` supplies privilege (so no inner sudo), and the URL's inner
  // double-quotes are dropped so the whole script fits inside `-a "…"` without a quote
  // collision. Each host's own curl pulls its own fresh one-time enrollment token.
  const ansibleCmd =
    `ansible <group> -i <inventory> --become -m shell -a ` +
    `"curl -k -sS -f -u '${curlUser}:<password>' -o /tmp/sysible-agent-bundle.zip ` +
    `https://${curlHost}:${curlPort}/cli/bundle ` +
    `&& unzip -o /tmp/sysible-agent-bundle.zip -d /tmp/sysible-agent-bundle ` +
    `&& cd /tmp/sysible-agent-bundle && chmod +x run_agent.sh && ./run_agent.sh"`;

  const reachable = `https://${cfg.address || "<controller>"}:${curlPort}`;

  const TABS = [["hosts", "Enrolled Hosts"], ["enroll", "Enroll a Host"],
                ["relay", "Relay / Bastion"], ["portal", "Webserver Portal"]];

  return (
    <div>
      {edition.host_limit != null && (
        <div className="muted" style={{ marginBottom: 12 }}>
          {(edition.edition || "Community")} edition — {edition.host_count ?? agents.length}/{edition.host_limit} hosts used
        </div>
      )}

      <div className="tabs" style={{ marginBottom: 16 }}>
        {TABS.map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>

      {/* On the Enrolled Hosts tab the status banner is rendered down in the
          actions column instead (under the buttons that triggered it); other
          tabs keep it here at the top. */}
      {tab !== "hosts" && err && <div className="error-box">{err}</div>}
      {tab !== "hosts" && msg && <div className="ok-text" style={{ marginBottom: 10 }}>{msg}</div>}

      {/* ============================ TAB: ENROLLED HOSTS ============================ */}
      {tab === "hosts" && (
      <div className="he-2col">
        <fieldset className="tool-group-box he-hosts">
          <legend>Enrolled Hosts (grouped by environment)</legend>
          <div className="ctl-row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
            <button className="btn ghost sm" onClick={() => setChecked(agents.map(idOf))}>Select All</button>
            <button className="btn ghost sm" onClick={() => setChecked([])}>Deselect All</button>
            <button className="btn ghost sm" onClick={() => setCollapsed(Object.fromEntries(groups.map(([e]) => [e, true])))}>Collapse All</button>
            <button className="btn ghost sm" onClick={() => setCollapsed({})}>Expand All</button>
            <button className="btn ghost sm" onClick={load}>Refresh</button>
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {rows.length === 0 && <div className="faint" style={{ padding: 8 }}>No hosts enrolled yet — use the “Enroll a Host” tab.</div>}
            {groups.map(([env, list]) => {
              const open = !collapsed[env];
              // Only agent rows. The bulk actions below all post to agent
              // routes, so ticking an SSH-only row would send "ssh:web-01" to
              // an endpoint that takes a host_id.
              const groupIds = list.filter((a) => !a.ssh_only).map((a) => idOf(a));
              const allInGroup = groupIds.length > 0
                && groupIds.every((id) => checked.includes(id));
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
                  {open && list.map((a) => (
                    <label className="host-row he-host" key={idOf(a)}>
                      {a.ssh_only
                        // No checkbox: every bulk action below is an agent
                        // route. A spacer, so the rows still line up.
                        ? <span style={{ width: 13, flex: "0 0 13px" }} />
                        : <input type="checkbox" checked={checked.includes(idOf(a))} onChange={() => toggle(idOf(a))} />}
                      <span className={"dot " + (a.ssh_only ? "warn" : a.online === false ? "bad" : a.online === true ? "ok" : "")}
                            title={a.ssh_only ? "Enrolled over SSH; no agent has reported"
                                   : a.online === false ? "Offline" : a.online === true ? "Online" : ""} />
                      <span className="he-host-body">
                        <span className="he-host-name">{a.hostname || a.host_id}{" "}
                          {a.is_controller && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--accent, #3d7dd8)", color: "#fff" }} title="This host is the Sysible controller itself">controller</span>}{" "}
                          {a.requires_sudo_password && <span className="badge" style={{ marginLeft: 6, fontSize: 10 }}>pw-sudo</span>}{" "}
                          {a.revoked && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--red, #e05656)", color: "#fff" }} title="Agent secret revoked — re-enroll to restore">revoked</span>}{" "}
                          {a.ssh_only && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--amber, #e0a83b)", color: "#1a2744" }}
                                               title="The Controller has an SSH record for this host, but no agent has ever reported from it. Everything that needs the agent — health, terminals, updates, config backup — is unavailable until one does.">no agent</span>}
                          {!a.revoked && !a.ssh_only && a.integrity_quarantined && <span className="badge" style={{ marginLeft: 6, fontSize: 10, background: "var(--amber, #e0a93c)", color: "#1a2744" }} title={(a.integrity_detail || []).join("\n") || "Integrity mismatch — dispatch paused"}>⚠ quarantined</span>}
                        </span>
                        <span className="he-host-meta">{a.address || a.ip || "—"}
                          {a.ssh_only
                            ? " · SSH only — no agent has reported"
                            : a.last_seen != null ? ` · seen ${fmtSeen(a.last_seen)}` : ""}</span>
                      </span>
                      <span className="he-host-actions" style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                        {a.revoked &&
                          <button className="btn ghost sm" title="Un-revoke in place, keeping the existing secret — the still-installed agent resumes immediately (no re-enroll)" onClick={(e) => restoreHost(a, e)}>Restore</button>}{" "}
                        {!a.revoked && a.integrity_quarantined &&
                          <button className="btn ghost sm" title="Rebaseline: clear the quarantine and resume dispatch (use after a legitimate change)" onClick={(e) => resumeHost(a, e)}>Resume</button>}
                        {!a.revoked && !a.ssh_only &&
                          <button className="btn ghost sm" title="Mint a single-use token bound to THIS host to re-enroll it after a reinstall that wiped its agent secret (a plain token can't re-bind an existing host)" onClick={(e) => reissueHost(a, e)}>Reissue</button>}
                        {!a.revoked && !a.ssh_only &&
                          <button className="btn outline-danger sm" title="Hard lock-out: revoke this agent's secret until re-enrolled" onClick={(e) => revokeHost(a, e)}>Revoke</button>}
                        {a.ssh_only &&
                          <button className="btn outline-danger sm"
                                  title="Remove this Controller's SSH record for the host. Nothing on the host is touched, and nothing else is removed — this is the one record that was making the host count as managed."
                                  onClick={(e) => forgetSshHost(a, e)}>Forget</button>}
                      </span>
                    </label>
                  ))}
                </div>
              );
            })}
          </div>
          <div className="he-footer">
            <div className="he-footer-actions">
              <button className="btn danger sm" onClick={disenrollChecked} disabled={!!busy || checked.length === 0}>Disenroll Host(s)</button>
              <button className="btn outline-danger sm" onClick={forceDeleteChecked} disabled={!!busy || checked.length === 0}
                      title="Force-remove a zombie/broken-build host from the console without waiting for its agent to tear down — purges its enroll token too">
                Force Delete
              </button>
              <button className="btn outline-danger sm" onClick={revokeChecked} disabled={!!busy || checked.length === 0}
                      title="Lock out the checked agents immediately (no heartbeat/poll/report) but KEEP their records — the one-click emergency stop for a runaway or compromised fleet">
                Revoke Checked
              </button>
              <span className="he-checked">{checked.length} checked</span>
            </div>
            <button className={"btn sm he-pause " + (enrollPaused ? "" : "outline-danger")} onClick={toggleEnrollPause}
                    disabled={!!busy}
                    title="Stop the controller accepting any new enrollments — the emergency brake for a runaway that re-appears faster than you can delete it. Existing agents keep running.">
              {enrollPaused ? "Resume Enrollment" : "Pause Enrollment"}
            </button>
          </div>
          {enrollPaused && (
            <div className="error-box" role="status" style={{ marginTop: 8 }}>
              New agent enrollment is <strong>PAUSED</strong> — no host can enroll until you resume.
              Clear the runaway rows now (Force Delete / Revoke), fix the source host, then Resume.
            </div>
          )}
          {busy && (
            <div className="ok-text" role="status" aria-live="polite"
                 style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
              <span className="spin" aria-hidden="true" />{busy}
            </div>
          )}
        </fieldset>

        <div className="he-actions">
          <fieldset className="tool-group-box"><legend>Environment</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button className="btn sm" onClick={newEnvironment}
                      title="Create a new, empty environment (no hosts need be selected)">New Environment</button>
              <select value={assignEnv} onChange={(e) => setAssignEnv(e.target.value)} style={{ maxWidth: 220 }}>
                <option value="">(unassigned)</option>
                {envs.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
              <button className="btn sm" onClick={assignEnvironment}>Set Environment</button>
              <button className="btn ghost sm" onClick={removeEnvironment}
                      disabled={!assignEnv}
                      title="Delete the selected environment (must have no hosts assigned)">Remove environment</button>
              <span className="faint">Set Environment applies to {checked.length} checked host(s).</span>
            </div>
          </fieldset>

          <fieldset className="tool-group-box"><legend>Sudo policy</legend>
            <div className="section-title" style={{ marginTop: 0 }}>For the checked host(s) — {checked.length} selected</div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button className="btn sm" disabled={checked.length === 0} onClick={() => setSudo(true)}>Requires Password</button>
              <button className="btn sm" disabled={checked.length === 0} onClick={() => setSudo(false)}>Passwordless (NOPASSWD)</button>
            </div>

            <div className="section-title">For an entire environment (new hosts inherit this)</div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <select value={sudoEnv} onChange={(e) => setSudoEnv(e.target.value)} style={{ maxWidth: 200 }}>
                <option value="">Choose environment…</option>
                {envs.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
              {sudoEnv && (
                <span className="faint">
                  currently {sudoDefaults[sudoEnv] ? "requires password" : "passwordless"}
                </span>
              )}
            </div>
            {sudoEnv && (
              <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
                <button className="btn sm" onClick={() => run(() => api.setEnvSudoDefault(sudoEnv, true),
                  `Set ${sudoEnv} default to password sudo.`)}>Requires Password</button>
                <button className="btn sm" onClick={() => run(() => api.setEnvSudoDefault(sudoEnv, false),
                  `Set ${sudoEnv} default to passwordless.`)}>Passwordless (NOPASSWD)</button>
              </div>
            )}

            <p className="faint" style={{ marginTop: 10 }}>
              For password-sudo hosts, store your own sudo password from the “Sudo Password” button in the header.
            </p>
          </fieldset>

          {/* Status for the actions in this column shows here, under the buttons,
              rather than at the top of the page — easier to notice where you clicked. */}
          {err && <div className="error-box">{err}</div>}
          {msg && <div className="ok-text" style={{ marginTop: 4 }}>{msg}</div>}
        </div>
      </div>
      )}

      {/* ============================ TAB: ENROLL A HOST ============================ */}
      {tab === "enroll" && (
      <div className="enroll-one">
      <div className="enroll-main">
        {/* ONE way in, not two. This was a "from here" form and a "from a
            hosts file" form side by side, which is the same job described
            twice: both end at the same api.enrollSsh, both need the same SSH
            account, and an operator with one host to add had to decide which
            half of the page was for them. Now there is one list -- a typed
            host is a one-line list -- and one set of credentials under it. */}
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}>
          <legend>Enroll hosts</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            One host per line, one host or forty. The Controller connects to each over SSH,
            installs its own key and enrolls the agent. The password is used once per
            host to install that key and is never stored — every later connection uses
            the key.
          </p>

          <div className="enroll-step"><span className="step-n">1</span> Which hosts</div>

          {/* ONE input. There was a Host name + Address pair with an Add
              button sitting on top of a box that takes exactly "address name"
              lines -- two ways to say the same thing, one of which only wrote
              into the other. Reported as what it was: "what is the point of
              the hostname field when you're putting a list with the IP and
              hostname?". The box below takes one line or forty, and the name
              each host is enrolled under is editable per row in the table once
              the list is read, which is a better place for it than a field
              that applies to whichever host you happen to be typing. */}
          <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 10,
                                        flexWrap: "wrap" }}>
            {/* The list an operator wants is nearly always the one the server
                already keeps, and the Controller can read it directly. Asking
                them to go and fetch it by hand was asking them to do this. */}
            <button className="btn sm ghost" type="button" disabled={busy === "server-hosts"}
                    onClick={loadServerHosts}>
              {busy === "server-hosts" ? "Reading…" : "Use the server's hosts file"}
            </button>
            <span className="faint" style={{ fontSize: 12 }}>or</span>
            <input type="file" accept=".txt,.hosts,text/plain,application/octet-stream"
                   onChange={async (e) => {
                     const f = e.target.files && e.target.files[0];
                     if (!f) return;
                     if (f.size > 1000000) { setErr("That file is larger than 1 MB."); return; }
                     const text = await f.text();
                     setBulkText(text);
                     // Let the same file be chosen again after an edit.
                     e.target.value = "";
                     await previewHostsFile(text);
                   }} />
          </div>

          <textarea value={bulkText} rows={6} spellCheck={false}
                    placeholder={"web-01\n\n# a name on its own, if this machine can already resolve it.\n# Otherwise an address and its names, as in /etc/hosts:\n192.168.155.135  ubuntu-lab\n10.0.0.5         db.internal db\n\n# An address on its own works too."}
                    style={{ width: "100%", marginTop: 8, fontFamily: "ui-monospace, monospace",
                             fontSize: 12.5 }}
                    onChange={(e) => { setBulkText(e.target.value); setBulkRows(null); }} />
          <div className="row" style={{ marginTop: 8, gap: 10, alignItems: "center" }}>
            <button className="btn sm" type="button"
                    disabled={!bulkText.trim() || busy === "bulk-read"}
                    onClick={() => previewHostsFile()}>
              {busy === "bulk-read" ? "Reading…" : "Read the list"}
            </button>
            {bulkRows && (
              <span className="faint" style={{ fontSize: 12 }}>
                {bulkRows.filter((r) => r.enrollable).length} of {bulkRows.length} line(s)
                can be enrolled.
              </span>
            )}
            {!bulkRows && bulkText.trim() && (
              <span className="faint" style={{ fontSize: 12 }}>
                Nothing is enrolled until you read the list and pick.
              </span>
            )}
          </div>

          {bulkRows && bulkRows.length > 0 && (
            <table className="table" style={{ marginTop: 12, fontSize: 13 }}>
              <thead><tr>
                <th style={{ width: 28 }}></th>
                <th>Connect to</th><th>Enroll as</th><th>From the list</th>
              </tr></thead>
              <tbody>
                {bulkRows.map((r) => (
                  <tr key={r.line} style={{ opacity: r.enrollable ? 1 : 0.55 }}>
                    <td>
                      {r.enrollable ? (
                        <input type="checkbox" checked={!!bulkPick[r.line]}
                               onChange={(e) => setBulkPick({ ...bulkPick, [r.line]: e.target.checked })} />
                      ) : null}
                    </td>
                    <td><code>{r.target || r.address || "—"}</code></td>
                    <td>
                      {r.enrollable ? (
                        <input value={bulkName[r.line] ?? r.name} autoComplete="off"
                               style={{ width: "100%", maxWidth: 220 }}
                               onChange={(e) => setBulkName({ ...bulkName, [r.line]: e.target.value })} />
                      ) : <span className="faint">—</span>}
                    </td>
                    <td className="faint" style={{ fontSize: 12 }}>
                      {/* Why a line is not on offer, in place of the line
                          itself. A filter that drops entries without saying
                          which, or why, is the one an operator stops trusting. */}
                      {r.skip_reason
                        ? <span>{r.skip_reason}</span>
                        : r.conflict
                          ? <span className="warn-text">{r.conflict}</span>
                          : <span>{(r.names || []).join(" ") || "—"}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="enroll-step"><span className="step-n">2</span> How to connect</div>
          <p className="faint" style={{ marginTop: 0, fontSize: 12 }}>
            One account for the batch, used once per host and never stored. It needs SSH
            reachable from the Controller and permission to write its own{" "}
            <code>~/.ssh/authorized_keys</code>.
          </p>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>SSH user</span>
              <input value={bulkCreds.username} autoComplete="off"
                     onChange={(e) => setBulkCreds({ ...bulkCreds, username: e.target.value })} /></label></div>
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Password</span>
              <input type="password" value={bulkCreds.password} autoComplete="new-password"
                     onChange={(e) => setBulkCreds({ ...bulkCreds, password: e.target.value })} /></label></div>
          </div>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Environment</span>
              <select value={bulkCreds.environment}
                      onChange={(e) => setBulkCreds({ ...bulkCreds, environment: e.target.value })}>
                <option value="">(unassigned)</option>
                {envs.map((x) => <option key={x} value={x}>{x}</option>)}
              </select></label></div>
          </div>
          <div className="row" style={{ marginTop: 10, gap: 10, alignItems: "center" }}>
            <button className="btn" type="button"
                    disabled={busy === "bulk-enroll" || !picked.length}
                    onClick={enrollPicked}>
              {busy === "bulk-enroll"
                ? "Enrolling…"
                : picked.length === 1
                  ? `Enroll ${(bulkName[picked[0].line] || picked[0].name) || "1 host"}`
                  : `Enroll ${picked.length} hosts`}
            </button>
            {bulkProgress && (
              <span className="faint" style={{ fontSize: 12 }}>
                {bulkProgress.done} of {bulkProgress.total} — {bulkProgress.current}…
              </span>
            )}
          </div>

          {/* THE RUN LOG. One line per host, written as the run goes. What
              this replaces is a count and a red box: "Enrolled 11 of 12" told
              an operator a number, and the twelfth host's reason arrived mixed
              in with everyone else's and vanished on the next click. The
              controller's reasons are already specific -- "SSH authentication
              failed", "Could not reach host: ...", a host-key mismatch -- they
              just had nowhere to be read. */}
          {runLog.length > 0 && (
            <div className="run-log" style={{ marginTop: 12 }}>
              <div className="row" style={{ alignItems: "center", gap: 10,
                                            marginBottom: 6 }}>
                <strong style={{ fontSize: "var(--fs-sm)" }}>Run log</strong>
                <span className="faint" style={{ fontSize: 12 }}>
                  {runLog.filter((e) => e.state === "ok").length} enrolled,{" "}
                  {runLog.filter((e) => e.state === "fail").length} failed
                </span>
                <button className="btn sm ghost" type="button" style={{ marginLeft: "auto" }}
                        title="Copy every line, to paste into a ticket or a message"
                        onClick={() => {
                          const text = runLog.map((e) =>
                            `${e.state}\t${e.name}\t${e.target}\t${e.detail}`).join("\n");
                          navigator.clipboard?.writeText(text);
                          setCopiedLog(true);
                          setTimeout(() => setCopiedLog(false), 1500);
                        }}>
                  {copiedLog ? "Copied" : "Copy"}
                </button>
                <button className="btn sm ghost" type="button"
                        onClick={() => setRunLog([])}>Clear</button>
              </div>
              <table className="compact" style={{ width: "100%" }}>
                <tbody>
                  {runLog.map((e, i) => (
                    <tr key={i}>
                      <td style={{ width: 18 }}>
                        <span className={"dot " + (e.state === "ok" ? "ok"
                          : e.state === "fail" ? "bad"
                          : e.state === "running" ? "warn" : "")}
                          title={e.state} />
                      </td>
                      <td style={{ fontFamily: "ui-monospace, monospace" }}>{e.name}</td>
                      <td className="faint" style={{ fontFamily: "ui-monospace, monospace" }}>
                        {e.target || "—"}</td>
                      <td className={e.state === "fail" ? "warn-text" : "faint"}
                          style={{ fontSize: 12 }}>
                        {e.state === "running" ? "connecting…"
                          : e.state === "waiting" ? "queued"
                          : e.detail || e.state}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
                Every attempt, including the ones that failed, is also written to
                the Controller&rsquo;s activity log, with the account that made it —
                this table goes when the page does, that one does not.
              </p>
            </div>
          )}
        </fieldset>

        {/* Folded away, not deleted. As a second panel beside the form it was
            mostly a restatement of the one above it -- the same names, the
            same addresses, a second box to paste a hosts file into -- competing
            for attention with the thing an operator came here to do. What it
            is actually FOR is the case where a name will not resolve, so it
            opens by itself when that happens and stays shut otherwise. */}
        <fieldset className="tool-group-box">
          <legend>Names this Controller can resolve</legend>
          <details open={namesFoldOpen}
                   onToggle={(e) => setNamesFoldOpen(e.target.open)}>
          {/* The count and nothing else: the legend above already says what
              they are, and a summary that repeats it is the redundancy this
              fold was made to remove. */}
          <summary style={{ cursor: "pointer" }}>
            {namesEntries === null
              ? "Loading\u2026"
              : `${namesEntries.length} name${namesEntries.length === 1 ? "" : "s"}`}
            {staleServerHosts ? " \u2014 and a problem reading the server's file" : ""}
          </summary>
          <p className="faint" style={{ fontSize: 12 }}>
            Leave a host&rsquo;s Address blank above and the name is looked up here,
            then in the server&rsquo;s <code>/etc/hosts</code>, then through the
            server&rsquo;s DNS. Nothing outside this Controller is changed.
          </p>

          <div className="row" style={{ gap: 6, alignItems: "center", marginBottom: 8,
                                        flexWrap: "wrap" }}>
            <input value={addName} placeholder="name" spellCheck={false}
                   style={{ flex: "1 1 110px", minWidth: 90 }}
                   onChange={(e) => setAddName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addEntry(); } }} />
            <input value={addAddr} placeholder="address" spellCheck={false}
                   style={{ flex: "1 1 120px", minWidth: 100 }}
                   onChange={(e) => setAddAddr(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addEntry(); } }} />
            <button className="btn sm" type="button"
                    disabled={!addName.trim() || !addAddr.trim() || busy === "add-name"}
                    onClick={addEntry}>
              {busy === "add-name" ? "Adding…" : "Add name"}
            </button>
          </div>

          {staleServerHosts && (
            <p className="warn-text" style={{ fontSize: 12, marginTop: 0 }}>
              The server&rsquo;s <code>/etc/hosts</code> is readable here, but{" "}
              {staleServerHosts}. Until then, names added to it on the server will
              not appear in this list — the ones added here will.
            </p>
          )}

          {namesEntries === null ? (
            <div className="faint" style={{ fontSize: 12 }}>Loading…</div>
          ) : namesEntries.length === 0 ? (
            <div className="empty" style={{ padding: 16, fontSize: 12 }}>
              No names yet. Add one above, or paste a hosts file below.
            </div>
          ) : (
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              <table className="compact">
                <thead><tr><th>Name</th><th>Address</th><th>From</th><th></th></tr></thead>
                <tbody>
                  {namesEntries.map((e) => (
                    <tr key={e.name}>
                      <td style={{ fontFamily: "ui-monospace, monospace" }}>{e.name}</td>
                      <td style={{ fontFamily: "ui-monospace, monospace" }}>{e.address}</td>
                      <td className="faint" style={{ fontSize: 11 }}
                          title={e.shadows.length
                            ? "also written in: " + e.shadows.join(", ")
                            : e.source_label}>
                        {SRC_SHORT[e.source] || e.source}
                        {e.shadows.length > 0 && " ⃰"}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {e.editable && (
                          <button className="linklike" type="button" title={"Remove " + e.name}
                                  disabled={busy === "rm-" + e.name}
                                  onClick={() => removeEntry(e.name)}>remove</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="row" style={{ marginTop: 10, gap: 8, alignItems: "center",
                                        flexWrap: "wrap" }}>
            <input value={checkName} placeholder="check a name…" spellCheck={false}
                   style={{ flex: "1 1 130px", minWidth: 110 }}
                   onChange={(e) => { setCheckName(e.target.value); setCheckResult(null); }}
                   onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); runCheck(); } }} />
            <button className="btn sm ghost" type="button"
                    disabled={!checkName.trim() || busy === "check"}
                    onClick={runCheck}>
              {busy === "check" ? "Checking…" : "Check"}
            </button>
          </div>
          {checkResult && (
            <div style={{ fontSize: 12, marginTop: 6 }}
                 className={checkResult.addresses.length ? "ok-text" : "warn-text"}>
              {checkResult.addresses.length
                ? `${checkResult.name} → ${checkResult.addresses.join(", ")} (from ${checkResult.via_label})`
                : `${checkResult.name} cannot be resolved here. ${checkResult.reason}`}
            </div>
          )}

          <div style={{ marginTop: 12, borderTop: "1px solid var(--border)",
                        paddingTop: 10 }}>
            <p className="faint" style={{ fontSize: 12, marginTop: 0 }}>
              Where these names come from, and a hosts file to paste over them:
            </p>
            {namesSources && namesSources.length > 0 && (
              <ul className="faint" style={{ fontSize: 11.5, marginTop: 8, paddingLeft: 18 }}>
                {namesSources.map((src) => (
                  <li key={src.key}>
                    {src.label} (<code>{src.path}</code>) —{" "}
                    {src.present
                      ? `${src.names} name${src.names === 1 ? "" : "s"}`
                      : (src.key === "server" && src.in_container
                          ? (<>not mounted. Add{" "}
                             <code>/etc:/host/etc:ro</code> to the controller
                             service&rsquo;s <code>volumes:</code> and recreate it,
                             and these names arrive by themselves.</>)
                          : "not present")}
                  </li>
                ))}
              </ul>
            )}
            <textarea value={namesText} rows={6} spellCheck={false}
                      placeholder={"# an /etc/hosts, pasted whole\n192.168.155.135  ubuntu-lab\n10.0.0.5         db.internal db"}
                      style={{ width: "100%", fontFamily: "ui-monospace, monospace",
                               fontSize: 12 }}
                      onChange={(e) => setNamesText(e.target.value)} />
            <div className="row" style={{ marginTop: 8, gap: 8, alignItems: "center" }}>
              <button className="btn sm" type="button"
                      disabled={busy === "names" || namesText === namesSaved}
                      onClick={saveNames}>
                {busy === "names" ? "Saving…" : "Replace list"}
              </button>
              {bulkText.trim() && bulkText !== namesText && (
                <button className="btn sm ghost" type="button"
                        onClick={() => setNamesText(bulkText)}>
                  Use the list above
                </button>
              )}
            </div>
            <p className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
              Replaces this Controller&rsquo;s own list entirely. Loopback,
              link-local and multicast addresses are dropped: they never name a
              machine to manage.
            </p>
          </div>
          </details>
        </fieldset>
        {/* Three always-open panels for three ways in, under the one an
            operator came here to use. They are not redundant -- a host with no
            inbound SSH needs the bundle, a terminal-only host needs the curl
            line, a whole inventory needs the Ansible one -- but they are
            REFERENCE, wanted once and then not again, and leaving them open
            made the page four screens long. */}
        <fieldset className="tool-group-box">
          <legend>Other ways to enroll</legend>
          <details open={otherWaysOpen}
                   onToggle={(e) => setOtherWaysOpen(e.target.open)}>
          <summary style={{ cursor: "pointer" }}>
            A bundle to copy, a curl one-liner, or Ansible across an inventory
          </summary>
        <fieldset className="tool-group-box"><legend>Download the agent bundle</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            The ready-to-run bundle, built on demand — each download bakes in a fresh, one-time enrollment token.
            Copy it to the target host, unzip, and run <code>./run_agent.sh</code>.
          </p>
          <button className="btn sm" disabled={busy === "bundle"}
            onClick={async () => {
              // POST (not a plain <a href> GET): building the bundle mints a fresh
              // one-time enrollment token, so it must NOT be triggerable by a
              // top-level navigation that SameSite=Lax would authorize. Fetch as a
              // blob and save it, mirroring Settings' "Regenerate agent bundle".
              setErr(""); setMsg(""); setBusy("bundle");
              try {
                const res = await fetch(api.agentBundleUrl(), { method: "POST", credentials: "include" });
                if (!res.ok) {
                  let d = ""; try { d = (await res.json()).detail || ""; } catch { /* not JSON */ }
                  throw new Error(d || `Bundle download failed (HTTP ${res.status}).`);
                }
                const blob = await res.blob();
                const cd = res.headers.get("Content-Disposition") || "";
                const m = /filename="?([^"]+)"?/.exec(cd);
                const name = (m && m[1]) || "sysible-agent-bundle.zip";
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url; a.download = name; document.body.appendChild(a); a.click();
                a.remove(); URL.revokeObjectURL(url);
              } catch (e) { setErr(e.message); }
              finally { setBusy(""); }
            }}>
            {busy === "bundle" ? "Building…" : "Download Agent Bundle"}
          </button>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Headless install (curl one-liner)</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            For terminal-only hosts: downloads, unzips, and runs the installer in one shot, authenticating with the
            portal login (curl -u). Replace <code>&lt;password&gt;</code> with the real portal password; <code>-k</code> skips
            the self-signed-cert check; the install step needs sudo.
          </p>
          {curlIp && curlHostName && (
            <label className="faint" style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
              <input type="checkbox" checked={curlUseIp} onChange={(e) => setCurlUseIp(e.target.checked)} />
              Address the controller by IP ({curlIp}) instead of hostname ({curlHostName}) — use this when the
              target host has no DNS for the hostname.
            </label>
          )}
          <div className="cmd-preview" style={{ whiteSpace: "pre-wrap" }}>{curlCmd}</div>
          <button className="btn sm ghost" style={{ marginTop: 8 }}
                  onClick={() => navigator.clipboard?.writeText(curlCmd).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
            {copied ? "Copied ✓" : "Copy to Clipboard"}
          </button>
          <p className="faint" style={{ marginTop: 10 }}>
            The curl download needs the Webserver Portal {portal.running
              ? <>running (it is) with a login set.</>
              : <>running — start it and set a login on the <button className="linklike" onClick={() => setTab("portal")}>Webserver Portal</button> tab.</>}
          </p>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Mass enrollment (Ansible ad-hoc)</legend>
          <p className="faint" style={{ marginTop: 0 }}>
            Enroll a whole inventory group at once — the headless install above, run across every host by Ansible.
            Replace <code>&lt;group&gt;</code> and <code>&lt;inventory&gt;</code> with your target group and inventory
            file, and <code>&lt;password&gt;</code> with the real portal password. <code>--become</code> handles privilege
            escalation (so no <code>sudo</code> in the command); each host pulls its own fresh one-time enrollment token.
          </p>
          <div className="cmd-preview" style={{ whiteSpace: "pre-wrap" }}>{ansibleCmd}</div>
          <button className="btn sm ghost" style={{ marginTop: 8 }}
                  onClick={() => navigator.clipboard?.writeText(ansibleCmd).then(() => { setCopiedAnsible(true); setTimeout(() => setCopiedAnsible(false), 1500); })}>
            {copiedAnsible ? "Copied ✓" : "Copy to Clipboard"}
          </button>
          <p className="faint" style={{ marginTop: 10 }}>
            Needs Ansible on the control node with SSH reach to the group, plus the Webserver Portal {portal.running
              ? <>running (it is) with a login set.</>
              : <>running with a login set — configure it on the <button className="linklike" onClick={() => setTab("portal")}>Webserver Portal</button> tab.</>}
            {" "}Add <code>-u</code>/<code>--ask-become-pass</code> or <code>-e</code> vars as your SSH access requires.
          </p>
        </fieldset>
          </details>
        </fieldset>
      </div>

      </div>
      )}

      {/* ============================ TAB: RELAY / BASTION ============================ */}
      {/* Administered next to enrollment rather than under Settings: the relay is how
          a host is REACHED, so it belongs with the rest of "getting hosts managed". */}
      {tab === "relay" && <RelayConfig />}

      {/* ============================ TAB: WEBSERVER PORTAL ============================ */}
      {tab === "portal" && (
      <div>
        <div className="he-section-head" style={{ marginTop: 0, paddingTop: 0, borderTop: "none" }}>
          <h3>Webserver Portal</h3>
          <button className="btn ghost sm" onClick={() => { loadPortal(); loadHistory(); loadSessions(); loadUploads(); loadDownloads(); }}>Refresh</button>
        </div>
        <p className="faint" style={{ marginTop: 0, maxWidth: 880 }}>
          Host operators sign in with this login to download the agent bundle or exchange files. Run the portal only
          while provisioning, on a trusted network.
        </p>

        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Status &amp; port</legend>
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <span className={"badge" + (portal.running ? " green" : "")}>{portal.running ? "Running" : "Stopped"}</span>
              <button className="btn sm" disabled={portalBusy || portal.running}
                      onClick={() => portalAct("start", async () => {
                        const r = await api.portalStart();
                        if (r && r.running === false) throw new Error(r.error || "The portal failed to start.");
                        return r;
                      }, "Portal started.")}>
                {portalBusy === "start" ? <span className="spin" /> : "Start Portal"}</button>
              <button className="btn sm ghost" disabled={portalBusy || !portal.running}
                      onClick={() => portalAct("stop", () => api.portalStop(), "Portal stopped.")}>Stop Portal</button>
            </div>
            {/* "Running" only means the portal answered on THIS machine's
                loopback. In a container that is the container's loopback, so the
                console used to print an address the network could not reach —
                nmap said `closed` while the badge said Running, and nothing here
                explained the gap. When the controller can tell that the address
                will not answer, it says so instead of printing it. */}
            {portal.running && (portal.unreachable_reason
              ? <div className="portal-unreachable">
                  Running, but not reachable at {reachable} — {portal.unreachable_reason}
                </div>
              : <div className="faint" style={{ marginTop: 6 }}>Reachable at: {reachable}</div>)}
            <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center", marginTop: 10 }}>
              <span className="faint">Port</span>
              <input type="number" style={{ maxWidth: 120 }} value={portPort} onChange={(e) => setPortPort(e.target.value)} />
              <button className="btn sm" disabled={portalBusy || !portPort}
                      onClick={() => portalAct("port", () => api.portalSetPort(Number(portPort)),
                        "Port saved — restart the portal if it's running.")}>Save Port</button>
            </div>
          </fieldset>

        <div className="he-portal-grid">
        <fieldset className="tool-group-box" style={{ marginTop: 0 }}><legend>Portal login</legend>
          <div className="muted" style={{ fontSize: 13, marginBottom: 10 }}>
            {portal.credentials_configured
              ? <>Current user: <strong>{portal.username}</strong> · last login {fmtTime(portal.last_login?.timestamp ?? portal.last_login)}</>
              : <span className="faint">No portal login set yet.</span>}
          </div>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>New username</span>
              <input value={nu} onChange={(e) => setNu(e.target.value)} /></label></div>
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Current password (if set)</span>
              <input type="password" value={cur} onChange={(e) => setCur(e.target.value)} /></label></div>
          </div>
          <div className="group-fields">
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>New password</span>
              <input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label></div>
            <div className="group-field"><label className="field" style={{ marginTop: 0 }}><span>Confirm new password</span>
              <input type="password" value={np2} onChange={(e) => setNp2(e.target.value)} /></label></div>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn sm" disabled={portalBusy === "creds" || !nu || !np} onClick={saveCreds}>
              {portalBusy === "creds" ? <span className="spin" /> : "Save Login"}</button>
            <button className="btn sm danger" disabled={portalBusy === "remove" || !portal.credentials_configured} onClick={removeCreds}>Remove Login</button>
          </div>
        </fieldset>

        <fieldset className="tool-group-box"><legend>Active Sessions</legend>
          <p className="faint" style={{ marginTop: 0 }}>Operators currently signed in. Revoking one logs that browser out immediately.</p>
          {sessions.length === 0 ? <div className="empty" style={{ padding: 20 }}>No active sessions.</div> : (
            <table>
              <thead><tr><th>Logged In</th><th>IP</th><th></th></tr></thead>
              <tbody>
                {sessions.map((s, i) => (
                  <tr key={s.id ?? s.session_id ?? i}>
                    <td className="faint mono">{fmtTime(s.created ?? s.created_at ?? s.logged_in ?? s.started_at)}</td>
                    <td className="faint">{s.ip || s.ip_address || ""}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn ghost sm danger" onClick={() => revoke(s)}>Revoke</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </fieldset>
      </div>

      <fieldset className="tool-group-box"><legend>Login History</legend>
        {history.length === 0 ? <div className="empty" style={{ padding: 16 }}>No login history.</div> : (
          <div style={{ maxHeight: 200, overflowY: "auto" }}>
            <table>
              <thead><tr><th>Time</th><th>Event</th><th>Username</th><th>IP</th></tr></thead>
              <tbody>
                {history.map((h, i) => (
                  <tr key={h.id ?? i}>
                    <td className="faint mono">{fmtTime(h.timestamp ?? h.time ?? h.created_at)}</td>
                    <td>{h.event}</td><td>{h.username}</td>
                    <td className="faint">{h.ip || h.ip_address || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </fieldset>

      <div className="he-portal-grid">
        <fieldset className="tool-group-box"><legend>Files Uploaded By Hosts</legend>
          {uploads.length === 0 ? <div className="empty" style={{ padding: 16 }}>No uploaded files.</div> : (
            <table>
              <tbody>
                {uploads.map((f, i) => { const n = fname(f); return (
                  <tr key={n || i}>
                    <td>{n}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <a className="btn ghost sm" href={api.portalUploadUrl(n)}>Save</a>{" "}
                      <button className="btn ghost sm danger" onClick={async () => { if (!(await confirmDialog(`Delete the uploaded file "${n}"?`))) return; await api.portalUploadDelete(n); loadUploads(); }}>Delete</button>
                    </td>
                  </tr>
                ); })}
              </tbody>
            </table>
          )}
        </fieldset>

        <fieldset className="tool-group-box"><legend>Files Staged For Download</legend>
          <div style={{ marginBottom: 8 }}>
            <label className="btn ghost sm" style={{ cursor: "pointer" }}>
              Add File…<input type="file" style={{ display: "none" }} onChange={stageDownload} />
            </label>
          </div>
          {downloads.length === 0 ? <div className="empty" style={{ padding: 16 }}>No staged files.</div> : (
            <table>
              <tbody>
                {downloads.map((f, i) => { const n = fname(f); return (
                  <tr key={n || i}>
                    <td>{n}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn ghost sm danger" onClick={async () => { if (!(await confirmDialog(`Delete the staged file "${n}"?`))) return; await api.portalDownloadDelete(n); loadDownloads(); }}>Delete</button>
                    </td>
                  </tr>
                ); })}
              </tbody>
            </table>
          )}
        </fieldset>
        </div>
      </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Sysible Relay — the bastion / jump-box transport.
//
// Reach hosts the controller can't dial directly: it opens a single-hop SSH
// ProxyJump to the bastion and the on-bastion relay daemon forwards to permitted
// internal targets. Blank relay host = off; there is no separate enable switch to
// get out of sync with it.
//
// The controller REFUSES a configuration that can't work rather than storing it,
// so a rejected save shows the reason here instead of silently reaching hosts
// around the bastion.
// ---------------------------------------------------------------------------
function RelayConfig() {
  const [cfg, setCfg] = useState(null);        // null = loading
  const [draft, setDraft] = useState({});
  const [pubkey, setPubkey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api.getRelay()
      .then((d) => { setCfg(d.relay || {}); setPubkey(d.public_key || ""); setDraft({}); })
      .catch((e) => setErr(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  const val = (k) => (k in draft ? draft[k] : (cfg?.[k] ?? ""));
  const set = (k, v) => setDraft((m) => ({ ...m, [k]: v }));
  const dirty = Object.keys(draft).length;

  async function save() {
    setErr(""); setMsg(""); setBusy(true);
    try {
      const d = await api.setRelay(draft);
      setCfg(d.relay || {}); setPubkey(d.public_key || ""); setDraft({});
      setMsg((d.relay?.relay_host)
        ? `Saved — hosts route through ${d.relay.relay_host}.`
        : "Saved — the relay is off; every host is reached directly.");
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  if (cfg === null) return <div className="empty"><span className="spin" /></div>;

  const scriptUrl = (os) => `/api/relay/bastion-script?os=${os}`;

  return (
    <div style={{ maxWidth: 760 }}>
      <div className="he-section-head" style={{ marginTop: 0, paddingTop: 0, borderTop: "none" }}>
        <h3>Relay / Bastion</h3>
        <span className={"badge" + (cfg.relay_host ? " ok" : "")}>
          {cfg.relay_host ? "Routing through " + cfg.relay_host : "Off"}
        </span>
      </div>
      <p className="faint" style={{ marginTop: 0 }}>
        The jump box this controller reaches hosts <strong>THROUGH</strong>. Leave the
        relay host blank to route nothing. A host goes through the relay when it opts
        in, or when it matches the auto-route allowlist below.
      </p>

      <fieldset className="tool-group-box"><legend>Relay endpoint</legend>
        <label className="field"><span>Relay / bastion host</span>
          <input value={val("relay_host")} placeholder="bastion.example.com"
                 onChange={(e) => set("relay_host", e.target.value)} /></label>
        <p className="faint" style={{ fontSize: 12, marginTop: -4 }}>
          The BASTION — the machine where you ran install-*-bastion. Not this
          controller's own address.
        </p>
        <label className="field"><span>Relay SSH user</span>
          <input value={val("relay_user")} placeholder="sysible-relay"
                 onChange={(e) => set("relay_user", e.target.value)} /></label>
        <label className="field"><span>Relay SSH port</span>
          <input type="number" value={val("relay_port")} placeholder="22"
                 onChange={(e) => set("relay_port", e.target.value)} /></label>
        <label className="field"><span>Relay identity key path (private)</span>
          <input value={val("relay_identity")} placeholder="/data/relay_keys/relay_ed25519"
                 onChange={(e) => set("relay_identity", e.target.value)} /></label>
        <p className="faint" style={{ fontSize: 12, marginTop: -4 }}>
          The PRIVATE key, not the .pub. It's created for you on first save.
        </p>
      </fieldset>

      <fieldset className="tool-group-box"><legend>Which hosts route through it</legend>
        <label className="field"><span>Auto-route allowlist (CIDRs / domain suffixes)</span>
          <input value={val("route_allowlist")} placeholder="10.20.0.0/16, .internal.example.com"
                 onChange={(e) => set("route_allowlist", e.target.value)} /></label>
        <p className="faint" style={{ fontSize: 12, marginTop: -4 }}>
          The network(s) <strong>behind</strong> the bastion — normally a different
          subnet than this controller's own. Don't list the controller's own subnet:
          those hosts are already reachable directly. Leave blank to require explicit
          per-host opt-in.
        </p>
        <label className="field"><span>Relay OS</span>
          <select value={val("relay_os") || "auto"} onChange={(e) => set("relay_os", e.target.value)}>
            <option value="auto">auto</option>
            <option value="linux">linux</option>
            <option value="windows">windows</option>
          </select></label>
      </fieldset>

      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <button className="btn" disabled={busy || dirty === 0} onClick={save}>
          {busy ? <span className="spin" /> : "Save"}</button>
        {dirty > 0 && <span className="faint" style={{ fontSize: 12 }}>
          {dirty} unsaved change{dirty === 1 ? "" : "s"}</span>}
      </div>
      {msg && <div className="ok-text" style={{ marginTop: 10 }}>{msg}</div>}
      {err && <div className="error-box" style={{ marginTop: 10 }} role="alert">{err}</div>}

      <fieldset className="tool-group-box" style={{ marginTop: 18 }}>
        <legend>Set up the bastion</legend>
        <p className="faint" style={{ marginTop: 0 }}>
          Run the setup script <strong>on the jump box</strong> — it creates the
          least-privilege relay account and installs the forwarding daemon. Then
          authorize the key below so this controller can connect.
        </p>
        <div className="row" style={{ gap: 8 }}>
          <a className="btn ghost sm" href={scriptUrl("linux")}>Linux setup script</a>
          <a className="btn ghost sm" href={scriptUrl("windows")}>Windows setup script</a>
        </div>
        <div style={{ marginTop: 12 }}>
          <span className="faint" style={{ fontSize: 12 }}>
            This controller's relay public key — add it to the relay account's
            authorized_keys on the bastion:
          </span>
          <pre className="mono" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all",
                                         marginTop: 6 }}>
            {pubkey || "(no key yet — save a relay host and it is created for you)"}
          </pre>
        </div>
      </fieldset>
    </div>
  );
}
