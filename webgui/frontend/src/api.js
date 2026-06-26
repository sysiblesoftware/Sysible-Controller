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
  runTool: (action, targets, params) =>
    req(`/api/tool/${encodeURIComponent(action)}`, {
      method: "POST",
      body: { targets, params },
    }),
  // Sudo (become) password — encrypted at rest on the controller, per admin.
  sudoStatus: () => req("/api/sudo"),
  setSudo: (password, scope) =>
    req("/api/sudo", { method: "POST", body: { password, scope } }),
  clearSudo: (scope) =>
    req("/api/sudo", { method: "DELETE", body: { scope } }),
  // Sysible Connect
  fleet: (action, targets, command) =>
    req("/api/fleet", { method: "POST", body: { action, targets, command } }),
  checkin: () => req("/api/checkin", { method: "POST" }),
  controllerKey: () => req("/api/controller-key"),
  enrollSsh: (payload) => req("/api/enroll-ssh", { method: "POST", body: payload }),
  setHostEnvironment: (hostId, environment) =>
    req(`/api/host/${encodeURIComponent(hostId)}/environment`, { method: "POST", body: { environment } }),
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
};

// Websocket URL for the browser terminal (same origin, ws/wss to match page).
export function terminalWsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/terminal/ws`;
}
