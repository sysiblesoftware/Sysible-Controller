// In-app confirmation that does NOT rely on window.confirm().
//
// Browsers suppress native confirm()/alert()/prompt() dialogs once the user
// ticks "prevent this page from creating more dialogs" (Chrome/Firefox show that
// checkbox after a couple of prompts), and site-settings / extensions can block
// them outright. After that, window.confirm() returns false WITHOUT prompting —
// so every confirm-gated button silently does nothing (the "force delete / revoke
// / regenerate does nothing" trap). This renders our own modal and resolves a
// Promise<boolean>, so a destructive action always gets a real yes/no.
export function confirmDialog(message, opts = {}) {
  const { okLabel = "Confirm", cancelLabel = "Cancel", danger = true } = opts;
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:99999;display:flex;align-items:center;" +
      "justify-content:center;background:rgba(0,0,0,0.55);padding:20px;";
    const box = document.createElement("div");
    box.style.cssText =
      "max-width:520px;width:100%;background:var(--panel,#1a2130);" +
      "color:var(--text,#e8eef7);border:1px solid var(--border,#2a3446);" +
      "border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.5);" +
      "padding:18px 20px;font:14px/1.5 system-ui,-apple-system,sans-serif;";
    const msg = document.createElement("div");
    msg.style.cssText = "white-space:pre-wrap;margin-bottom:16px;";
    msg.textContent = message;
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:10px;justify-content:flex-end;";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = cancelLabel;
    cancel.style.cssText =
      "padding:7px 14px;border-radius:7px;border:1px solid var(--border,#2a3446);" +
      "background:transparent;color:inherit;cursor:pointer;";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.textContent = okLabel;
    ok.style.cssText =
      "padding:7px 14px;border-radius:7px;color:#fff;cursor:pointer;border:1px solid " +
      (danger ? "#c0392b" : "#3a8a5c") + ";background:" + (danger ? "#c0392b" : "#2f6f4a") + ";";

    function close(val) {
      try { document.body.removeChild(overlay); } catch (e) { /* already gone */ }
      document.removeEventListener("keydown", onKey);
      resolve(val);
    }
    function onKey(e) {
      if (e.key === "Escape") close(false);
      else if (e.key === "Enter") close(true);
    }
    cancel.onclick = () => close(false);
    ok.onclick = () => close(true);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    document.addEventListener("keydown", onKey);

    row.append(cancel, ok);
    box.append(msg, row);
    overlay.append(box);
    document.body.appendChild(overlay);
    ok.focus();
  });
}
