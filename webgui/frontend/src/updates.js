// What counts as "this host has updates waiting".
//
// It lives here, alone, because the answer was written out three times — the
// "N hosts with updates" count on Update Hosts, the "Select all with updates"
// button beside it, and the patch tile on the Dashboard — and all three said
// `(h.total || 0) > 0`.
//
// That is not the same question as "does this host have pending package
// updates", and on SUSE the difference is visible on the page. The two columns
// come from two different zypper commands:
//
//     PENDING  <- zypper list-updates            <- what `zypper update` installs
//     SECURITY <- zypper list-patches --category security
//                                                <- what `zypper patch` installs
//
// A Leap host with its security patch not yet applied legitimately reports
// PENDING 0 and SECURITY 1: there is no package update to make, and there IS a
// security patch to apply. Keying on `total` alone made those hosts invisible
// to the count and unselectable by the button — the page showed three rows with
// a red 1 in SECURITY, said "6 hosts with updates", and selected six. The only
// hosts in the fleet with outstanding SECURITY work were the three it would not
// select.
//
// dnf and apt hide this: there, a security update is also a package update, so
// security > 0 implies total > 0 and the old rule happened to be right.

/** True when this host has anything an operator could install right now. */
export function hasUpdates(h) {
  if (!h) return false;
  return (h.total || 0) > 0 || (h.security || 0) > 0;
}

/** Hosts that are reachable AND have something to install, by id. */
export function hostsWithUpdates(hosts) {
  return (hosts || []).filter((h) => h.online !== false && hasUpdates(h));
}

/** How many reachable hosts have something to install. */
export function countWithUpdates(hosts) {
  return hostsWithUpdates(hosts).length;
}

// ---------------------------------------------------------------------------
// ...and what counts as "we do not KNOW whether this host has updates".
//
// There was no such idea here, so there was no way to say it. A host whose scan
// failed comes back with total: null and security: null, and every reader did
// `(h.total || 0) > 0` — which is false, the same answer a fully patched host
// gives. The two were indistinguishable, so:
//
//   * "N hosts with updates" counted a fleet nobody could scan as a fleet with
//     nothing to install;
//   * the Dashboard's patch tile showed "Needs patching 0" for the same reason;
//   * and Update Hosts printed "All N hosts up to date" whenever that zero met
//     a zero security count and no reboot flag.
//
// Observed with nine of sixteen hosts reading "timed out waiting for agent":
// the header said 0 and 0, and only a single stray reboot flag on one unrelated
// host kept the all-clear banner from appearing over a fleet that was two
// thirds unscanned. On a PATCH COMPLIANCE screen that is the worst available
// failure — not a missing number, but a green one.
//
// Offline is NOT unknown. It has its own count and its own row treatment, and
// folding it in here would double-report it.

/** True when this host's patch state was never established. */
export function isUnknown(h) {
  if (!h) return true;
  if (h.online === false) return false;      // offline: separately counted
  return h.total == null && h.security == null;
}

/** How many reachable hosts came back without a usable answer. */
export function countUnknown(hosts) {
  return (hosts || []).filter(isUnknown).length;
}
