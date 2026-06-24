import React, { useEffect, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api } from "../api.js";

// Sysible Connect in the browser: an xterm.js terminal bridged to the
// controller's SSH PTY through the BFF websocket (/api/terminal/ws).
// Terminals are SSH-based, so only SSH and agent+SSH (merged) hosts can
// be connected to.
export default function TerminalPage({ onBack }) {
  const [hosts, setHosts] = useState([]);
  const [host, setHost] = useState("");
  const [status, setStatus] = useState("idle"); // idle | connecting | connected | closed | error
  const [statusMsg, setStatusMsg] = useState("");

  const mountRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);

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

  // Tear everything down on unmount.
  useEffect(() => () => teardown(), []);

  function teardown() {
    if (wsRef.current) {
      try { wsRef.current.close(); } catch (_) {}
      wsRef.current = null;
    }
    if (termRef.current) {
      try { termRef.current.dispose(); } catch (_) {}
      termRef.current = null;
    }
  }

  function connect() {
    if (!host) return;
    teardown();
    setStatus("connecting");
    setStatusMsg("");

    const term = new XTerm({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", monospace',
      theme: { background: "#0e1116", foreground: "#e6e9ee", cursor: "#2ea0c9" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(mountRef.current);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/terminal/ws`);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ t: "open", host, cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.t === "o") term.write(m.d);
      else if (m.t === "ready") {
        setStatus("connected");
        term.focus();
      } else if (m.t === "closed") {
        setStatus("closed");
        term.write("\r\n\x1b[33m[session closed]\x1b[0m\r\n");
      } else if (m.t === "error") {
        setStatus("error");
        setStatusMsg(m.d || "error");
        term.write(`\r\n\x1b[31m[error] ${m.d || ""}\x1b[0m\r\n`);
      }
    };
    ws.onclose = () => {
      setStatus((s) => (s === "error" ? s : "closed"));
    };
    ws.onerror = () => {
      setStatus("error");
      setStatusMsg("websocket error");
    };

    // Keystrokes -> host.
    term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d }));
    });

    // Resize -> host (debounced via the browser's resize event).
    const onResize = () => {
      if (!fitRef.current || !termRef.current) return;
      fitRef.current.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
      }
    };
    window.addEventListener("resize", onResize);
    ws.addEventListener("close", () => window.removeEventListener("resize", onResize));
  }

  function disconnect() {
    teardown();
    setStatus("idle");
  }

  const dot =
    status === "connected" ? "#2ea043" :
    status === "connecting" ? "#f5a623" :
    status === "error" ? "#d9544d" : "#9aa5b1";

  return (
    <div className="toolpage">
      <div className="toolpage-bar">
        <button className="btn-ghost" onClick={onBack}>← Tools</button>
        <h2 className="page-title inline">Sysible Connect</h2>
      </div>

      <div className="term-controls card">
        <label className="field term-host">
          <span>Host (SSH)</span>
          <select value={host} onChange={(e) => setHost(e.target.value)} disabled={status === "connected"}>
            {hosts.length === 0 && <option value="">No SSH hosts enrolled</option>}
            {hosts.map((h) => (
              <option key={h.id} value={h.id}>{h.label} — {h.type_text}</option>
            ))}
          </select>
        </label>
        {status === "connected" || status === "connecting" ? (
          <button className="btn btn-danger" onClick={disconnect}>Disconnect</button>
        ) : (
          <button className="btn" onClick={connect} disabled={!host}>Connect</button>
        )}
        <span className="term-status">
          <span className="term-dot" style={{ background: dot }} />
          {status}{statusMsg ? ` — ${statusMsg}` : ""}
        </span>
      </div>

      <div className="term-shell card">
        <div ref={mountRef} className="term-mount" />
      </div>
    </div>
  );
}
