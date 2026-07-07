// Client-side suppression matching. The backend stores suppressions; the SPA
// decides whether one is currently ACTIVE for a given finding, because the
// live data needed to evaluate expiry (a host's current boot time) lives here.

export const SNOOZE_LABELS = { "1h": "1 hour", "1d": "1 day", "7d": "7 days", "30d": "30 days" };
export const TYPE_LABEL = {
  env_necessity: "Environmental necessity",
  acknowledged: "Accepted risk",
  reboot: "Until next reboot",
  snooze: "Snoozed",
};

// Is suppression `s` active for finding context ctx = {host, env, key, bootEpoch}?
export function suppActive(s, ctx, now = Date.now()) {
  if (s.key !== ctx.key) return false;
  if (s.scope === "host" && s.target !== ctx.host) return false;
  if (s.scope === "env" && s.target !== ctx.env) return false;
  if (s.type === "snooze") return !s.until || s.until * 1000 > now;
  if (s.type === "reboot") {
    // Clears once the host has rebooted past the baseline boot time.
    if (ctx.bootEpoch == null || s.boot_epoch == null) return true;
    return Number(ctx.bootEpoch) <= Number(s.boot_epoch);
  }
  return true;   // env_necessity / acknowledged — indefinite
}

// First active suppression for this finding, or null.
export function findSupp(supps, ctx) {
  const now = Date.now();
  for (const s of supps || []) if (suppActive(s, ctx, now)) return s;
  return null;
}

// Human one-liner for a suppression (for the suppressed list + chip tooltip).
export function describeSupp(s) {
  if (!s) return "";
  if (s.type === "snooze") {
    const when = s.until ? new Date(s.until * 1000).toLocaleString() : "";
    return `Snoozed until ${when}`;
  }
  const scope = s.scope === "env" ? `env ${s.target}` : "this host";
  return `${TYPE_LABEL[s.type] || s.type} · ${scope}`;
}
