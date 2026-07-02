#!/usr/bin/env python3
"""
Generate the Sysible Controller HTML guides from the current source of truth.

Produces (at the repo root):
  - Sysible_Controller_Documentation.html   full administrator & user guide
  - Sysible_Controller_Quickstart.html       condensed quick-start

Web-console only (the desktop GUI was removed). The tool list is parsed live
from webgui/frontend/src/views/ToolRunner.jsx so it never drifts; the version
comes from version.py. No external dependencies. Re-run after changing tools or
features:  python3 tools/generate_docs.py
"""
import datetime
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_version():
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', (ROOT / "version.py").read_text())
    return m.group(1) if m else "0.0.0"


def read_tools():
    """[(name, description)] from ToolRunner.jsx's TOOLS array (excluding the
    hidden Quick System Actions tile, which has its own section)."""
    src = (ROOT / "webgui/frontend/src/views/ToolRunner.jsx").read_text()
    block = re.search(r"const TOOLS = \[(.*?)\n\];", src, re.S)
    tools = []
    if block:
        for name, desc in re.findall(r'\["((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"', block.group(1)):
            if name == "Quick System Actions":
                continue
            tools.append((name, desc.replace('\\"', '"')))
    return tools


CSS = """
:root{--navy:#1a2744;--blue:#2f6fed;--ink:#202733;--dim:#5b6573;--hair:#e2e6ee;
--bg:#eef1f6;--paper:#fff;--green:#2f9e44;--amber:#e0a93c;--red:#e05656;--code:#f4f6fa;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.6;font-size:15px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.page{max-width:900px;margin:0 auto 28px;background:var(--paper);padding:52px 60px;
box-shadow:0 0 40px rgba(0,0,0,.08);}
.cover{text-align:center;padding:90px 40px;background:linear-gradient(160deg,#1a2744,#2f6fed);color:#fff;}
.cover h1{font-size:40px;margin:0 0 8px;} .cover .sub{font-size:18px;opacity:.9;}
.cover .meta{margin-top:34px;font-size:13px;opacity:.8;}
h2{color:var(--navy);border-bottom:2px solid var(--hair);padding-bottom:6px;margin-top:38px;font-size:24px;}
h3{color:var(--navy);margin-top:26px;font-size:18px;}
a{color:var(--blue);text-decoration:none;} a:hover{text-decoration:underline;}
code{background:var(--code);border:1px solid var(--hair);border-radius:4px;padding:1px 5px;font-size:13px;}
pre{background:#1e2430;color:#e6e9ef;border-radius:8px;padding:14px 16px;overflow:auto;font-size:13px;}
pre code{background:none;border:none;color:inherit;padding:0;}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px;}
th,td{border:1px solid var(--hair);padding:8px 10px;text-align:left;vertical-align:top;}
th{background:#f6f8fc;}
.toc{columns:2;font-size:14px;} .toc a{display:block;padding:2px 0;}
.card{border:1px solid var(--hair);border-radius:8px;padding:12px 16px;margin:10px 0;}
.card h4{margin:0 0 4px;color:var(--navy);font-size:15px;}
.card p{margin:0;color:var(--dim);font-size:14px;}
.note{border-left:4px solid var(--blue);background:#f3f7ff;padding:10px 14px;border-radius:0 6px 6px 0;margin:14px 0;}
.role{border-left:4px solid var(--green);} .warn{border-left:4px solid var(--amber);background:#fff9ec;}
ol li,ul li{margin:4px 0;}
.pill{display:inline-block;background:#eef2fb;color:var(--blue);border-radius:10px;padding:1px 9px;font-size:12px;font-weight:600;}
footer{max-width:900px;margin:0 auto 40px;text-align:center;color:var(--dim);font-size:12px;padding:10px;}
"""


def page(inner):
    return f'<div class="page">{inner}</div>'


def tool_cards(tools):
    return "\n".join(
        f'<div class="card"><h4>{html.escape(n)}</h4><p>{html.escape(d)}</p></div>'
        for n, d in tools)


