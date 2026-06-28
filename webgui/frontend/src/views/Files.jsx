import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Files() {
  const [hosts, setHosts] = useState([]);
  const [err, setErr] = useState("");

  // upload state
  const [upHost, setUpHost] = useState("");
  const [remotePath, setRemotePath] = useState("");
  const [file, setFile] = useState(null);
  const [upBusy, setUpBusy] = useState(false);
  const [upMsg, setUpMsg] = useState("");
  const [upErr, setUpErr] = useState("");

  // download state
  const [dnHost, setDnHost] = useState("");
  const [dnPath, setDnPath] = useState("");

  useEffect(() => {
    api.hosts()
      .then((d) => {
        const hs = d.hosts || [];
        setHosts(hs);
        if (hs[0]) { setUpHost(hs[0].id); setDnHost(hs[0].id); }
      })
      .catch((x) => setErr(x.message));
  }, []);

  async function doUpload(e) {
    e.preventDefault();
    setUpBusy(true); setUpMsg(""); setUpErr("");
    try {
      const r = await api.uploadFile(upHost, remotePath.trim(), file);
      setUpMsg(`Uploaded ${r.filename} (${r.bytes} bytes) → ${r.remote_path}`);
      setFile(null);
      e.target.reset();
    } catch (e2) {
      setUpErr(e2.message);
    } finally {
      setUpBusy(false);
    }
  }

  function doDownload(e) {
    e.preventDefault();
    // Navigate to the streaming endpoint; browser handles the file save.
    window.location.href = api.downloadUrl(dnHost, dnPath.trim());
  }

  if (err) return <div className="error-box">{err}</div>;

  const hostOptions = hosts.map((h) => (
    <option key={h.id} value={h.id}>{h.label}{!h.has_agent ? " (ssh)" : ""}</option>
  ));

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))", gap: 20, maxWidth: 900 }}>
      <form className="card" onSubmit={doUpload}>
        <h3 style={{ marginTop: 0 }}>↑ Upload to host</h3>
        <p className="muted" style={{ marginTop: 0 }}>Push a local file to a host over SSH.</p>
        <label className="field">
          <span>Host</span>
          <select value={upHost} onChange={(e) => setUpHost(e.target.value)}>{hostOptions}</select>
        </label>
        <label className="field">
          <span>Remote path *</span>
          <input
            value={remotePath}
            onChange={(e) => setRemotePath(e.target.value)}
            placeholder="/tmp/ or /etc/app/config.yml"
          />
        </label>
        <label className="field">
          <span>File *</span>
          <input type="file" onChange={(e) => setFile(e.target.files[0] || null)} />
        </label>
        <button className="btn" style={{ marginTop: 16 }} disabled={upBusy || !file || !remotePath.trim()}>
          {upBusy ? <span className="spin" /> : "Upload"}
        </button>
        {upMsg && <div style={{ marginTop: 12, color: "var(--green)" }}>{upMsg}</div>}
        {upErr && <div className="error-box">{upErr}</div>}
      </form>

      <form className="card" onSubmit={doDownload}>
        <h3 style={{ marginTop: 0 }}>↓ Download from host</h3>
        <p className="muted" style={{ marginTop: 0 }}>Fetch a remote file over SSH to your machine.</p>
        <label className="field">
          <span>Host</span>
          <select value={dnHost} onChange={(e) => setDnHost(e.target.value)}>{hostOptions}</select>
        </label>
        <label className="field">
          <span>Remote file path *</span>
          <input
            value={dnPath}
            onChange={(e) => setDnPath(e.target.value)}
            placeholder="/var/log/syslog"
          />
        </label>
        <button className="btn" style={{ marginTop: 16 }} disabled={!dnPath.trim()}>
          Download
        </button>
      </form>
    </div>
  );
}
