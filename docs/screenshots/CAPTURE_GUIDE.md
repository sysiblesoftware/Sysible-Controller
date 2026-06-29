# Screenshot capture kit

Regenerating the documentation screenshots after UI changes. There are **30**
images in this folder, referenced from `README.md`,
`Sysible_Controller_Documentation.html`, and `Sysible_Controller_Quickstart.html`.

## How to capture (recommended: the live desktop GUI on a real controller)

The existing shots are of the **PySide6 desktop GUI** driven against a populated
fleet, so the screens show real data. For faithful replacements:

1. On a controller with **a couple of enrolled hosts** (so lists/tools aren't
   empty), start the backend and open the GUI:
   ```bash
   sudo sysible_controller start
   sudo sysible_controller gui
   ```
   For tools that act on hosts, **check one or two hosts** first so the panels
   show representative output.
2. Use the helper script in this folder to capture each window straight to the
   correct filename (it counts down, then grabs the focused window):
   ```bash
   cd docs/screenshots
   ./capture.sh --list           # show every target + what it should show
   ./capture.sh screenshot_user_group.png   # capture one
   ./capture.sh --all            # walk through all of them, in order
   ```
   The script auto-detects `gnome-screenshot` / `spectacle` / `scrot` / `maim` /
   ImageMagick `import`. Pass `--region` to drag-select an area instead of
   grabbing the whole window.
3. Aim for **roughly the original dimensions / aspect ratio** below so the docs'
   layout stays consistent. They don't need to match exactly; keep them PNG.

## Two special cases (cannot be captured from the Linux GUI)

- **`screenshot_remote_gui_x11.png`** — the controller's login window forwarded
  to a **Windows** desktop via PuTTY X11 + VcXsrv. Capture on the Windows side
  with the Snipping Tool.
- **`screenshot_rdp_dialog.png`** — the "RDP To A Windows Host" dialog (this one
  *is* in the Linux GUI: Sysible Connect → RDP To A Windows Host…). Capture the
  dialog window.

## The web console instead?

If you'd rather show the browser console (`sysible_controller webgui start`,
then `https://<controller>:8800/`), capture the browser at a comparable window
size. Note it looks different from the desktop GUI — if you switch, switch
*all* of them for consistency.

**Web-console-only screens worth capturing** (these features live in the browser
console, not the desktop GUI — so they have no shot in the table below yet):

- `screenshot_fleet_health.png` — Dashboard → **Fleet health**: the OK/Warning/
  Critical donut and the **per-environment** cards (expand one to show its hosts).
- `screenshot_performance.png` — the **Performance** view: environment-first
  CPU/memory/disk line charts; capture both the overview (one line per
  environment) and a drilled-in environment (one line per host) if you want both.
  Needs an up-to-date agent on a couple of hosts so the graphs have data.

If you add these, drop them in this folder and tell me — I'll wire them into the
README/HTML at the right spots.

## After capturing

Drop the new PNGs in this folder (same filenames) and tell me — I'll re-verify
every reference resolves, refresh any captions whose tool changed, and rebuild
the two PDFs.

## Targets

