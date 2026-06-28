import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Hosts() {
  const [hosts, setHosts] = useState(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    api.hosts().then((d) => setHosts(d.hosts || [])).catch((x) => setErr(x.message));
  }, []);

  if (err) return <div className="error-box">{err}</div>;
  if (hosts === null) return <div className="empty"><span className="spin" /></div>;

  const filtered = hosts.filter((h) => {
    const s = q.trim().toLowerCase();
    if (!s) return true;
    return (
      (h.label || "").toLowerCase().includes(s) ||
      (h.address || "").toLowerCase().includes(s) ||
      (h.environment || "").toLowerCase().includes(s)
    );
  });

  return (
    <div>
      <div className="spread" style={{ marginBottom: 14 }}>
        <input
          placeholder="Filter hosts…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ maxWidth: 280 }}
        />
        <span className="muted">{filtered.length} of {hosts.length}</span>
      </div>

      {filtered.length === 0 ? (
        <div className="empty">No hosts match.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Host</th>
              <th>Address</th>
              <th>Type</th>
              <th>Environment</th>
              <th>Transport</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((h) => (
              <tr key={h.id}>
                <td style={{ fontWeight: 600 }}>{h.label}</td>
                <td className="muted">{h.address || "—"}</td>
                <td className="muted">{h.type_text || "—"}</td>
                <td>{h.environment ? <span className="badge">{h.environment}</span> : <span className="faint">—</span>}</td>
                <td>
                  {h.has_agent
                    ? <span className="badge green">Agent</span>
                    : <span className="badge amber">SSH</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
