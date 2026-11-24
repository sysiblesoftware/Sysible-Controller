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

  // LABELLED. The first version was a dot, a number, and "/ 16 agents" set in
  // 12px beside the sign-in line -- reported as not looking like anything and
  // not saying what the number was. A count with no noun is a riddle: 16 of
  // what, measured how. So it carries its own label, reads as a sentence, and
  // sits in a chip rather than floating as stray text next to other stray text.
  const chip = (tone, value, title) => (
    <button type="button" className={"fleet-chip " + tone} onClick={onOpen}
            title={title} aria-label={"Agents: " + value}>
      <span className="dot" />
      <span className="fleet-chip-label">Agents</span>
      <span className="fleet-chip-value">{value}</span>
    </button>
  );

  if (state.kind === "loading") {
    return chip("idle", "checking…", "Reading the agent inventory");
  }
  if (state.kind === "err") {
    // Not "0 online". The inventory could not be read, which says nothing at
    // all about the hosts.
    return chip("bad", "status unavailable",
                `Could not read the agent inventory: ${state.why}`);
  }

  const { total, online, offline } = state.counts;
  if (!total) {
    return chip("idle", "none enrolled", "No agents are enrolled on this controller");
  }
  // Every agent silent is its own thing: it usually means the controller's
  // address moved, not that a whole fleet died at once.
  const allSilent = online === 0;
  const tone = allSilent ? "bad" : offline ? "warn" : "ok";
  const title = allSilent
    ? "No agent has reported in. Every host is silent — check the controller's address."
    : offline
      ? `${online} of ${total} agents have reported in recently; ${offline} have not.`
      : `All ${total} agents have reported in recently.`;
  return chip(tone, `${online} of ${total} online`, title);
}
