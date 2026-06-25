import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Dashboard({ onNavigate }) {
  const [hosts, setHosts] = useState(null);
  const [envs, setEnvs] = useState([]);
  const [edition, setEdition] = useState({});
  const [err, setErr] = useState("");

  useEffect(() => {
    Promise.all([api.hosts(), api.environments(), api.edition()])
      .then(([h, e, ed]) => {
        setHosts(h.hosts || []);
        setEnvs(e.environments || []);
        setEdition(ed || {});
      })
      .catch((x) => setErr(x.message));
  }, []);

  if (err) return <div className="error-box">{err}</div>;
  if (hosts === null) return <div className="empty"><span className="spin" /></div>;

  const agentCount = hosts.filter((h) => h.has_agent).length;
  const sshOnly = hosts.length - agentCount;
  const editionName = edition.edition || edition.name || "Community";

  return (
    <div>
      <div className="cards">
        <div className="card click" onClick={() => onNavigate("hosts")}>
          <div className="k">Managed hosts</div>
          <div className="v">{hosts.length}</div>
        </div>
        <div className="card">
          <div className="k">Agent-enrolled</div>
          <div className="v">{agentCount}</div>
        </div>
        <div className="card">
          <div className="k">SSH-only</div>
          <div className="v">{sshOnly}</div>
        </div>
        <div className="card">
          <div className="k">Environments</div>
          <div className="v">{envs.length}</div>
        </div>
      </div>

      <div className="section-title">Quick actions</div>
      <div className="cards">
        <div className="card click" onClick={() => onNavigate("tools")}>
          <div style={{ fontWeight: 600, fontSize: 15 }}>⚙ Run a tool</div>
          <div className="muted" style={{ marginTop: 6 }}>
            Dispatch any of the built-in administration actions across selected hosts.
          </div>
        </div>
        <div className="card click" onClick={() => onNavigate("terminal")}>
          <div style={{ fontWeight: 600, fontSize: 15 }}>▷ Open a terminal</div>
          <div className="muted" style={{ marginTop: 6 }}>
            Live SSH shell to any host, right in the browser.
          </div>
        </div>
        <div className="card click" onClick={() => onNavigate("files")}>
          <div style={{ fontWeight: 600, fontSize: 15 }}>↑↓ Transfer files</div>
          <div className="muted" style={{ marginTop: 6 }}>
            Upload to or download from a host over SSH.
          </div>
        </div>
      </div>

      <div className="section-title">Controller</div>
      <div className="card" style={{ maxWidth: 420 }}>
        <div className="spread">
          <span className="muted">Edition</span>
          <span className="badge blue">{editionName}</span>
        </div>
      </div>
    </div>
  );
}
