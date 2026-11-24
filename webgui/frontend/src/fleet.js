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
