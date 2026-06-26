import React, { useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { terminalWsUrl } from "../api.js";

// One independent browser terminal: its own xterm instance + websocket to one
// host. Exposes imperative controls (send sudo password, font zoom, clear) so a
// toolbar can drive the active session.
const TerminalSession = forwardRef(function TerminalSession({ hostId, label, active, onStatus, onClosed }, ref) {
  const elRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);
  const [font, setFont] = useState(13);

  useImperativeHandle(ref, () => ({
    sendSudo() {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "sudo" }));
    },
    zoom(delta) {
      setFont((f) => {
        const nf = Math.min(Math.max(f + delta, 8), 28);
        const term = termRef.current, fit = fitRef.current, ws = wsRef.current;
        if (term) { term.options.fontSize = nf; try { fit.fit(); } catch { /* */ }
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows })); }
        return nf;
      });
    },
    clear() { termRef.current && termRef.current.clear(); },
    focus() { termRef.current && termRef.current.focus(); },
  }));

  useEffect(() => {
    const term = new XTerm({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: font,
      theme: { background: "#000000", foreground: "#e6edf3" },
      cursorBlink: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(elRef.current);
    try { fit.fit(); } catch { /* not visible yet */ }
    termRef.current = term;
    fitRef.current = fit;

    const ws = new WebSocket(terminalWsUrl());
    wsRef.current = ws;
    onStatus && onStatus("connecting");

    ws.onopen = () => ws.send(JSON.stringify({ t: "open", host: hostId, label, cols: term.cols, rows: term.rows }));
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.t === "ready") { onStatus && onStatus("connected"); term.focus(); }
      else if (m.t === "o") term.write(m.d);
      else if (m.t === "closed") { onStatus && onStatus("closed"); onClosed && onClosed(); }
      else if (m.t === "error") { onStatus && onStatus("error:" + (m.d || "")); }
    };
    ws.onerror = () => onStatus && onStatus("error:websocket");
    ws.onclose = () => onStatus && onStatus("disconnected");
    term.onData((d) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d })); });

    const onResize = () => {
      if (!active) return;
      try { fit.fit(); } catch { return; }
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
    };
    window.addEventListener("resize", onResize);
    return () => { window.removeEventListener("resize", onResize); try { ws.close(); } catch { /* */ } term.dispose(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!active) return;
    const t = setTimeout(() => {
      const term = termRef.current, fit = fitRef.current, ws = wsRef.current;
      if (!term || !fit) return;
      try { fit.fit(); } catch { return; }
      term.focus();
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
    }, 30);
    return () => clearTimeout(t);
  }, [active]);

  return <div className="term-host" ref={elRef} style={{ height: "100%" }} />;
});

export default TerminalSession;
