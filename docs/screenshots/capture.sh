#!/usr/bin/env bash
# Sysible docs screenshot capture helper. Run on the controller (Linux) with the
# desktop GUI open. Captures the focused window straight to the right filename.
#   ./capture.sh --list                       list all targets + what to show
#   ./capture.sh screenshot_user_group.png    capture one (focus its window first)
#   ./capture.sh --all                        walk through them all, in order
#   ./capture.sh --region <name|--all>        drag-select an area instead of a window
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DELAY=4          # seconds to focus the target window before the grab
MODE=window

# --- capture targets: name|WxH|description -----------------------------------
read -r -d '' DATA <<'DATA_EOF' || true
screenshot_backup.png|1347x857|Backup & Recovery — back up/restore/verify files, LVM snapshots, scheduled backups, and a disaster-recovery drill.
screenshot_boot.png|2000x818|System Boot & Recovery — analyze boot failures, manage GRUB, set recovery targets, kernel parameters, initramfs, and old kernels.
screenshot_certs.png|1347x865|Certificate Management — generate CSRs, install/renew/replace certs, check expiry, and verify chains / troubleshoot TLS.
screenshot_containers.png|1351x858|Containers & VMs — list and control Docker/Podman containers and images, and manage libvirt virtual machines.
screenshot_cron_timers.png|1346x860|Cron & Systemd Timers — list/add/remove cron jobs and create, start/stop, enable/disable, and delete systemd timers.
screenshot_dashboard.png|1058x1013|The Dashboard — a search box over a single column of tiles, each opening its own popout window.
screenshot_directory.png|1353x863|Directory Services — install AD/LDAP dependencies, join Active Directory, manage realm status and logins, and configure LDAP/LDAPS and Kerberos.
screenshot_environmental_policies.png|1155x907|Environmental Policies — set the password, lockout, sudo, and umask baseline, then push selected policies to checked hosts.
screenshot_filesystem.png|1401x904|File System Management — List/Create/Remove Directory, copy/move/rename, and tabs for permissions, mounts, NFS/CIFS, resize, fstab, quotas, and archives.
screenshot_firewall.png|1348x856|Firewall Administration — firewalld status, ports, zones, and rich rules, plus raw nftables and iptables tabs.
screenshot_health_logs.png|1309x860|System Health, Logs & Recovery — health/logs/process tools grouped into tabs (Overview & Disk, Processes, Logs, Diagnostics, Support & Reports), with a green/amber/red verdict per host.
screenshot_host_enrollment.png|1147x1039|Host Enrollment — download the agent bundle, set a default environment, and manage enrolled hosts.
screenshot_host_software.png|1348x853|Host Software Management — detect the package manager, then install, remove, update, query, verify, and clean packages across apt/dnf/yum/zypper.
screenshot_live_activity.png|1036x1055|Live Activity & Logs — a feed of fleet actions and a tail of the controller log, both auto-refreshing.
screenshot_network.png|1377x887|Network Management — ping/traceroute/DNS diagnostics and port inspection (Diagnostics tab), plus addressing, DNS, routing, and layer-2 tabs.
screenshot_portal.png|1720x1392|Webserver Portal Configuration — start/stop the portal, set its port and credentials, review login history and sessions, and manage the shared file pool.
screenshot_repository.png|1253x806|Repository Management — list, add, enable, disable, and remove repositories on one host or roll a new repo out to the whole fleet.
screenshot_security.png|1480x1042|Security Administration — SELinux, SSH hardening, audit & logins, updates & policy, and hardening & scans tabs.
screenshot_service.png|1347x864|Service Management — start/stop/restart, enable/disable, status, logs, and troubleshooting for systemd services across checked hosts.
screenshot_settings.png|1178x1249|Sysible Controller Settings — controller configuration, administrators, password policy, audit log, and license/version, all in one place.
screenshot_storage.png|1393x899|Storage Administration — disks and SMART, partitions, formatting, LVM, RAID, and swap, organized into tabs.
screenshot_subscription.png|1349x861|Distro Subscription & Licensing — Overview, Red Hat (RHSM), Ubuntu Pro, and SUSE (SCC) tabs for registering and managing commercial-distro subscriptions across checked hosts.
screenshot_sudo_password.png|455x428|My Sudo Password — store or clear your encrypted sudo password as a fleet default or per host.
screenshot_sysible_connect.png|1298x797|Sysible Connect — managed hosts with IPs, per-host file transfer, one-click SSH enrollment, and a Run-Script-on-All-Hosts launcher.
screenshot_sysible_connect_terminal.png|1520x600|A pop-out terminal — green user@host prompt (red for root), with upload/download, find, save-output, and font controls.
screenshot_system_administration.png|1760x2458|The System Administration launcher — a three-column grid of eighteen color-coded tool tiles, each opening its own focused window.
screenshot_timesync.png|1348x859|Time Synchronization — chrony/NTP status and configuration, drift troubleshooting, and time-zone management.
screenshot_user_group.png|1285x800|User & Group Administration — host checklist on the left, the tabbed account panel (Create User, Account, Password, Groups, Reports), and per-host result tabs.
DATA_EOF

