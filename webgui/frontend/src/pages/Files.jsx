import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// File transfer for Sysible Connect: upload a local file to a host path,
// or download a file from a host. SSH-based, so the host picker lists
// SSH and agent+SSH (merged) hosts.
export default function FilesPage({ onBack }) {
  const [hosts, setHosts] = useState([]);
  const [host, setHost] = useState("");

  const [remotePath, setRemotePath] = useState("/tmp");
  const [file, setFile] = useState(null);
  const fileInput = useRef(null);
  const [upBusy, setUpBusy] = useState(false);
  const [upMsg, setUpMsg] = useState(null);

  const [dlPath, setDlPath] = useState("");
  const [dlMsg, setDlMsg] = useState(null);

  useEffect(() => {
    api
      .hosts()
      .then((d) => {
        const ssh = (d.hosts || []).filter((h) => h.kind === "ssh" || h.kind === "merged");
        setHosts(ssh);
        if (ssh.length) setHost(ssh[0].id);
      })
      .catch(() => setHosts([]));
  }, []);

  const upload = async (e) => {
    e.preventDefault();
    if (!host || !file) return;
    setUpBusy(true);
    setUpMsg(null);
    try {
      const r = await api.uploadFile(host, remotePath, file);
      setUpMsg({ ok: true, text: `Uploaded ${r.filename} (${r.bytes} bytes) to ${r.remote_path}` });
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
    } catch (err) {
      setUpMsg({ ok: false, text: err.message });
    } finally {
      setUpBusy(false);
    }
  };

  const download = (e) => {
    e.preventDefault();
    if (!host || !dlPath) return;
    setDlMsg(null);
    // Let the browser perform the GET so it streams to disk; errors come
    // back as a JSON body, so we can't easily show them inline here -
    // a failed transfer simply won't download.
    const a = document.createElement("a");
    a.href = api.downloadUrl(host, dlPath);
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setDlMsg({ ok: true, text: `Requested ${dlPath}` });
  };

  return (
    <div className="toolpage">
      <div className="toolpage-bar">
        <button className="btn-ghost" onClick={onBack}>← Tools</button>
        <h2 className="page-title inline">File Transfer</h2>
      </div>

      <label className="field" style={{ maxWidth: 420 }}>
        <span>Host (SSH)</span>
        <select value={host} onChange={(e) => setHost(e.target.value)}>
          {hosts.length === 0 && <option value="">No SSH hosts enrolled</option>}
          {hosts.map((h) => (
            <option key={h.id} value={h.id}>{h.label} — {h.type_text}</option>
          ))}
        </select>
      </label>

      <div className="files-grid">
        <form className="card" onSubmit={upload}>
          <h3 className="files-h">Upload to host</h3>
          <label className="field">
            <span>Local file</span>
            <input ref={fileInput} type="file" onChange={(e) => setFile(e.target.files[0] || null)} />
          </label>
          <label className="field">
            <span>Destination path on host</span>
            <input value={remotePath} onChange={(e) => setRemotePath(e.target.value)}
                   placeholder="/tmp or /tmp/name.ext" />
          </label>
          <button className="btn" disabled={upBusy || !file || !host}>
            {upBusy ? "Uploading…" : "Upload"}
          </button>
          {upMsg && (
            <div className={`alert ${upMsg.ok ? "" : "error"}`}
                 style={upMsg.ok ? { color: "#8fd6a6" } : undefined}>
              {upMsg.text}
            </div>
          )}
        </form>

        <form className="card" onSubmit={download}>
          <h3 className="files-h">Download from host</h3>
          <label className="field">
            <span>File path on host</span>
            <input value={dlPath} onChange={(e) => setDlPath(e.target.value)}
                   placeholder="/var/log/messages" />
          </label>
          <button className="btn" disabled={!dlPath || !host}>Download</button>
          {dlMsg && (
            <div className="alert" style={{ color: "#8fd6a6" }}>{dlMsg.text}</div>
          )}
          <p className="muted small">
            The file streams straight to your browser's downloads.
          </p>
        </form>
      </div>
    </div>
  );
}
