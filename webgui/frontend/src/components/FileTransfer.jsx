import React, { useEffect, useState } from "react";
import { api, noteRawStatus } from "../api.js";

// Upload a file to / download a file from one host. Works for both agent-managed
// hosts (transfer rides through the agent) and pure-SSH hosts (SFTP) — the BFF
// picks the transport per host. Shared by the Connect page's Selected Host panel
// and by each pop-out terminal window, so a terminal is a first-class shell with
// its own file transfer. Errors show inline in this panel (and, if provided, are
// also bubbled up via onErr for the page-level error banner).
export default function FileTransfer({ host, onErr }) {
  const [remotePath, setRemotePath] = useState("");
  const [file, setFile] = useState(null);
  const [dnPath, setDnPath] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [browse, setBrowse] = useState(null); // "upload" | "download" target for the picker

  const fail = (m) => { setErr(m); setMsg(""); if (onErr) onErr(m); };
  const ok = (m) => { setMsg(m); setErr(""); if (onErr) onErr(""); };

  async function upload(e) {
    e.preventDefault();
    const form = e.target;
    setBusy("up"); setMsg(""); setErr("");
    try {
      const r = await api.uploadFile(host.id, remotePath.trim(), file);
      ok(`Uploaded ${r.filename} (${r.bytes} bytes) → ${r.remote_path}`);
      setFile(null); form.reset();
    } catch (e2) { fail(e2.message); }
    finally { setBusy(""); }
  }

  // Fetch → blob → save, rather than navigating the window to the download URL.
  // In a pop-out terminal a plain navigation would replace the shell, and an
  // error response (agent offline, file too large) would paint a raw JSON page
  // over it; fetching keeps the shell intact and surfaces errors inline.
  async function download(e) {
    e.preventDefault();
    const path = dnPath.trim();
    if (!path) return;
    setBusy("dn"); setMsg(""); setErr("");
    try {
      const resp = await fetch(api.downloadUrl(host.id, path), { credentials: "include" });
      noteRawStatus(resp.status);  // this fetch bypasses req(); bounce to login on 401
      if (!resp.ok) {
        let detail = `Download failed (${resp.status})`;
        try { const j = await resp.json(); if (j && j.detail) detail = j.detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get("Content-Disposition") || "";
      const m = /filename="?([^"]+)"?/.exec(cd);
      const name = (m && m[1]) || path.split("/").filter(Boolean).pop() || "download";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      ok(`Downloaded ${name} (${blob.size} bytes)`);
    } catch (e2) { fail(e2.message); }
    finally { setBusy(""); }
  }

  const secLabel = { fontSize: 12, fontWeight: 600, color: "var(--text-dim)", margin: "0 0 6px" };

  return (
    <div>
      {/* Upload: pick a local file, then say where it goes on the host. */}
      <form onSubmit={upload}>
        <div style={secLabel}>Upload a file to this host</div>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <label className="btn sm ghost" style={{ flexShrink: 0, cursor: "pointer" }}>
            Choose File…
            <input type="file" style={{ display: "none" }}
                   onChange={(e) => setFile(e.target.files[0] || null)} />
          </label>
          <span className="faint" style={{ minWidth: 0, overflow: "hidden",
                       textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {file ? file.name : "No file chosen"}
          </span>
        </div>
        <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
          <input style={{ flex: 1, minWidth: 160 }} placeholder="Remote destination path (e.g. /tmp or /home/me/app.conf)"
                 value={remotePath} onChange={(e) => setRemotePath(e.target.value)} />
          <button type="button" className="btn sm ghost" onClick={() => setBrowse("upload")}>Browse…</button>
          <button className="btn sm" disabled={busy === "up" || !file || !remotePath.trim()}>
            {busy === "up" ? <span className="spin" /> : "Upload"}
          </button>
        </div>
      </form>

      {/* Download: a remote file path off the host. */}
      <form onSubmit={download} style={{ marginTop: 14 }}>
        <div style={secLabel}>Download a file from this host</div>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <input style={{ flex: 1, minWidth: 200 }} placeholder="Remote file path to fetch (e.g. /var/log/syslog)"
                 value={dnPath} onChange={(e) => setDnPath(e.target.value)} />
          <button type="button" className="btn sm ghost" onClick={() => setBrowse("download")}>Browse…</button>
          <button className="btn sm" disabled={busy === "dn" || !dnPath.trim()}>
            {busy === "dn" ? <span className="spin" /> : "Download"}
          </button>
        </div>
      </form>

      <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
        Agent-managed hosts transfer as your own account through the agent (limit 140&nbsp;KB per file). Pure-SSH hosts use the shared controller key and are superuser-only.
      </div>
      {msg && <div className="ok-text" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error-box" role="alert" style={{ marginTop: 8 }}>{err}</div>}
      {browse && (
        <RemoteBrowse host={host} mode={browse} onErr={fail}
          onClose={() => setBrowse(null)}
          onPick={(path) => { (browse === "upload" ? setRemotePath : setDnPath)(path); setBrowse(null); }} />
      )}
    </div>
  );
}

// Lightweight remote file/folder picker: lists a directory on the host (via a
// one-off `ls` exec) and lets you click into folders or pick an entry, so you
// don't have to remember and type the full remote path. In "upload" mode a
// folder is the selection (destination dir); in "download" mode a file is.
function RemoteBrowse({ host, mode, onPick, onClose, onErr }) {
  // Start at the filesystem root so the whole disk is reachable. `ls -1Ap` omits
  // `.`/`..`, so we synthesize a `../` entry (below) to let the operator climb up
  // from wherever they land — without it the picker can only ever descend.
  const [dir, setDir] = useState("/");
  const [entries, setEntries] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [pathInput, setPathInput] = useState("/");

  async function list(d) {
    setBusy(true); setErr("");
    const cmd = `cd '${d.replace(/'/g, "'\\''")}' 2>/dev/null && pwd && ls -1Ap`;
    try {
      const r = await api.fleet("script", [host.id], cmd);
      const res = (r.results || [])[0] || {};
      const out = (res.stdout || "");
      const lines = out.split("\n").filter((l) => l !== "");
      if (lines.length === 0) { setErr(res.stderr || res.error || "Could not read that directory."); return; }
      const cwd = lines.shift();
      setDir(cwd);
      setPathInput(cwd);
      // Prepend a parent-directory entry so navigation up the tree is possible
      // (ls -1Ap never emits `..`); the root has no parent, so skip it there.
      setEntries(cwd === "/" ? lines : ["../", ...lines]);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }
  useEffect(() => { list("/"); /* eslint-disable-next-line */ }, []);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const join = (name) => (dir.endsWith("/") ? dir + name : dir + "/" + name);

  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true"
           aria-label={mode === "upload" ? "Choose destination folder" : "Choose file to download"}
           style={{ maxWidth: 560, textAlign: "left" }}>
        <h3 style={{ textAlign: "left", marginBottom: 4 }}>{mode === "upload" ? "Choose destination folder" : "Choose file to download"}</h3>
        <div className="faint mono" style={{ fontSize: 12, marginBottom: 8, wordBreak: "break-all" }}>{host.label}:{dir}</div>
        <form className="row" style={{ gap: 6, marginBottom: 8 }}
              onSubmit={(e) => { e.preventDefault(); if (pathInput.trim()) list(pathInput.trim()); }}>
          <input className="mono" style={{ flex: 1, minWidth: 160 }} placeholder="Jump to path (e.g. /var/log or /)"
                 value={pathInput} onChange={(e) => setPathInput(e.target.value)} />
          <button type="submit" className="btn ghost sm" disabled={busy}>Go</button>
        </form>
        {err && <div className="error-box">{err}</div>}
        <div style={{ maxHeight: "46vh", overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
          {busy ? <div className="empty" style={{ padding: 20 }}><span className="spin" /></div>
            : entries.map((name) => {
                const isDir = name.endsWith("/");
                const clean = isDir ? name.slice(0, -1) : name;
                return (
                  <div key={name} className="host-row" style={{ cursor: "pointer", justifyContent: "space-between" }}
                       onClick={() => { if (isDir) list(name === "../" ? join("..") : join(clean)); else if (mode === "download") onPick(join(clean)); }}
                       onDoubleClick={() => { if (isDir && mode === "upload") onPick(join(clean)); }}>
                    <span>{isDir ? "📁 " : "📄 "}{clean}</span>
                    {isDir && mode === "upload" &&
                      <button type="button" className="btn ghost sm" onClick={(e) => { e.stopPropagation(); onPick(join(clean)); }}>Use this folder</button>}
                  </div>
                );
              })}
        </div>
        <div className="row" style={{ justifyContent: "space-between", marginTop: 12 }}>
          <span className="faint">{mode === "upload" ? "Click a folder to open it; use the button to pick it." : "Click a folder to open it; click a file to pick it."}</span>
          <div className="row" style={{ gap: 8 }}>
            {mode === "upload" && <button className="btn sm" onClick={() => onPick(dir)}>Use “{dir}”</button>}
            <button className="btn ghost sm" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}
