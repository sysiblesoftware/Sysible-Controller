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
