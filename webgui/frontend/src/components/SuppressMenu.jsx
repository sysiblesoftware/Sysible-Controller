import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { SNOOZE_LABELS } from "../suppress.js";

// A compact "Suppress ▾" control for one finding on one host. ctx carries the
// finding key, the host name + environment (the two possible scopes), and the
// host's current boot time (baseline for "remind after next reboot"). onDone is
// called after a suppression is created so the caller can refresh.
export default function SuppressMenu({ ctx, onDone, label = "Suppress" }) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState("host");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const canEnv = ctx.env && ctx.env !== "Unassigned";
  const target = scope === "host" ? ctx.host : ctx.env;

  const submit = async (type, snooze) => {
    setBusy(true); setErr("");
    try {
      await api.addSuppression({
        scope, target, key: ctx.key, type, snooze,
        boot_epoch: ctx.bootEpoch != null ? ctx.bootEpoch : undefined,
      });
      setOpen(false); onDone && onDone();
    } catch (e) { setErr(e.message || String(e)); } finally { setBusy(false); }
  };

  const Item = ({ children, onClick, disabled, title }) => (
    <button className="btn ghost sm" disabled={busy || disabled} title={title}
            onClick={onClick} style={{ display: "block", width: "100%", textAlign: "left", border: "none" }}>
      {children}
    </button>
  );

  return (
    <span ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
              title="Suppress this finding">{label} ▾</button>
      {open && (
        <div onClick={(e) => e.stopPropagation()}
             style={{ position: "absolute", right: 0, top: "100%", zIndex: 40, marginTop: 4, minWidth: 232,
                      background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8,
                      padding: 6, boxShadow: "0 8px 24px rgba(0,0,0,0.35)" }}>
          <div className="row" style={{ gap: 4, marginBottom: 6 }}>
            <button className={"btn sm" + (scope === "host" ? "" : " ghost")} onClick={() => setScope("host")}
                    style={{ flex: 1 }} title={`Just ${ctx.host}`}>This host</button>
            <button className={"btn sm" + (scope === "env" ? "" : " ghost")} onClick={() => canEnv && setScope("env")}
                    disabled={!canEnv} style={{ flex: 1 }} title={canEnv ? `All of ${ctx.env}` : "No environment"}>
              {canEnv ? `Env: ${ctx.env}` : "Env: —"}</button>
          </div>
          <Item onClick={() => submit("env_necessity")} title="Required here — silence indefinitely">
            Environmental necessity</Item>
          <Item onClick={() => submit("acknowledged")} title="Known, accepted risk — silence indefinitely">
            Acknowledge (accepted risk)</Item>
          <Item onClick={() => submit("reboot")} disabled={scope !== "host"}
                title={scope === "host" ? "Clears once this host reboots" : "Reboot scope is per-host only"}>
            Remind after next reboot</Item>
          <div className="faint" style={{ fontSize: 11, margin: "6px 6px 2px" }}>Snooze</div>
          <div className="row" style={{ gap: 4, flexWrap: "wrap", padding: "0 4px 2px" }}>
            {Object.entries(SNOOZE_LABELS).map(([k, l]) => (
              <button key={k} className="btn ghost sm" disabled={busy} onClick={() => submit("snooze", k)}>{l}</button>
            ))}
          </div>
          {err && <div className="error-box" style={{ fontSize: 11, marginTop: 4 }}>{err}</div>}
        </div>
      )}
    </span>
  );
}
