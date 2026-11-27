import React, { useState } from "react";

// Copy whatever output is on screen. "I should be able to copy logs from ANYWHERE
// in the platforms" -- the Controller could already copy the things it ASKS you to
// paste elsewhere (enrollment commands, a generated password, a command preview)
// but not the output it produces: per-host results, package output, the activity
// log, the update logs. Those are what you need when something went wrong, and
// drag-selecting a pane that is still streaming loses the selection every time a
// line lands.
//
// `text` may be a function, so a long log is only turned into a string when the
// button is pressed rather than on every render of a streaming pane.
//
// navigator.clipboard needs a secure context. The hidden-textarea fallback keeps
// the button honest on a plain-http deployment, and the label says which happened
// -- a button that silently does nothing is worse than no button.
export default function CopyButton({ text, label = "Copy", title = "Copy to the clipboard", className = "btn ghost sm" }) {
  const [state, setState] = useState("");   // "" | "ok" | "fail"
  async function copy() {
    const s = typeof text === "function" ? text() : (text || "");
    if (!s) { setState("fail"); setTimeout(() => setState(""), 1500); return; }
    let ok = false;
    try {
      await navigator.clipboard.writeText(s);
      ok = true;
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = s;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select(); ta.setSelectionRange(0, s.length);
        ok = document.execCommand("copy");
        document.body.removeChild(ta);
      } catch { ok = false; }
    }
    setState(ok ? "ok" : "fail");
    setTimeout(() => setState(""), 1500);
  }
  return (
    <button type="button" className={className} onClick={copy}
      title={state === "fail" ? "Could not reach the clipboard — select the text and copy it by hand" : title}>
      {state === "ok" ? "✓ Copied" : state === "fail" ? "✗ Blocked" : label}
    </button>
  );
}
