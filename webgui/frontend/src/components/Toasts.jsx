import React from "react";

// A small fixed-position toast stack. Rendered once at the app shell; messages
// auto-dismiss (App owns the timers) and can be dismissed manually.
export default function Toasts({ items, onDismiss }) {
  if (!items.length) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={"toast " + (t.kind || "info")}>
          <div className="toast-body">
            {t.title && <div className="toast-title">{t.title}</div>}
            <div className="toast-msg">{t.msg}</div>
          </div>
          <button className="toast-x" onClick={() => onDismiss(t.id)} aria-label="Dismiss">×</button>
        </div>
      ))}
    </div>
  );
}