def build_full(version, tools, today):
    toc_items = [
        ("overview", "1. Overview"), ("requirements", "2. Requirements"),
        ("install", "3. Installation"), ("first-login", "4. First login & accounts"),
        ("roles", "5. Roles & identity"), ("sudo", "6. Sudo modes"),
        ("dashboard", "7. Dashboard"), ("updates", "8. Update Hosts (patching)"),
        ("schedules", "9. Scheduled jobs"), ("alerts", "10. Alerting"),
        ("performance", "11. Performance"), ("enroll", "12. Host enrollment"),
        ("tools", "13. System Administration Tools"), ("qsa", "14. Quick System Actions"),
        ("connect", "15. Sysible Connect"), ("activity", "16. Activity & logs"),
        ("settings", "17. Settings"), ("portal", "18. Webserver Portal"),
        ("cli", "19. CLI reference"), ("security", "20. Security model"),
    ]
    toc = "".join(f'<a href="#{i}">{html.escape(t)}</a>' for i, t in toc_items)

    cover = (f'<div class="cover"><h1>Sysible Controller</h1>'
             f'<div class="sub">Administrator &amp; User Guide</div>'
             f'<div class="meta">Version {version} · {today} · Web console edition</div></div>')

    body = f"""
<h2 id="overview">1. Overview</h2>
<p><b>Sysible Controller</b> is a self-hosted, point-and-click way to manage a fleet of Linux hosts.
It is two pieces: a <b>FastAPI backend</b> (the <code>sysible-backend</code> service, HTTPS on port 9000,
holding the fleet inventory, credentials, and task queue in SQLite) and a <b>browser web console</b>
(the <code>sysible-webgui</code> service, HTTPS on port 8800). You administer everything from any browser on
the network — no desktop environment is needed on the controller or your workstation.</p>
<p>Hosts are managed two interchangeable ways, mixable in one fleet:</p>
<ul>
<li><b>The Sysible agent</b> — a small Python daemon on the host that heartbeats to the controller and polls for work.</li>
<li><b>Direct SSH</b> — hand the controller SSH credentials once; it installs its own key and drives the host from then on.</li>
</ul>
<div class="note"><b>Every action runs as the administrator who triggered it.</b> When <code>alice</code> runs a
command, the agent executes it as the local <code>alice</code> account (<code>runuser -u alice</code>) with that user's
sudo rights — not as a faceless root daemon. Reads are tried unprivileged first, then escalated only when the OS
reports a privilege error. The run-as identity comes from the signed login token, never from the browser.</div>

<h2 id="requirements">2. Requirements</h2>
<ul>
<li>A Linux controller host — <b>Debian/Ubuntu</b>, <b>RHEL/CentOS/Fedora</b>, or <b>openSUSE/SLES</b> — with root for install.</li>
<li>Python 3 (provisioned by the installer) and Node.js 18+ (installed by the installer; build-time only for the web console).</li>
<li>Any modern browser on a machine that can reach the controller. No desktop environment required.</li>
</ul>

<h2 id="install">3. Installation</h2>
<pre><code># from the folder containing the project files
sudo ./install_sysible.sh</code></pre>
<p>The installer deploys to <code>/opt/sysible</code>, creates a Python venv, generates a self-signed TLS
certificate and admin API key, installs the two systemd services (<code>sysible-backend</code>,
<code>sysible-webgui</code>), builds the React front end, and installs the <code>sysible_controller</code> CLI.
The backend and web console start independently:</p>
<pre><code>sudo sysible_controller start          # backend (port 9000)
sudo sysible_controller webgui start   # web console (https://&lt;host&gt;:8800/)</code></pre>

<h2 id="first-login">4. First login &amp; administrator accounts</h2>
<p>A fresh install <b>seeds a default superuser</b> named <code>admin</code> with a one-time password printed
<b>once, in red, at the end of the install output</b>. Sign in at <code>https://&lt;controller&gt;:8800/</code>, then
change it under <b>Settings → My Account</b>. If administrators already existed, no default is seeded — use
<code>sudo sysible_controller reset-admin</code> to set one.</p>

<h2 id="roles">5. Roles &amp; identity</h2>
<div class="card role"><h4>Superuser</h4><p>Full control, plus manages other administrators, TLS, the Webserver Portal, and controller/agent updates.</p></div>
<div class="card role"><h4>Sysadmin</h4><p>Manages the fleet — every tool, Connect, enrollment, scheduling, alerting — but not administrators or the portal/TLS.</p></div>
<div class="card role"><h4>Auditor</h4><p>Read-only oversight: sees the dashboard, patch status, performance, and the activity log, but <b>cannot act</b>. Enforced server-side, not just hidden in the UI.</p></div>

<h2 id="sudo">6. Sudo modes — passwordless or password ("become")</h2>
<p>Privileged commands run as your mapped user and escalate via that host's sudo. Passwordless-sudo hosts just work.
For hosts that require a sudo password, store yours once with the header's <b>Sudo Password</b> button (encrypted at
rest on the controller, fed to <code>sudo -S</code> over stdin only). Read-only actions (listing services/packages,
check-in) never need sudo.</p>

<h2 id="dashboard">7. Dashboard</h2>
<p>The home screen: a fleet-health overview (per-environment cards with disk/memory meters and problem signals) and a
compliance/posture strip. Click a count (online/offline/enrolled) or a compliance signal to see exactly which hosts.
Click a host to open its <b>posture drill-down</b>, where every warn/bad finding is <b>actionable</b> — reboot right
there, or jump straight to the tool that fixes it (SSH → Security Administration, cert → Certificate Management, etc.).</p>

<h2 id="updates">8. Update Hosts (patching)</h2>
<p>A fleet-wide patch view: pending updates, security updates, and reboot-required per host, grouped by environment.
<b>Rescan</b> recounts from cached repo metadata (fast); <b>Refresh metadata &amp; rescan</b> forces a live repo refresh.
Select hosts and <b>Install security updates</b> or <b>Install all updates</b> — installs run in the background with a
<b>live per-host progress bar and command output</b>, and the counts refresh as hosts finish. <b>Reboot</b> selected
hosts from the same screen.</p>

<h2 id="schedules">9. Scheduled jobs</h2>
<p>Recurring, unattended maintenance you set once. Pick an action, targets (all or selected hosts), and a cadence
(hourly/daily/weekly at a chosen time). Actions: rescan patch status, rescan posture, install security/all updates,
clean package cache, vacuum journal logs, trim filesystems (fstrim), clear failed units, sync clock, restart a named
service, run a shell command, or reboot. Jobs run on the controller and dispatch as root on agent hosts. Each job
shows its next/last run; <b>Run now</b>, pause, and delete are one click.</p>

<h2 id="alerts">10. Alerting</h2>
<p>Notify on threshold crossings via <b>email (SMTP)</b> and/or a <b>Slack-compatible webhook</b>. Built-in rules
include host offline, disk/memory/load thresholds, failed units, OOM kills, pending/security updates, reboot required,
cert expiry, firewall disabled, SELinux/AppArmor not enforcing, SSH root login, and clock drift. <b>Custom regex
rules</b> run a command on each host and alert when its output matches (or fails to match) a pattern. The controller
evaluates rules every ~5 minutes, fires once per condition, and resolves when it clears. The SMTP password is encrypted
at rest and never returned to the browser.</p>

<h2 id="performance">11. Performance</h2>
<p>Time-series graphs (CPU, memory, swap, disk, network, disk I/O, load, processes) per environment or drilled into a
single host. Hover for a crosshair and per-series values; <b>drag to zoom</b> a time range (shared across all charts);
click a chart title to <b>enlarge it</b> into its own window for analysis.</p>

<h2 id="enroll">12. Host enrollment</h2>
<p><b>Host Enrollment</b> (superuser) gives you the agent bundle to install on a host, and one-click <b>SSH enrollment</b>
(password used once, then discarded for a generated key). Agent hosts are auto-enrolled for SSH so they also get a real
terminal when an SSH server is present.</p>

<h2 id="tools">13. System Administration Tools</h2>
<p>Every tool follows the same pattern: <b>check the target hosts</b> on the left, fill any fields, and run an action —
results come back per host, <b>grouped by environment</b>, each collapsible with a one-line summary, an "Only problems"
filter, and a search.</p>
{tool_cards(tools)}

<h2 id="qsa">14. Quick System Actions</h2>
<p>A fast lane for everyday fixes across selected hosts, in its own sidebar item: restart/start/stop a service (with a
live service browser — list running services on a host and click to pick one), restart NetworkManager / SSH / time sync
/ Docker / the Sysible agent, flush DNS, sync the clock, free memory, clean package cache, vacuum journal logs, trim
filesystems, clear failed units, reload systemd, and reboot / power off.</p>

<h2 id="connect">15. Sysible Connect</h2>
<p>A unified list of agent- and SSH-managed hosts. Double-click a host for a real PTY terminal (multiple sessions per
host, opened as your administrator user, with file upload/download, find, save-output, and Send-sudo-password). The
right side has <b>Fleet Actions</b> (run a script, restart the agent, reboot, power off — across the fleet, confirmed),
file transfer, SSH-enroll, and <b>RDP to a Windows host</b>. <b>Check In / Ping</b> probes the checked hosts (or one, by
clicking its status dot) and shows the reachable/unreachable results in a popup. Right-click a host to assign an
environment.</p>

<h2 id="activity">16. Activity &amp; logs</h2>
<p>The <b>Activity feed</b> attributes every action to the administrator who ran it, where, and when (visible to
superusers and auditors). The <b>Controller log</b> tab (superuser only) tails the backend service journal.</p>

<h2 id="settings">17. Settings</h2>
<ul>
<li><b>My Account</b> — change your own username/password.</li>
<li><b>Administrators</b> (superuser) — add accounts with a role, reset passwords, promote/demote.</li>
<li><b>TLS certificate</b> (superuser) — install a real cert/key/chain for the controller.</li>
<li><b>Webserver Portal</b> (superuser) — run and credential the host-operator portal.</li>
<li><b>Software updates</b> (superuser) — update the controller and agents together with live output, and download the update log.</li>
</ul>

<h2 id="portal">18. Webserver Portal</h2>
<p>An optional, separate self-service site for <b>host operators</b> (not Sysible administrators) to download the agent
bundle or exchange files with the controller from a browser — no shell access needed. Superusers start it, set its port,
and manage its credentials and sessions from Settings.</p>

<h2 id="cli">19. CLI reference</h2>
<table>
<tr><th>Command</th><th>Root?</th><th>What it does</th></tr>
<tr><td><code>start</code></td><td>Yes</td><td>Start the backend service.</td></tr>
<tr><td><code>stop</code> / <code>restart</code></td><td>Yes</td><td>Stop / restart the backend (and web console).</td></tr>
<tr><td><code>status</code> / <code>logs</code></td><td>No</td><td>Backend + web console status; tail the backend log.</td></tr>
<tr><td><code>webgui {{start|stop|restart|status|logs}}</code></td><td>Yes*</td><td>Control the web console service; builds the front end on start.</td></tr>
<tr><td><code>update</code></td><td>Yes</td><td>Pull latest code, redeploy, and restart in place.</td></tr>
<tr><td><code>reset-admin [user] [pass]</code></td><td>Yes</td><td>Set/create a web-console admin password (printed once).</td></tr>
<tr><td><code>destroy</code></td><td>Yes</td><td>Remove the deployment.</td></tr>
</table>

<h2 id="security">20. Security model</h2>
<ul>
<li><b>Roles enforced server-side</b> — the controller refuses privileged actions for an auditor token; the web BFF blocks every write route for auditors. Portal/TLS are superuser-only at both layers.</li>
<li><b>Run-as identity</b> from the signed login token (never the browser), so each host's sudo policy and audit trail stay meaningful.</li>
<li><b>HTTPS everywhere</b>, TOFU SSH host-key pinning, and secrets (sudo/SMTP passwords) encrypted at rest.</li>
<li><b>Session cookie</b> — signed, http-only, SameSite=Strict; the controller API key never reaches the browser.</li>
</ul>
"""
    toc_page = page('<h2>Contents</h2><div class="toc">' + toc + '</div>')
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<title>Sysible Controller — Administrator &amp; User Guide</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<style>{CSS}</style></head><body>'
            f'{cover}{toc_page}{page(body)}'
            f'<footer>Sysible Controller v{version} — generated {today} by tools/generate_docs.py</footer>'
            f'</body></html>')