| File | Original size | Capture | Should show |
|------|---------------|---------|-------------|
| `screenshot_backup.png` | 1347×857 | tool/popout window | Backup & Recovery — back up/restore/verify files, LVM snapshots, scheduled backups, and a disaster-recovery drill. |
| `screenshot_boot.png` | 2000×818 | tool/popout window | System Boot & Recovery — analyze boot failures, manage GRUB, set recovery targets, kernel parameters, initramfs, and old kernels. |
| `screenshot_certs.png` | 1347×865 | tool/popout window | Certificate Management — generate CSRs, install/renew/replace certs, check expiry, and verify chains / troubleshoot TLS. |
| `screenshot_containers.png` | 1351×858 | tool/popout window | Containers & VMs — list and control Docker/Podman containers and images, and manage libvirt virtual machines. |
| `screenshot_cron_timers.png` | 1346×860 | tool/popout window | Cron & Systemd Timers — list/add/remove cron jobs and create, start/stop, enable/disable, and delete systemd timers. |
| `screenshot_dashboard.png` | 1058×1013 | tool/popout window | The Dashboard — a search box over a single column of tiles, each opening its own popout window. |
| `screenshot_directory.png` | 1353×863 | tool/popout window | Directory Services — install AD/LDAP dependencies, join Active Directory, manage realm status and logins, and configure LDAP/LDAPS and Kerberos. |
| `screenshot_environmental_policies.png` | 1155×907 | tool/popout window | Environmental Policies — set the password, lockout, sudo, and umask baseline, then push selected policies to checked hosts. |
| `screenshot_filesystem.png` | 1401×904 | tool/popout window | File System Management — List/Create/Remove Directory, copy/move/rename, and tabs for permissions, mounts, NFS/CIFS, resize, fstab, quotas, and archives. |
| `screenshot_firewall.png` | 1348×856 | tool/popout window | Firewall Administration — firewalld status, ports, zones, and rich rules, plus raw nftables and iptables tabs. |
| `screenshot_health_logs.png` | 1309×860 | tool/popout window | System Health, Logs & Recovery — health/logs/process tools grouped into tabs (Overview & Disk, Processes, Logs, Diagnostics, Support & Reports), with a green/amber/red verdict per host. |
| `screenshot_host_enrollment.png` | 1147×1039 | tool/popout window | Host Enrollment — download the agent bundle, set a default environment, and manage enrolled hosts. |
| `screenshot_host_software.png` | 1348×853 | tool/popout window | Host Software Management — detect the package manager, then install, remove, update, query, verify, and clean packages across apt/dnf/yum/zypper. |
| `screenshot_live_activity.png` | 1036×1055 | tool/popout window | Live Activity & Logs — a feed of fleet actions and a tail of the controller log, both auto-refreshing. |
| `screenshot_network.png` | 1377×887 | tool/popout window | Network Management — ping/traceroute/DNS diagnostics and port inspection (Diagnostics tab), plus addressing, DNS, routing, and layer-2 tabs. |
| `screenshot_portal.png` | 1720×1392 | tool/popout window | Webserver Portal Configuration — start/stop the portal, set its port and credentials, review login history and sessions, and manage the shared file pool. |
| `screenshot_rdp_dialog.png` | 1303×794 | Windows host (Snipping Tool) | RDP dialog — host, username, password, and resolution, with an option to remember credentials encrypted. |
| `screenshot_remote_gui_x11.png` | 1790×1019 | Windows host (Snipping Tool) | The Sysible Controller administrator login window rendered on a Windows desktop, forwarded from the controller over SSH with PuTTY's X11 forwarding and a local X server (VcXsrv). |
| `screenshot_repository.png` | 1253×806 | tool/popout window | Repository Management — list, add, enable, disable, and remove repositories on one host or roll a new repo out to the whole fleet. |
| `screenshot_security.png` | 1480×1042 | tool/popout window | Security Administration — SELinux, SSH hardening, audit & logins, updates & policy, and hardening & scans tabs. |
| `screenshot_service.png` | 1347×864 | tool/popout window | Service Management — start/stop/restart, enable/disable, status, logs, and troubleshooting for systemd services across checked hosts. |
| `screenshot_settings.png` | 1178×1249 | tool/popout window | Sysible Controller Settings — controller configuration, administrators, password policy, audit log, and license/version, all in one place. |
| `screenshot_storage.png` | 1393×899 | tool/popout window | Storage Administration — disks and SMART, partitions, formatting, LVM, RAID, and swap, organized into tabs. |
| `screenshot_subscription.png` | 1349×861 | tool/popout window | Distro Subscription & Licensing — Overview, Red Hat (RHSM), Ubuntu Pro, and SUSE (SCC) tabs for registering and managing commercial-distro subscriptions across checked hosts. |
| `screenshot_sudo_password.png` | 455×428 | dialog window | My Sudo Password — store or clear your encrypted sudo password as a fleet default or per host. |
| `screenshot_sysible_connect.png` | 1298×797 | tool/popout window | Sysible Connect — managed hosts with IPs, per-host file transfer, one-click SSH enrollment, and a Run-Script-on-All-Hosts launcher. |
| `screenshot_sysible_connect_terminal.png` | 1520×600 | tool/popout window | A pop-out terminal — green user@host prompt (red for root), with upload/download, find, save-output, and font controls. |
| `screenshot_system_administration.png` | 1760×2458 | tool/popout window | The System Administration launcher — a three-column grid of eighteen color-coded tool tiles, each opening its own focused window. |
| `screenshot_timesync.png` | 1348×859 | tool/popout window | Time Synchronization — chrony/NTP status and configuration, drift troubleshooting, and time-zone management. |
| `screenshot_user_group.png` | 1285×800 | tool/popout window | User & Group Administration — host checklist on the left, the tabbed account panel (Create User, Account, Password, Groups, Reports), and per-host result tabs. |
