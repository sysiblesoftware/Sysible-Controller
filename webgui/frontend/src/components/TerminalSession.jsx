import React, { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { terminalWsUrl } from "../api.js";

// One independent browser terminal: its own xterm instance + websocket to one
// host. Stays mounted while its tab exists (hidden when not active) so several
// sessions can run at once, like the desktop's pop-out terminals.
export default function TerminalSession({ hostId, active, onStatus, onClosed }) {
  const elRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);

  useEffect(() => {
    const term = new XTerm({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: 13,
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

    ws.onopen = () => ws.send(JSON.stringify({ t: "open", host: hostId, cols: term.cols, rows: term.rows }));
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

    return () => {
      window.removeEventListener("resize", onResize);
      try { ws.close(); } catch { /* ignore */ }
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // When this tab becomes active, refit to the now-visible size and tell the host.
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

  // No display:none — the dock hides inactive sessions with `visibility`,
  // which keeps a real layout box so xterm's fit() always has dimensions
  // (display:none collapses to 0×0 and renders the terminal blank).
  return <div className="term-host" ref={elRef} style={{ height: "100%" }} />;
}
