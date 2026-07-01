// Thin fetch wrapper for the Sysible Web GUI BFF. Every call rides the
// signed http-only session cookie (credentials: "include"); the controller
// API key never reaches the browser. A 401 means "not logged in" and is
// surfaced so the app can bounce back to the login screen.

async function req(path, { method = "GET", body, headers, raw = false } = {}) {
  const opts = { method, credentials: "include", headers: { ...(headers || {}) } };
  if (body !== undefined) {
    if (body instanceof FormData) {
      opts.body = body; // browser sets multipart boundary
    } else {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  const res = await fetch(path, opts);
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
  logout: () => req("/api/logout", { method: "POST" }),
  me: () => req("/api/me"),
  edition: () => req("/api/edition"),
  hosts: () => req("/api/hosts"),
  environments: () => req("/api/environments"),
  tools: () => req("/api/tools"),
  fleetHealth: () => req("/api/fleet-health"),
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
  setControllerConfig: (cfg) =>
    req("/api/controller-config", { method: "POST", body: cfg }),
  controllerUpdate: () => req("/api/controller-update", { method: "POST" }),
  updateAgents: () => req("/api/update-agents", { method: "POST" }),
  auditLog: (limit = 200) => req(`/api/audit-log?limit=${limit}`),
  license: () => req("/api/license"),
  changeMyCredentials: (current_password, new_username, new_password) =>
    req("/api/admin/change-credentials", { method: "POST", body: { current_password, new_username, new_password } }),
  localIps: () => req("/api/local-ips"),
  tlsInfo: () => req("/api/tls-info"),
  trustCertUrl: () => "/api/trust-certificate",
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
  enrollToken: () => req("/api/enroll-token", { method: "POST" }),
  agentBundleUrl: () => "/api/agent-bundle",
  // Sudo (become) password — encrypted at rest on the controller, per admin.
  sudoStatus: () => req("/api/sudo"),
  setSudo: (password, scope) =>
    req("/api/sudo", { method: "POST", body: { password, scope } }),
  clearSudo: (scope) =>
    req("/api/sudo", { method: "DELETE", body: { scope } }),
  // Sysible Connect
  fleet: (action, targets, command, sudoPassword = "") =>
    req("/api/fleet", { method: "POST", body: { action, targets, command, sudo_password: sudoPassword } }),
  checkin: () => req("/api/checkin", { method: "POST" }),
  restartUnit: (hostId, unit, sudoPassword = "") =>
    req(`/api/host/${encodeURIComponent(hostId)}/restart-unit`,
        { method: "POST", body: { unit, sudo_password: sudoPassword } }),
  controllerKey: () => req("/api/controller-key"),
  enrollSsh: (payload) => req("/api/enroll-ssh", { method: "POST", body: payload }),
  setHostEnvironment: (hostId, environment) =>
    req(`/api/host/${encodeURIComponent(hostId)}/environment`, { method: "POST", body: { environment } }),
  environments: () => req("/api/environments"),
  createEnvironment: (name) => req("/api/environments", { method: "POST", body: { name } }),
  deleteEnvironment: (name) => req(`/api/environments/${encodeURIComponent(name)}`, { method: "DELETE" }),
  setHostSudo: (hostId, required) =>
    req(`/api/host/${encodeURIComponent(hostId)}/sudo`, { method: "POST", body: { required } }),
  envSudoDefaults: () => req("/api/environment-sudo-defaults"),
  setEnvSudoDefault: (name, required) =>
    req("/api/environment-sudo-default", { method: "POST", body: { name, required } }),
  removeHost: (hostId) =>
    req(`/api/host/${encodeURIComponent(hostId)}`, { method: "DELETE" }),
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

// Websocket URL for the browser terminal (same origin, ws/wss to match page).
export function terminalWsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/terminal/ws`;
}
