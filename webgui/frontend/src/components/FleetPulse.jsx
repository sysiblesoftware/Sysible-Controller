import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { agentHealthCounts } from "../fleet.js";

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
        if (alive) setState({ kind: "ok", counts: agentHealthCounts(d.agents || []) });
      } catch (e) {
        if (alive) setState({ kind: "err", why: e.message || String(e) });
      }
    };
    tick();
    const t = setInterval(tick, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // A BAR, finally. Three earlier versions were all the same mistake at
  // different sizes — a scrap of text beside the sign-in line. A dot and
  // "16 / 16 agents" read as nothing; a label named it; a pill was out of
  // place; dropping the pill left something reported as wimpy and as saying
  // nothing beyond "healthy". It was: one adjective, with the breakdown in a
  // tooltip nobody opens.
  const shell = (tone, body, title) => (
    <button type="button" className={"fleet-bar " + tone} onClick={onOpen} title={title}>
      {body}
    </button>
  );
  const head = (value, unit) => (
    <div className="fleet-bar-head">
      <div className="fleet-bar-k">Agent health</div>
      <div className="fleet-bar-n">{value}{unit ? <small> {unit}</small> : null}</div>
    </div>
  );

  if (state.kind === "loading") {
    return shell("", <>{head("—", "reading the inventory…")}</>, "Reading the agent inventory");
  }
  if (state.kind === "err") {
    // Not "0 healthy". The inventory could not be read, which says nothing at
    // all about the hosts.
    return shell("bad",
      <>{head("—", "status unavailable")}
       <div className="fleet-bar-note">
         The agent inventory could not be read, so this says nothing about the
         fleet. {state.why}
       </div></>,
      `Could not read the agent inventory: ${state.why}`);
  }

  const { total, healthy, silent, quarantined } = state.counts;
  if (!total) {
    return shell("",
      <>{head(0, "agents enrolled")}
       <div className="fleet-bar-note">No agents are enrolled on this controller yet.</div></>,
      "No agents are enrolled on this controller");
  }

  // Nothing healthy is its own thing. A whole fleet does not fail at once;
  // every agent silent usually means the controller's address moved.
  const tone = healthy === 0 ? "bad" : healthy < total ? "warn" : "ok";
  const pct = (n) => `${(n / total) * 100}%`;
  // Every segment is also a named count below — the bar is a second encoding of
  // the split, never the only one.
  const segs = [["ok", healthy], ["warn", silent], ["bad", quarantined]]
    .filter(([, n]) => n > 0);
  const legend = [
    ["ok", healthy, "healthy"],
    ["warn", silent, "silent"],
    ["bad", quarantined, "quarantined"],
    // A zero only earns a slot once it has been something other than zero;
    // otherwise a healthy fleet reads as a list of problems it does not have.
  ].filter(([, n], i) => n > 0 || i === 0);

  const why = [];
  if (silent) {
    why.push(silent === total
      ? `all ${silent} silent — no agent has reported in, which usually means the controller's address moved`
      : `${silent} silent (no recent heartbeat)`);
  }
  if (quarantined) {
    why.push(`${quarantined} quarantined — reporting in, but the controller hands them no work until their integrity baseline is re-sealed`);
  }
  const title = why.length
    ? `${healthy} of ${total} agents healthy. ` + why.join("; ") + ". Click to open the fleet."
    : `All ${total} agents are reporting in and able to take work. Click to open the fleet.`;

  return shell(tone,
    <>
      {head(healthy, `/ ${total} reporting`)}
      <div className="fleet-bar-track">
        {segs.map(([k, n]) => (
          <div key={k} className={"fleet-bar-seg " + k} style={{ width: pct(n) }} />
        ))}
      </div>
      <div className="fleet-bar-legend">
        {legend.map(([k, n, word]) => (
          <span key={k}><i className={"sw " + k} /><b>{n}</b> {word}</span>
        ))}
      </div>
    </>,
    title);
}