# Windows-only shots (capture on the Windows side with the Snipping Tool):
#   screenshot_remote_gui_x11.png, screenshot_rdp_dialog.png (RDP dialog is also
#   reachable in the Linux GUI: Sysible Connect -> RDP To A Windows Host...).

desc_of(){ awk -F'|' -v n="$1" '$1==n{print $3}' <<<"$DATA"; }
size_of(){ awk -F'|' -v n="$1" '$1==n{print $2}' <<<"$DATA"; }
all_names(){ awk -F'|' '{print $1}' <<<"$DATA"; }

detect_tool(){
  for t in gnome-screenshot spectacle scrot maim import; do
    command -v "$t" >/dev/null 2>&1 && { echo "$t"; return; }
  done
  echo ""; 
}
TOOL="$(detect_tool)"

grab(){ # $1=outfile
  local out="$1"
  case "$TOOL" in
    gnome-screenshot) [ "$MODE" = region ] && gnome-screenshot -a -f "$out" || gnome-screenshot -w -f "$out" ;;
    spectacle)        [ "$MODE" = region ] && spectacle -r -b -n -o "$out" || spectacle -a -b -n -o "$out" ;;
    scrot)            [ "$MODE" = region ] && scrot -s "$out" || scrot -u "$out" ;;
    maim)             [ "$MODE" = region ] && maim -s "$out" || { command -v xdotool >/dev/null && maim -i "$(xdotool getactivewindow)" "$out" || maim -s "$out"; } ;;
    import)           [ "$MODE" = region ] && import "$out" || { command -v xdotool >/dev/null && import -window "$(xdotool getactivewindow)" "$out" || import "$out"; } ;;
    *) echo "No screenshot tool found. Install one: gnome-screenshot, spectacle, scrot, maim, or imagemagick." >&2; exit 1 ;;
  esac
}

dims(){ command -v identify >/dev/null 2>&1 && identify -format '%wx%h' "$1" 2>/dev/null || echo "?"; }

capture_one(){
  local name="$1"
  local d; d="$(desc_of "$name")"
  [ -z "$d" ] && { echo "Unknown target: $name  (try --list)"; return 1; }
  echo
  echo "=> $name   (target ~$(size_of "$name"))"
  echo "   SHOW: $d"
  if [ "$MODE" = region ]; then
    echo "   Get the window ready; you'll drag-select the area now."
  else
    echo "   Focus/click the target window now; capturing in ${DELAY}s..."
    for i in $(seq "$DELAY" -1 1); do printf "   %s\r" "$i"; sleep 1; done
  fi
  grab "$DIR/$name"
  echo "   saved $name  (got $(dims "$DIR/$name"))"
}

# --- args --------------------------------------------------------------------
[ "${1:-}" = "--region" ] && { MODE=region; shift; }
case "${1:-}" in
  --list|"")
    echo "Screenshot targets (tool detected: ${TOOL:-none}):"
    while IFS='|' read -r n s d; do printf "  %-42s %-10s %s\n" "$n" "$s" "$d"; done <<<"$DATA"
    echo
    echo "Windows-only (capture on Windows): screenshot_remote_gui_x11.png, screenshot_rdp_dialog.png"
    ;;
  --all)
    for n in $(all_names); do
      read -r -p "Next: $n  [Enter=capture, s=skip, q=quit] " a </dev/tty || a=q
      case "$a" in s) continue;; q) break;; *) capture_one "$n";; esac
    done
    ;;
  *) capture_one "$1" ;;
esac
