// Thin fetch wrapper. All requests are same-origin and rely on the
// http-only session cookie set by the BFF at login, so there is no token
// handling here and the controller API key never reaches the browser.

async function req(method, path, body) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`/api${path}`, opts);
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* empty / non-JSON body */
  }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText;
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  login: (username, password) => req("POST", "/login", { username, password }),
  logout: () => req("POST", "/logout"),
  me: () => req("GET", "/me"),
  edition: () => req("GET", "/edition"),
  hosts: () => req("GET", "/hosts"),
  environments: () => req("GET", "/environments"),
  tools: () => req("GET", "/tools"),
  runTool: (action, targets, params) =>
    req("POST", `/tool/${action}`, { targets, params }),
};
