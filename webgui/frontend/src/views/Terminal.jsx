import React, { useEffect, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { api, terminalWsUrl } from "../api.js";

export default function Terminal() {
  const [hosts, setHosts] = useState([]);
  const [host, setHost] = useState("");
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");

  const elRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);

  useEffect(() => {
    api.hosts()
      .then((d) => {
        const agentful = (d.hosts || []);
        setHosts(agentful);
        if (agentful[0]) setHost(agentful[0].id);
      })
      .catch((x) => setErr(x.message));
    return () => disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function disconnect() {
    if (wsRef.current) {
      try { wsRef.current.close(); } catch { /* ignore */ }
      wsRef.current = null;
    }
    if (termRef.current) {
      termRef.current.dispose();
      termRef.current = null;
    }
    setConnected(false);
  }

  function connect() {
    if (!host) return;
    setErr("");
    setStatus("Connecting…");

    const term = new XTerm({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: 13,
      theme: { background: "#000000", foreground: "#e6edf3" },
      cursorBlink: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(elRef.current);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;

    const ws = new WebSocket(terminalWsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ t: "open", host, cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === "ready") {
        setConnected(true);
        setStatus("");
        term.focus();
      } else if (msg.t === "o") {
        term.write(msg.d);
      } else if (msg.t === "closed") {
        setStatus("Session closed.");
        disconnect();
      } else if (msg.t === "error") {
        setErr(msg.d || "terminal error");
        disconnect();
      }
    };
    ws.onclose = () => { setConnected(false); };
    ws.onerror = () => { setErr("WebSocket error — is the controller reachable?"); };

    term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d }));
    });

    const onResize = () => {
      if (!fitRef.current || !termRef.current) return;
      fitRef.current.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
      }
    };
    window.addEventListener("resize", onResize);
    ws._cleanup = () => window.removeEventListener("resize", onResize);
  }

  if (err && !connected) {
    // show error but keep the picker usable
  }

  return (
    <div className="term-wrap">
      <div className="row" style={{ marginBottom: 12 }}>
        <select
          value={host}
          onChange={(e) => setHost(e.target.value)}
          disabled={connected}
          style={{ maxWidth: 320 }}
        >
          {hosts.length === 0 && <option value="">No hosts</option>}
          {hosts.map((h) => (
            <option key={h.id} value={h.id}>
              {h.label}{!h.has_agent ? " (ssh)" : ""}
            </option>
          ))}
        </select>
        {!connected ? (
          <button className="btn" onClick={connect} disabled={!host}>Connect</button>
        ) : (
          <button className="btn secondary" onClick={disconnect}>Disconnect</button>
        )}
        {status && <span className="muted">{status}</span>}
      </div>

      {err && <div className="error-box" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="term-host" ref={elRef} style={{ display: connected || status ? "block" : "none" }} />
      {!connected && !status && (
        <div className="empty">Pick a host and connect to open a live shell.</div>
      )}
    </div>
  );
}