def build_quickstart(version, today):
    body = """
<h2>Quick start</h2>
<ol>
<li><b>Install.</b> On the controller, run <code>sudo ./install_sysible.sh</code>. Copy the one-time
<code>admin</code> password printed in red at the end.</li>
<li><b>Sign in.</b> Browse to <code>https://&lt;controller&gt;:8800/</code>, log in as <code>admin</code>, and change your
password under <b>Settings → My Account</b>.</li>
<li><b>Enroll a host.</b> Go to <b>Host Enrollment</b> — install the agent bundle on the host, or SSH-enroll it with a
one-time password.</li>
<li><b>Do something.</b> Open a <b>System Administration</b> tool, check your hosts on the left, fill the fields, and run
an action — results come back per host, grouped by environment.</li>
<li><b>Patch the fleet.</b> Open <b>Update Hosts</b>, review pending/security updates, select hosts, and install — with
live per-host progress.</li>
<li><b>Automate.</b> Add a <b>Schedule</b> (e.g. weekly security updates) and set up <b>Alerts</b> (email/webhook) so you
hear about problems before your users do.</li>
</ol>
<div class="note">Full detail for every screen is in the <a href="Sysible_Controller_Documentation.html">Administrator &amp; User Guide</a>.</div>
"""
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<title>Sysible Controller — Quick Start</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<style>{CSS}</style></head><body>'
            f'<div class="cover"><h1>Sysible Controller</h1><div class="sub">Quick Start</div>'
            f'<div class="meta">Version {version} · {today}</div></div>'
            f'{page(body)}'
            f'<footer>Sysible Controller v{version} — generated {today}</footer>'
            f'</body></html>')


def main():
    version = read_version()
    tools = read_tools()
    today = datetime.date.today().isoformat()
    (ROOT / "Sysible_Controller_Documentation.html").write_text(build_full(version, tools, today))
    (ROOT / "Sysible_Controller_Quickstart.html").write_text(build_quickstart(version, today))
    print(f"Generated guides for v{version} with {len(tools)} tools.")


if __name__ == "__main__":
    main()
