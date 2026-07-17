import React, { useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { terminalWsUrl, revalidateSession } from "../api.js";

// One independent browser terminal: its own xterm instance + websocket to one
// host. Exposes imperative controls (send sudo password, font zoom, clear) so a
// toolbar can drive the active session.
const TerminalSession = forwardRef(function TerminalSession({ hostId, label, active, onStatus, onClosed }, ref) {
  const elRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const searchRef = useRef(null);
  const wsRef = useRef(null);
  const [font, setFont] = useState(13);

  useImperativeHandle(ref, () => ({
    sendSudo() {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "sudo" }));
    },
    sendCtrlC() {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d: "\x03" }));
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
    find(q, prev) {
      const s = searchRef.current; if (!s || !q) return;
      prev ? s.findPrevious(q) : s.findNext(q);
    },
    saveOutput() {
      const term = termRef.current; if (!term) return;
      const buf = term.buffer.active;
      const lines = [];
      for (let i = 0; i < buf.length; i++) lines.push(buf.getLine(i)?.translateToString(true) ?? "");
      const text = lines.join("\n").replace(/\n+$/, "") + "\n";
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
      a.download = `${label || hostId}-terminal.txt`;
      a.click(); URL.revokeObjectURL(a.href);
    },
    clear() { termRef.current && termRef.current.clear(); },
    focus() { termRef.current && termRef.current.focus(); },
  }));

  useEffect(() => {
    // Pull the app's palette so the terminal matches the console (not pure
    // black). Falls back to the dark defaults if the vars aren't present.
    const cssVar = (name, fallback) => {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    };
    const bg = cssVar("--bg", "#17191d");
    const fg = cssVar("--text", "#e7e9ec");
    const accent = cssVar("--accent", "#8c95cf");
    const term = new XTerm({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: font,
      theme: { background: bg, foreground: fg, cursor: accent,
               cursorAccent: bg, selectionBackground: "rgba(140,149,207,0.35)" },
      cursorBlink: true,
    });
    let ready = false;
    const dim = (s) => term.writeln("\x1b[2m" + s + "\x1b[0m");
    const fit = new FitAddon();
    const search = new SearchAddon();
    term.loadAddon(fit);
    term.loadAddon(search);
    term.open(elRef.current);
    try { fit.fit(); } catch { /* not visible yet */ }
    termRef.current = term;
    fitRef.current = fit;
    searchRef.current = search;

    const ws = new WebSocket(terminalWsUrl());
    wsRef.current = ws;
    onStatus && onStatus("connecting");
    dim(`Connecting to ${label || hostId}…`);

    ws.onopen = () => ws.send(JSON.stringify({ t: "open", host: hostId, label, cols: term.cols, rows: term.rows }));
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.t === "ready") { ready = true; onStatus && onStatus("connected"); dim("Connected."); term.focus(); }
      else if (m.t === "o") term.write(m.d);
      else if (m.t === "closed") { onStatus && onStatus("closed"); term.writeln("\r\n\x1b[33m[session closed]\x1b[0m"); onClosed && onClosed(); }
      // Write the reason INTO the terminal — otherwise a failed connection just
      // looks like a blank screen with a blinking cursor and no explanation.
      else if (m.t === "error") {
        onStatus && onStatus("error:" + (m.d || ""));
        term.writeln("\r\n\x1b[31m" + (m.d || "terminal error") + "\x1b[0m");
        dim("The host's own agent runs this shell locally and streams it back over its check-in channel — no inbound SSH to the host is used. Check that the host is online and its agent is checking in (Sysible Connect → ping), then reopen the terminal.");
      }
    };
    ws.onerror = () => onStatus && onStatus("error:websocket");
    ws.onclose = (e) => {
      onStatus && onStatus("disconnected");
      if (!ready) {
        term.writeln("\r\n\x1b[31mCould not connect — the controller closed the terminal connection.\x1b[0m");
        // A policy close (1008) before the session ever opened means the BFF rejected
        // the handshake — most often an expired/revoked login. The WS bypasses req(),
        // so re-check the session out-of-band: if it's gone, the app returns to login.
        if (e && e.code === 1008) revalidateSession();
      }
    };
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
