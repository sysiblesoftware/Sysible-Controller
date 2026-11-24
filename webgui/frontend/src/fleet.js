// One answer to "how is the fleet doing", for every surface that shows it.
//
// It lives here, alone, for the reason updates.js does: the question was
// written out more than once and the copies did not agree. The Dashboard
// carried `const STALE = 20` twice and aged `last_seen` itself — while
// /api/agents was ALREADY attaching an aged `online` flag computed from the
// server's own window (_AGENT_ONLINE_SECONDS, which an operator can tune). So
// the console re-derived a fact it was being told, from a constant that could
// drift away from the one the server uses, on a screen whose whole job is to
// agree with the rest of the product.
//
// Prefer what the server said. Age it here only for a controller old enough
// not to send the flag.

/** The server's default window, mirrored for that fallback only. */
export const STALE_SECONDS = 20;

/** Agents that count as part of the fleet. A revoked host is not enrolled. */
export function enrolled(inventory) {
  return (inventory || []).filter((a) => a && !a.revoked);
}

/** Is this agent reporting in? */
export function isOnline(a, now = Date.now() / 1000) {
  if (!a) return false;
  if (typeof a.online === "boolean") return a.online;      // what the server said
  return !!(a.last_seen && (now - a.last_seen) <= STALE_SECONDS);
}

/** {total, online, offline} over the enrolled agents. */
export function fleetCounts(inventory, now = Date.now() / 1000) {
  const inv = enrolled(inventory);
  const online = inv.filter((a) => isOnline(a, now)).length;
  return { total: inv.length, online, offline: inv.length - online };
}

// ---------------------------------------------------------------------------
// HEALTH, which is a wider question than reachability.
//
// The top-bar chip first reported "16 of 16 online", and online means one
// thing only: this agent has heartbeated recently. An agent can do that and
// still be no use. The clearest case is integrity quarantine -- the controller
// hands a quarantined agent NO TASKS at all (see _probe_updates and
// _probe_posture, which both fail it fast rather than wait out the deadline) --
// so it heartbeats on schedule, counts as online, and runs nothing anyone asks
// of it. Reported as the indicator not describing agent health.
//
// Only facts already on the inventory are used; the chip sits on every screen
// and must not start a sweep. Two of them are available and honest:
//
//   silent       the heartbeat has aged out
//   quarantined  integrity_quarantined -- enrolled, reporting, handed no work
//
// Deliberately NOT here: agent version. The console only learns the expected
// version from the response to an update-agents POST, so "out of date" cannot
// be answered from this payload, and guessing it would be worse than leaving
// it out.
//
// A silent agent may also be quarantined. It is counted ONCE, as silent: that
// is the condition in the way, and the count must not exceed the fleet.

/** "healthy" | "silent" | "quarantined" for one agent. */
export function agentHealth(a, now = Date.now() / 1000) {
  if (!isOnline(a, now)) return "silent";
  if (a && a.integrity_quarantined) return "quarantined";
  return "healthy";
}

/** {total, healthy, silent, quarantined} over the enrolled agents.
 *
 * NOT `fleetHealth`: that is api.fleetHealth(), the server-side sweep that
 * probes every host. This one is arithmetic over an inventory already in hand.
 * Two names one letter apart for a cheap local count and a fleet-wide probe is
 * how someone eventually calls the expensive one from a component that repaints
 * every fifteen seconds. */
export function agentHealthCounts(inventory, now = Date.now() / 1000) {
  const out = { total: 0, healthy: 0, silent: 0, quarantined: 0 };
  for (const a of enrolled(inventory)) {
    out.total += 1;
    out[agentHealth(a, now)] += 1;
  }
  return out;
}
