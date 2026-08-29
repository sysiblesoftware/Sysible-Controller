// Thin fetch wrapper for the Sysible Web GUI BFF. Every call rides the
// signed http-only session cookie (credentials: "include"); the controller
// API key never reaches the browser. A 401 means "not logged in" and is
// surfaced so the app can bounce back to the login screen.

// A global "session is gone" handler. api.js calls it whenever the BFF answers a
// 401 (other than a failed login), so the app can drop the stale session and bounce
// back to the login screen instead of leaving the user on a dead, half-loaded UI.
let _onUnauthorized = null;
export function setUnauthorizedHandler(fn) { _onUnauthorized = fn; }
function _fireUnauthorized() {
  if (_onUnauthorized) { try { _onUnauthorized(); } catch { /* never let the handler mask the error */ } }
}

// Some surfaces bypass req() — direct fetch() downloads need the raw Response
// for blob handling. They must funnel into the SAME bounce-to-login handler,
// otherwise an expired session there just shows an error and strands the user.
// This helper is how they do it:

// For a raw fetch() response: bounce if it's a 401.
export function noteRawStatus(status) {
  if (status === 401) _fireUnauthorized();
}

// Default per-request timeout (ms) for JSON calls. Guards against a hung controller
// wedging a caller's busy/spinner state forever. `raw` responses (streams, uploads,
// downloads) legitimately run long, so they default to no timeout; any caller can
// override with `timeout` (ms), or `timeout: 0` to disable.
const DEFAULT_TIMEOUT_MS = 60000;

