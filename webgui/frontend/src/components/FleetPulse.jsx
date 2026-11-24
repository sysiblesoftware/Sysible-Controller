import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { fleetCounts } from "../fleet.js";

// The fleet, at the top of every screen.
//
// Asked for as "some kind of controller health bar at the top of controller to
// monitor agent health". Until now the only place that said whether agents were
// reporting in was the Dashboard — so the answer was a navigation away from
// anyone who was in the middle of doing something to the fleet.
//
// It reads /api/agents, which is the instant inventory (heartbeat ages, no
// traffic to any host) and already carries the server's own online flag.
//
// A FAILED poll says so. It does not keep showing the last good numbers, and it
// does not show zeros: "0 online" and "I could not ask" are different facts, and
// this session has spent most of its time on screens that confused the two.
export default function FleetPulse({ onOpen }) {
  const [state, setState] = useState({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await api.agents();
        if (alive) setState({ kind: "ok", counts: fleetCounts(d.agents || []) });
      } catch (e) {
        if (alive) setState({ kind: "err", why: e.message || String(e) });
      }
    };
    tick();
    const t = setInterval(tick, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  if (state.kind === "loading") {
    return <span className="faint" style={{ fontSize: 12 }}>
      <span className="spin" style={{ marginRight: 6 }} />fleet…
    </span>;
  }
  if (state.kind === "err") {
    return (
      <span className="pulse-bad" title={`Could not read the agent inventory: ${state.why}`}
            style={{ fontSize: 12, color: "var(--red)" }}>
        ⚠ fleet status unavailable
      </span>
    );
  }

  const { total, online, offline } = state.counts;
  if (!total) {
    return <span className="faint" style={{ fontSize: 12 }}>no agents enrolled</span>;
  }
  // Every agent silent is its own thing: it usually means the controller's
  // address moved, not that a whole fleet died at once.
  const allSilent = online === 0;
  const colour = allSilent ? "var(--red)" : offline ? "var(--amber)" : "var(--green)";
  return (
    <span
      role={onOpen ? "button" : undefined}
      onClick={onOpen}
      title={allSilent
        ? `No agent has reported in. Every host is silent — check the controller's address.`
        : offline
          ? `${online} of ${total} agents reporting in; ${offline} not.`
          : `All ${total} agents reporting in.`}
      style={{ fontSize: 12, cursor: onOpen ? "pointer" : undefined,
               display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="dot" style={{ background: colour }} />
      <span style={{ color: colour }}>{online}</span>
      <span className="faint">/ {total} agents</span>
      {offline > 0 && <span className="faint">· {offline} silent</span>}
    </span>
  );
}