async function req(path, { method = "GET", body, headers, raw = false, timeout } = {}) {
  const opts = { method, credentials: "include", headers: { ...(headers || {}) } };
  if (body !== undefined) {
    if (body instanceof FormData) {
      opts.body = body; // browser sets multipart boundary
    } else {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  const ms = timeout !== undefined ? timeout : (raw ? 0 : DEFAULT_TIMEOUT_MS);
  const ctrl = ms > 0 ? new AbortController() : null;
  if (ctrl) opts.signal = ctrl.signal;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), ms) : null;

  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (timer) clearTimeout(timer);
    if (e && e.name === "AbortError") {
      const te = new Error("Request timed out — the controller didn't respond in time. Try again.");
      te.status = 0; te.timeout = true;
      throw te;
    }
    throw e; // network/DNS error — surfaced to the caller as-is
  }
  if (timer) clearTimeout(timer);

  // Session expired / not authenticated: fire the global handler so every screen
  // reacts the same way. Skip the login endpoint itself (a wrong password is a 401
  // that must NOT be treated as an expired session).
  if (res.status === 401 && path !== "/api/login") _fireUnauthorized();

  if (raw) return res;
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  login: (username, password) =>
    req("/api/login", { method: "POST", body: { username, password } }),
  // First-run: is there no administrator yet? and create the first one.
  setupRequired: () => req("/api/admin/setup-required"),
  setup: (username, password) =>
    req("/api/admin/setup", { method: "POST", body: { username, password } }),
  logout: () => req("/api/logout", { method: "POST" }),
  me: () => req("/api/me"),
  edition: () => req("/api/edition"),
  hosts: () => req("/api/hosts"),
  environments: () => req("/api/environments"),
  tools: () => req("/api/tools"),
  fleetHealth: (refresh) => req("/api/fleet-health" + (refresh ? "?refresh=1" : "")),
  fleetMetrics: (window = 3600) => req(`/api/fleet-metrics?window=${window}`),
  hostSnapshot: (hostId) => req(`/api/host-snapshot/${encodeURIComponent(hostId)}`),
  // Posture / compliance (read-only sweep + per-host drill-down)
  fleetPosture: (refresh = false) => req(`/api/fleet-posture${refresh ? "?refresh=1" : ""}`),
  hostPosture: (hostId) => req(`/api/host-posture/${encodeURIComponent(hostId)}`),
  pathCritical: (paths) => req("/api/path-critical", { method: "POST", body: { paths } }),
  runTool: (action, targets, params) =>
    req(`/api/tool/${encodeURIComponent(action)}`, {
      method: "POST",
      body: { targets, params },
    }),
  // Live Activity & Logs
  activity: (limit = 200, sinceId = 0) =>
    req(`/api/activity?limit=${limit}&since_id=${sinceId}`),
  controllerLog: (lines = 400) => req(`/api/controller-log?lines=${lines}`),
  // Settings
  admins: () => req("/api/admins"),
  addAdmin: (username, password, role) =>
    req("/api/admins", { method: "POST", body: { username, password, role } }),
  removeAdmin: (username) =>
    req(`/api/admins/${encodeURIComponent(username)}`, { method: "DELETE" }),
  resetAdminPassword: (username, new_password) =>
    req(`/api/admins/${encodeURIComponent(username)}/password`, { method: "POST", body: { new_password } }),
  setAdminSudoConnect: (username, allowed) =>
    req(`/api/admins/${encodeURIComponent(username)}/sudo-connect`, { method: "POST", body: { allowed } }),
  setAdminRole: (username, role) =>
    req(`/api/admins/${encodeURIComponent(username)}/role`, { method: "POST", body: { role } }),
  passwordPolicy: () => req("/api/password-policy"),
  setPasswordPolicy: (policy) =>
    req("/api/password-policy", { method: "POST", body: policy }),
  controllerConfig: () => req("/api/controller-config"),
  controllerVersion: () => req("/api/version"),
  setControllerConfig: (cfg) =>
    req("/api/controller-config", { method: "POST", body: cfg }),
  controllerRestart: () => req("/api/controller-restart", { method: "POST" }),
  decommissionController: (confirm) => req("/api/controller/decommission", { method: "POST", body: { confirm } }),
  controllerUpdate: () => req("/api/controller-update", { method: "POST" }),
  rebuildWebgui: () => req("/api/webgui-rebuild", { method: "POST" }),
  controllerUpdateStatus: () => req("/api/controller-update-status"),
  controllerUpdateLog: (lines = 400) => req(`/api/controller-update-log?lines=${lines}`),
  updateAgents: () => req("/api/update-agents", { method: "POST" }),
  updateStatus: () => req("/api/update-status"),
  healthWarnings: () => req("/api/health-warnings"),
  auditLog: (limit = 200) => req(`/api/audit-log?limit=${limit}`),
  license: () => req("/api/license"),
  changeMyCredentials: (current_password, new_username, new_password) =>
    req("/api/admin/change-credentials", { method: "POST", body: { current_password, new_username, new_password } }),
  localIps: () => req("/api/local-ips"),
  tlsInfo: () => req("/api/tls-info"),
  trustCertUrl: () => "/api/trust-certificate",
  regenerateSelfSignedCert: () => req("/api/tls-regenerate-self-signed", { method: "POST" }),
  installCertificate: (certFile, keyFile, chainFile) => {
    const fd = new FormData();
    fd.append("cert", certFile);
    fd.append("key", keyFile);
    if (chainFile) fd.append("chain", chainFile);
    return req("/api/tls-certificate", { method: "POST", body: fd });
  },
  envPolicy: () => req("/api/environmental-policy"),
  setEnvPolicy: (policy) => req("/api/environmental-policy", { method: "POST", body: policy }),
  // Webserver Portal
  portalStatus: () => req("/api/portal/status"),
  portalStart: () => req("/api/portal/start", { method: "POST" }),
  portalStop: () => req("/api/portal/stop", { method: "POST" }),
  portalSetPort: (port) => req("/api/portal/config", { method: "POST", body: { port } }),
  portalSetCreds: (username, password, current_password) =>
    req("/api/portal/credentials", { method: "POST", body: { username, password, current_password } }),
  portalRemoveCreds: (current_password) =>
    req("/api/portal/credentials", { method: "DELETE", body: { current_password } }),
  portalLoginHistory: (limit = 200) => req(`/api/portal/login-history?limit=${limit}`),
  portalSessions: () => req("/api/portal/sessions"),
  portalRevokeSession: (id) => req(`/api/portal/sessions/${encodeURIComponent(id)}/revoke`, { method: "POST" }),
  portalUploads: () => req("/api/portal/uploads"),
  portalUploadUrl: (name) => `/api/portal/uploads/${encodeURIComponent(name)}`,
  portalUploadDelete: (name) => req(`/api/portal/uploads/${encodeURIComponent(name)}`, { method: "DELETE" }),
  portalDownloads: () => req("/api/portal/downloads"),
  portalStageDownload: (file) => { const fd = new FormData(); fd.append("file", file); return req("/api/portal/downloads", { method: "POST", body: fd }); },
  portalDownloadDelete: (name) => req(`/api/portal/downloads/${encodeURIComponent(name)}`, { method: "DELETE" }),
  // User & Group — live host inventory
  usersSync: (hostId) => req("/api/users/sync", { method: "POST", body: { host_id: hostId } }),
  servicesList: (hostId, running) => req("/api/services/list", { method: "POST", body: { host_id: hostId, running } }),
  packagesList: (hostId) => req("/api/packages/list", { method: "POST", body: { host_id: hostId } }),
  installLocalPackage: (file, targets) => { const fd = new FormData(); fd.append("file", file); fd.append("targets", JSON.stringify(targets)); return req("/api/packages/install-local", { method: "POST", body: fd }); },
  // Host Enrollment
  agents: () => req("/api/agents"),
  // Same-identity migration: re-point selected agents at a NEW controller address
  // (shared-DB failover / IP change). body: { new_url, host_ids }.
  migrateAgents: (body) => req("/api/migrate-agents", { method: "POST", body }),
  revokeHost: (hostId) =>
    req(`/api/host/${encodeURIComponent(hostId)}/revoke`, { method: "POST" }),
  resumeHost: (hostId) =>
    req(`/api/host/${encodeURIComponent(hostId)}/resume`, { method: "POST" }),
  restoreHost: (hostId) =>
    req(`/api/host/${encodeURIComponent(hostId)}/restore`, { method: "POST" }),
  enrollToken: () => req("/api/enroll-token", { method: "POST" }),
  // Mint a host-bound reissue token to re-enroll an EXISTING host after a reinstall
  // that wiped its agent secret (a bare enroll token is refused for re-binding).
  reissueToken: (hostId) =>
    req("/api/enroll-token/reissue", { method: "POST", body: { host_id: hostId } }),
  // Runaway kill-switch: pause/resume all new agent enrollment (superuser).
  enrollmentPause: () => req("/api/enrollment-pause"),
  setEnrollmentPause: (paused) =>
    req("/api/enrollment-pause", { method: "POST", body: { paused } }),
  enrollAllowlist: () => req("/api/enroll-allowlist"),
  addEnrollAllowlist: (cidr, note) =>
    req("/api/enroll-allowlist", { method: "POST", body: { cidr, note } }),
  removeEnrollAllowlist: (id) =>
    req(`/api/enroll-allowlist/${id}`, { method: "DELETE" }),
  // Cache-buster so a repeat download after a controller hostname/IP change
  // can't be served from the browser cache (server also sends no-store).
  agentBundleUrl: () => `/api/agent-bundle?t=${Date.now()}`,
  // Sudo (become) password — encrypted at rest on the controller, per admin.
  sudoStatus: () => req("/api/sudo"),
  setSudo: (password, scope) =>
    req("/api/sudo", { method: "POST", body: { password, scope } }),
  clearSudo: (scope) =>
    req("/api/sudo", { method: "DELETE", body: { scope } }),
  // Run an action (script/reboot/power-off/restart-agent) across managed hosts.
  fleet: (action, targets, command, sudoPassword = "") =>
    req("/api/fleet", { method: "POST", body: { action, targets, command, sudo_password: sudoPassword } }),
  // Per-host operator metadata (tags / owner / notes / criticality).
  hostMeta: () => req("/api/host-meta"),
  setHostMeta: (name, meta) =>
    req(`/api/host-meta/${encodeURIComponent(name)}`, { method: "POST", body: meta }),
  // Finding suppressions — silence a dashboard finding on a host or environment.
  suppressions: () => req("/api/suppressions"),
  addSuppression: (body) => req("/api/suppressions", { method: "POST", body }),
  removeSuppression: (id) => req(`/api/suppressions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // Fleet Query — ask one read-only question across the fleet.
  fleetQueryTypes: () => req("/api/fleet-query/types"),
  fleetQuery: (qtype, arg, targets = []) =>
    req("/api/fleet-query", { method: "POST", body: { qtype, arg, targets } }),
  fleetUpdates: (refresh = 0, live = 0) =>
    req("/api/fleet-updates" + (live ? "?refresh=1&live=1" : refresh ? "?refresh=1" : "")),
  fleetInstall: (targets, kind, flags = "") => req("/api/fleet-updates/install", { method: "POST", body: { targets, kind, flags } }),
  fleetInstallStatus: (jobId) => req(`/api/fleet-updates/install-status/${encodeURIComponent(jobId)}`),
  schedules: () => req("/api/schedules"),
  scheduleCreate: (body) => req("/api/schedules", { method: "POST", body }),
  scheduleUpdate: (id, body) => req(`/api/schedules/${encodeURIComponent(id)}`, { method: "PATCH", body }),
  scheduleDelete: (id) => req(`/api/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }),
  scheduleRunNow: (id) => req(`/api/schedules/${encodeURIComponent(id)}/run-now`, { method: "POST" }),
  alertsGet: () => req("/api/alerts"),
  alertsSet: (body) => req("/api/alerts", { method: "POST", body }),
  alertsTest: () => req("/api/alerts/test", { method: "POST" }),
  restartUnit: (hostId, unit, sudoPassword = "") =>
    req(`/api/host/${encodeURIComponent(hostId)}/restart-unit`,
        { method: "POST", body: { unit, sudo_password: sudoPassword, mode: "restart" } }),
  resetUnit: (hostId, unit, sudoPassword = "") =>
    req(`/api/host/${encodeURIComponent(hostId)}/restart-unit`,
        { method: "POST", body: { unit, sudo_password: sudoPassword, mode: "reset-failed" } }),
  setHostEnvironment: (hostId, environment) =>
    req(`/api/host/${encodeURIComponent(hostId)}/environment`, { method: "POST", body: { environment } }),
  createEnvironment: (name) => req("/api/environments", { method: "POST", body: { name } }),
  deleteEnvironment: (name) => req(`/api/environments/${encodeURIComponent(name)}`, { method: "DELETE" }),
  setHostSudo: (hostId, required) =>
    req(`/api/host/${encodeURIComponent(hostId)}/sudo`, { method: "POST", body: { required } }),
  envSudoDefaults: () => req("/api/environment-sudo-defaults"),
  setEnvSudoDefault: (name, required) =>
    req("/api/environment-sudo-default", { method: "POST", body: { name, required } }),
  removeHost: (hostId, force = false) =>
    req(`/api/host/${encodeURIComponent(hostId)}${force ? "?force=1" : ""}`, { method: "DELETE" }),
  uploadFile: (host, remotePath, file) => {
    const fd = new FormData();
    fd.append("host", host);
    fd.append("remote_path", remotePath);
    fd.append("file", file);
    return req("/api/files/upload", { method: "POST", body: fd });
  },
  downloadUrl: (host, path) =>
    `/api/files/download?host=${encodeURIComponent(host)}&path=${encodeURIComponent(path)}`,
  compareFile: (path, targets) =>
    req("/api/files/compare", { method: "POST", body: { path, targets } }),
};
