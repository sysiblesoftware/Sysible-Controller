#!/usr/bin/env bash
# Set up a Linux host as a Sysible Relay bastion (jump box) -- guided.
#
# The Sysible Relay is NOT an agent -- nothing from Sysible runs here. The bastion
# is a pure SSH jump host: the Controller opens an SSH ProxyJump to it and it
# forwards that TCP connection to an internal target. This script creates a
# dedicated, shell-less, key-only account restricted to forwarding.
#
# Run on the BASTION. With no arguments it is interactive: it asks for the Sysible
# Controller address and a one-time relay enrollment token (console: Host Enrollment ->
# Sysible Relay -> "Enroll a bastion"), then:
#   * BOOTSTRAP (bastion can reach the Controller): fetches the relay PUBLIC key
#     over HTTPS, registers this bastion into the console "Relay hosts" panel.
#   * OFFLINE (segmented): if the Controller can't be reached, paste the key.
#
# Usage:
#   sudo ./install-linux-bastion.sh                       # fully interactive
#   sudo ./install-linux-bastion.sh --controller https://192.168.1.10:9000 \
#        --token 8f3c...e1 --permitopen 192.168.1.50:22,192.168.1.51:22
#   sudo ./install-linux-bastion.sh --pubkey-file /tmp/relay_ed25519.pub \
#        --permitopen 192.168.1.50:22        # offline
#
# Options:
#   --controller <url>     Controller base URL/host (https, :9000 default)
#   --token <tok>          One-time relay enrollment token (blank -> offline)
#   --controller-cert <p>  Pin the Controller TLS cert (server.crt) for bootstrap
#   --pubkey <line>        Offline: Controller PUBLIC key line
#   --pubkey-file <path>   Offline: file with the Controller PUBLIC key line
#   --permitopen <list>    Comma-separated host:port targets, or 'any'
#   --user <name>          Relay account (default: sysible-relay)
#   --port <n>             SSH port (default: 22)
set -euo pipefail

USER_NAME="sysible-relay"; PORT="22"
CONTROLLER=""; TOKEN=""; CONTROLLER_CERT=""
PUBKEY=""; PUBKEY_FILE=""; PERMITOPEN=""
TOKEN_SET=0

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }
ok()   { echo "    [ok] $*"; }
warn() { echo "    [!]  $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --controller)       CONTROLLER="${2:-}"; shift 2 ;;
    --token)            TOKEN="${2:-}"; TOKEN_SET=1; shift 2 ;;
    --controller-cert)  CONTROLLER_CERT="${2:-}"; shift 2 ;;
    --pubkey)           PUBKEY="${2:-}"; shift 2 ;;
    --pubkey-file)      PUBKEY_FILE="${2:-}"; shift 2 ;;
    --permitopen)       PERMITOPEN="${2:-}"; shift 2 ;;
    --user)             USER_NAME="${2:-}"; shift 2 ;;
    --port)             PORT="${2:-}"; shift 2 ;;
    -h|--help)          grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                  die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)."
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid --user '$USER_NAME'."
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || die "invalid --port '$PORT'."

validate_pubkey() {
  local k="$1"
  # Reject anything with a newline FIRST: a glob like `ssh-ed25519 *` matches across
  # newlines, so a two-line value would otherwise pass and write a SECOND authorized
  # key (a malicious/MITM'd controller could smuggle an unrestricted key this way).
  case "$k" in
    *$'\n'*|*$'\r'*) die "public key must be a single line (multi-line input rejected)." ;;
  esac
  case "$k" in
    ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-*@openssh.com\ *) : ;;
    *) die "that does not look like an OpenSSH public key line." ;;
  esac
}

# Temp files created below (captured controller cert, sshd -t output) are cleaned on exit.
TMPFILES=()
cleanup_tmp() { [ "${#TMPFILES[@]}" -gt 0 ] && rm -f "${TMPFILES[@]}" 2>/dev/null || true; }
trap cleanup_tmp EXIT

resolve_controller_url() {
  local raw; raw="$(echo "$1" | xargs)"
  [ -z "$raw" ] && { echo ""; return; }
  [[ "$raw" =~ ^https?:// ]] || raw="https://$raw"
  # add default :9000 if no port present in the authority
  if ! echo "$raw" | sed -E 's#^https?://##' | grep -q ':[0-9]'; then
    raw="${raw%/}:9000"
  fi
  echo "${raw%/}"
}

# BOOTSTRAP: POST token -> relay pubkey + suggested permitopen; register bastion.
SUGGESTED=""
BOOT_SRC_IP=""    # the Controller-facing IP the Controller reports it saw us connect from
AGENT_TUNNEL_PUBKEY=""   # dedicated key for agents BEHIND this bastion (authorized alongside the relay key)

# Ordered, de-duplicated candidate base URLs to try: the operator-given one first, then
# the SAME host on the agent-API port (9000, where the bootstrap endpoint lives natively)
# and the console port (8800, via the BFF). A segmented bastion often reaches one but not
# the other, so we try them automatically instead of making the operator guess the port.
_candidate_bases() {
  local base="$1" scheme host authority out cand p
  scheme="$(printf '%s' "$base" | sed -nE 's#^(https?)://.*#\1#p')"; [ -n "$scheme" ] || scheme="https"
  authority="$(printf '%s' "$base" | sed -E 's#^https?://##; s#/.*$##')"
  host="${authority%%:*}"
  out="$base"
  for p in 9000 8800; do
    cand="$scheme://$host:$p"
    case " $out " in *" $cand "*) : ;; *) out="$out $cand" ;; esac
  done
  printf '%s' "$out"
}

bootstrap() {
  local base="$1" token="$2" cert="$3"
  command -v curl >/dev/null 2>&1 || { warn "curl not found; cannot bootstrap."; return 1; }
  local hostname; hostname="$(hostname)"
  # Build the JSON body with proper escaping (python3 is present on any agent-stack
  # host). Without escaping, a token/hostname with a quote/backslash could break the
  # JSON or inject structure into the request.
  local payload
  if command -v python3 >/dev/null 2>&1; then
    payload="$(SR_TOK="$token" SR_HN="$hostname" python3 -c 'import json,os; print(json.dumps({"token":os.environ["SR_TOK"],"hostname":os.environ["SR_HN"],"os":"linux"}))')"
  else
    case "$token$hostname" in
      *[\"\\]*|*$'\n'*|*$'\r'*) die "token/hostname contains characters that need JSON escaping; install python3 to bootstrap." ;;
    esac
    payload="$(printf '{"token":"%s","hostname":"%s","os":"linux"}' "$token" "$hostname")"
  fi
  # Find a REACHABLE Controller port among the candidates (given -> :9000 -> :8800). This
  # is the fix for "could not retrieve the Controller certificate": if the given port is
  # blocked across the bastion's subnet, we automatically try the other ports before
  # giving up, so the operator never has to guess which one is open.
  local capem="" working="" cand cauth tmp
  if [ -n "$cert" ]; then
    [ -f "$cert" ] || die "controller cert not found: $cert"
    capem="$cert"
    for cand in $(_candidate_bases "$base"); do
      cauth="$(printf '%s' "$cand" | sed -E 's#^https?://##')"
      if echo | openssl s_client -connect "$cauth" 2>/dev/null | grep -q CONNECTED; then
        working="$cand"; break
      fi
    done
  else
    # Self-signed LAN cert, TOFU: capture the LEAF cert, show its SHA-256 fingerprint for
    # the operator to compare OUT-OF-BAND, then PIN that exact cert for the real request
    # via --cacert (never --insecure), so a MITM that shows one cert can't answer with
    # another.
    command -v openssl >/dev/null 2>&1 || die "openssl needed for the TOFU cert check; install it or pass --controller-cert."
    for cand in $(_candidate_bases "$base"); do
      cauth="$(printf '%s' "$cand" | sed -E 's#^https?://##')"
      info "Trying Controller at $cand ..."
      tmp="$(mktemp)"; TMPFILES+=("$tmp")
      echo | openssl s_client -connect "$cauth" -servername "${cauth%%:*}" 2>/dev/null \
          | openssl x509 -outform PEM > "$tmp" 2>/dev/null || true
      if [ -s "$tmp" ]; then capem="$tmp"; working="$cand"; break; fi
      warn "no TLS response from $cand -- trying the next port."
    done
  fi
  if [ -z "$working" ]; then
    warn "could not reach the Controller on any of: $(_candidate_bases "$base" | tr ' ' ',') -- this bastion has no network path to the Controller on those ports (check routing/firewall). Falling back to OFFLINE, which needs no path back to the Controller."
    return 1
  fi
  base="$working"
  if [ -z "$cert" ]; then
    local fp; fp="$(openssl x509 -in "$capem" -noout -fingerprint -sha256 2>/dev/null)"
    info "Controller reachable at $base"
    warn "Controller TLS certificate fingerprint:"
    warn "    $fp"
    printf '    Compare this with the Controller (out of band) and trust it? [y/N] ' >&2
    read -r ans </dev/tty || ans=""
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Controller certificate not trusted; aborting bootstrap."
  fi
  # POST the enrollment payload from a 0600 temp file, not inline with -d "$payload":
  # an inline -d exposes the one-time bootstrap token (inside $payload) in `ps` /
  # /proc/<pid>/cmdline to any local user on the bastion for the request's lifetime.
  # (The Windows script already uses --data-binary @file for the same reason.)
  local _bodyf; _bodyf="$(mktemp)" || die "cannot create a temp file for the request body."
  chmod 600 "$_bodyf" 2>/dev/null || true
  printf '%s' "$payload" > "$_bodyf"
  local curl_opts=(--fail --show-error --silent --max-time 20 \
      -H 'Content-Type: application/json' -X POST --data-binary @"$_bodyf" --cacert "$capem")
  info "Contacting Controller at $base/api/relay/bootstrap"
  local resp _rc
  resp="$(curl "${curl_opts[@]}" "$base/api/relay/bootstrap")"; _rc=$?
  rm -f "$_bodyf"
  [ "$_rc" -eq 0 ] || return 1
  # Extract fields with a tiny python (present on any host running the agent stack)
  # falling back to sed. Keep it dependency-light.
  local key user_out targets src tunnel
  if command -v python3 >/dev/null 2>&1; then
    key="$(printf '%s' "$resp"     | python3 -c 'import sys,json; print(json.load(sys.stdin).get("relay_pubkey",""))' 2>/dev/null)"
    user_out="$(printf '%s' "$resp"| python3 -c 'import sys,json; print(json.load(sys.stdin).get("relay_user",""))' 2>/dev/null)"
    targets="$(printf '%s' "$resp" | python3 -c 'import sys,json; print(",".join(json.load(sys.stdin).get("permitopen",[]) or []))' 2>/dev/null)"
    src="$(printf '%s' "$resp"     | python3 -c 'import sys,json; print(json.load(sys.stdin).get("bastion_source_ip",""))' 2>/dev/null)"
    tunnel="$(printf '%s' "$resp"  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("agent_tunnel_pubkey",""))' 2>/dev/null)"
  else
    key="$(printf '%s' "$resp" | sed -n 's/.*"relay_pubkey"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    tunnel="$(printf '%s' "$resp" | sed -n 's/.*"agent_tunnel_pubkey"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  fi
  # The address the Controller saw us connect from = this box's NIC on the Controller's
  # network = where the Controller reaches the jump box back. Only accept a clean IPv4.
  if [ -n "${src:-}" ] && printf '%s' "$src" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    BOOT_SRC_IP="$src"
  fi
  [ -n "$key" ] || { warn "bootstrap response had no relay_pubkey"; return 1; }
  # The response is UNTRUSTED (network + a possibly-MITM'd controller): validate every
  # value with the same strictness as CLI input before it reaches sshd_config/useradd.
  validate_pubkey "$key"
  PUBKEY="$key"
  if [ -n "${user_out:-}" ]; then
    [[ "$user_out" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] \
      || die "controller returned an invalid relay_user: $(printf %q "$user_out")"
    case "$user_out" in root|daemon|bin|sys) die "controller returned a reserved relay_user: $user_out" ;; esac
    USER_NAME="$user_out"
  fi
  [ -n "${targets:-}" ] && SUGGESTED="$targets"
  # Dedicated agent-tunnel key for hosts BEHIND this bastion (optional -- empty if the
  # controller couldn't create it). Validated like any untrusted controller value; a
  # malformed one is dropped with a warning rather than aborting the relay setup.
  if [ -n "${tunnel:-}" ]; then
    case "$tunnel" in
      *$'\n'*|*$'\r'*) warn "ignoring malformed agent_tunnel_pubkey (multi-line)" ;;
      ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-*@openssh.com\ *)
        AGENT_TUNNEL_PUBKEY="$tunnel" ;;
      *) warn "ignoring agent_tunnel_pubkey that isn't an OpenSSH public key line" ;;
    esac
  fi
  return 0
}

# ---- resolve the public key: offline flag > bootstrap > prompt ----
if [ -n "$PUBKEY_FILE" ]; then
  [ -z "$PUBKEY" ] || die "pass only one of --pubkey / --pubkey-file."
  [ -f "$PUBKEY_FILE" ] || die "pubkey file not found: $PUBKEY_FILE"
  PUBKEY="$(grep -vE '^\s*(#|$)' "$PUBKEY_FILE" | head -n1)"
fi

if [ -z "$PUBKEY" ]; then
  if [ -z "$CONTROLLER" ]; then
    printf 'Sysible Controller address (host or URL; blank = offline paste): ' >&2
    read -r CONTROLLER </dev/tty || CONTROLLER=""
  fi
  BASE="$(resolve_controller_url "$CONTROLLER")"
  if [ -n "$BASE" ]; then
    if [ "$TOKEN_SET" -eq 0 ] && [ -z "$TOKEN" ]; then
      printf 'Relay enrollment token (console: Host Enrollment -> Relay Enrollment -> Enroll a bastion; blank = offline): ' >&2
      read -r TOKEN </dev/tty || TOKEN=""
    fi
    if [ -n "$TOKEN" ]; then
      if bootstrap "$BASE" "$TOKEN" "$CONTROLLER_CERT"; then
        ok "Bootstrapped from Controller (relay key retrieved; bastion registered)"
      else
        warn "Bootstrap failed; falling back to OFFLINE mode."
      fi
    fi
  fi
fi

if [ -z "$PUBKEY" ]; then
  echo "OFFLINE mode. On the Controller run:  sudo cat /opt/sysible/relay_keys/relay_ed25519.pub" >&2
  printf 'Relay PUBLIC key: ' >&2
  read -r PUBKEY </dev/tty || PUBKEY=""
fi
[ -n "$PUBKEY" ] || die "no public key provided."
validate_pubkey "$PUBKEY"

# ---- normalise PermitOpen (suggested + explicit) ----
merge_targets="$SUGGESTED"
[ -n "$PERMITOPEN" ] && merge_targets="${merge_targets:+$merge_targets,}$PERMITOPEN"
if [ -z "$merge_targets" ]; then
  {
    printf '\n'
    printf 'Which internal targets may this relay FORWARD to?\n'
    printf '  Enter one or more IP-or-hostname:port entries (NOT a URL), comma/space-separated.\n'
    printf '  This is the sshd PermitOpen allowlist -- the exact host:port pairs the Controller\n'
    printf '  may tunnel THROUGH this jump box to. Examples:\n'
    printf '      192.168.1.29:9000                 the Controller (agent egress back to it)\n'
    printf '      192.168.8.50:22, 192.168.8.51:22  specific hosts, SSH\n'
    printf '  Note: OpenSSH PermitOpen does NOT accept CIDR/subnets -- list each host:port.\n'
    printf '  For MANY subnets, list a target per host, or type "any" (allows ALL host:port\n'
    printf '  -- an unrestricted pivot, not recommended).\n'
    printf 'Targets (host:port[,host:port...] or "any"): '
  } >&2
  read -r merge_targets </dev/tty || merge_targets=""
fi
PERMIT_LINE=""
if [ -z "$merge_targets" ]; then
  # Fail closed: a bastion that forwards to ANYTHING is an unrestricted pivot. Require
  # an explicit target list, or a deliberate "any".
  die "no forwarding targets given. Re-run with --permitopen host:port[,host:port...] so the relay account can only reach those internal targets. To deliberately allow all (not recommended), pass --permitopen any."
elif [ "$merge_targets" = "any" ]; then
  warn "PermitOpen=any: the relay account may forward to ANY reachable host:port -- an unrestricted pivot into the internal network. Not recommended."
  # If this 'any' came from the bootstrap RESPONSE (SUGGESTED by the controller) rather
  # than an operator --permitopen argument, require an explicit interactive confirmation:
  # a MITM'd or compromised controller must not silently obtain an unrestricted pivot.
  if [ "$SUGGESTED" = "any" ] && [ -z "$PERMITOPEN" ]; then
    warn "This unrestricted 'any' was SUGGESTED BY THE CONTROLLER during bootstrap, not passed by you."
    printf '    Allow the Controller to open an UNRESTRICTED pivot (PermitOpen any)? [y/N] ' >&2
    read -r ans </dev/tty || ans=""
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || die "refused controller-suggested 'PermitOpen any'. Re-run with --permitopen host:port[,host:port...] to restrict the relay account."
  fi
  PERMIT_LINE="PermitOpen any"
else
  seen=""
  IFS=',' read -ra _t <<< "$merge_targets"
  for t in "${_t[@]}"; do
    t="$(echo "$t" | xargs)"; [ -z "$t" ] && continue
    [[ "$t" =~ ^[A-Za-z0-9._-]+:[0-9]{1,5}$ ]] || die "invalid target '$t' (want host:port)."
    # Bound the port to 1..65535 (the [0-9]{1,5} regex alone would accept 99999);
    # matches the Windows script's Test-PermitOpenTarget so a bad port fails here
    # rather than only later at `sshd -t`.
    _port="${t##*:}"
    { [ "$_port" -ge 1 ] && [ "$_port" -le 65535 ]; } 2>/dev/null \
        || die "invalid target '$t' (port out of range 1-65535)."
    case " $seen " in *" $t "*) continue ;; esac
    seen="$seen $t"
  done
  PERMIT_LINE="PermitOpen$seen"
fi

echo
echo "Sysible Relay -- Linux bastion setup"
echo "  account   : $USER_NAME (non-login, key-only, forwarding-only)"
echo "  ssh port  : $PORT"
echo "  permitopen: ${PERMIT_LINE#PermitOpen }"
echo

info "Checking OpenSSH server"
command -v sshd >/dev/null 2>&1 || command -v /usr/sbin/sshd >/dev/null 2>&1 \
  || die "sshd not found. Install openssh-server first."
ok "sshd present"

info "Ensuring least-privilege account '$USER_NAME'"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin \
    --comment "Sysible Relay (forwarding only)" "$USER_NAME"
  ok "created user '$USER_NAME'"
else
  ok "user '$USER_NAME' already exists"
fi
passwd -l "$USER_NAME" >/dev/null 2>&1 || true
ok "'$USER_NAME' is key-only (password locked, nologin shell)"

# Dedicated group so a global AllowGroups (if the box gates SSH that way) can permit
# EXACTLY this account. AllowGroups/AllowUsers are evaluated BEFORE any Match block, so
# without this a hardened sshd rejects the relay account pre-auth.
RELAY_GROUP="sysible-relay"
if ! getent group "$RELAY_GROUP" >/dev/null 2>&1; then
  groupadd --system "$RELAY_GROUP" 2>/dev/null || groupadd "$RELAY_GROUP" 2>/dev/null || true
fi
if getent group "$RELAY_GROUP" >/dev/null 2>&1; then
  usermod -aG "$RELAY_GROUP" "$USER_NAME" 2>/dev/null || true
  ok "'$USER_NAME' is a member of group '$RELAY_GROUP'"
fi

HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[ -n "$HOME_DIR" ] || die "could not resolve home dir for '$USER_NAME'."
info "Installing Controller public key -> $HOME_DIR/.ssh/authorized_keys"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh"
# Authorize the relay key (controller-initiated ProxyJump) and, when the controller
# supplied it, the dedicated agent-tunnel key (agents BEHIND this bastion open their
# egress tunnel with THAT key, not the relay key). Overwrite -- never append -- so a
# re-run replaces both cleanly with no duplicates and a rotated key takes effect.
{ printf '%s\n' "$PUBKEY"
  [ -n "$AGENT_TUNNEL_PUBKEY" ] && printf '%s\n' "$AGENT_TUNNEL_PUBKEY"
} > "$HOME_DIR/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
if [ -n "$AGENT_TUNNEL_PUBKEY" ]; then
  ok "installed relay + agent-tunnel keys (mode 600, owned by $USER_NAME)"
else
  ok "installed key (mode 600, owned by $USER_NAME)"
fi

CFG="/etc/ssh/sshd_config"
# Fixed, username-INDEPENDENT markers so a re-run always strips the prior managed block
# even if the relay user changed between runs (otherwise a stale Match block for the old
# username would be left behind -- e.g. re-enrolling with a different --user).
BEGIN="# >>> sysible-relay managed block -- do not edit by hand >>>"
END="# <<< sysible-relay managed block <<<"
info "Hardening $CFG for '$USER_NAME' (forwarding-only)"
if [ -f "$CFG" ]; then
  cp -a "$CFG" "${CFG}.sysible.bak"
  # Delete any prior managed block from BEGIN..END. The pattern deliberately matches both
  # the current marker and the LEGACY form that embedded the username
  # ("# >>> sysible-relay (someuser) managed block ..."), so a box enrolled by an older
  # version migrates cleanly instead of retaining a second, stale Match block.
  sed -i '/^# >>> sysible-relay .*managed block.*>>>$/,/^# <<< sysible-relay .*managed block.*<<<$/d' "$CFG"
  # If a GLOBAL AllowGroups/AllowUsers gates SSH (evaluated before Match), the relay
  # account is rejected pre-auth unless permitted. Append our group / the user to the
  # FIRST matching directive line (append-only; original backed up above).
  _append_allow() {
    local d="$1" v="$2"
    grep -Eiq "^[[:space:]]*${d}[[:space:]]+" "$CFG" || return 0            # directive absent
    if grep -Ei "^[[:space:]]*${d}[[:space:]]" "$CFG" | grep -qw "$v"; then return 0; fi
    sed -ri "0,/^([[:space:]]*${d}[[:space:]].*)$/ s//\1 ${v}/" "$CFG"
    ok "added '${v}' to the existing sshd '${d}' allow-list"
  }
  _append_allow AllowGroups "$RELAY_GROUP"
  _append_allow AllowUsers "$USER_NAME"
fi
cat >> "$CFG" <<EOF

$BEGIN
Match User $USER_NAME
    PasswordAuthentication no
    PubkeyAuthentication yes
    AllowTcpForwarding yes
    $PERMIT_LINE
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    GatewayPorts no
    ForceCommand /bin/echo 'sysible-relay: this account is for Sysible Controller port-forwarding only. No shell.'
$END
EOF
ok "wrote managed Match block (backup: ${CFG}.sysible.bak)"

info "Validating config and reloading sshd"
# mktemp (not a predictable /tmp/...$$ path) so a local user can't pre-create a
# symlink there and have our root-owned 2> redirect clobber an arbitrary file.
SSHD_TEST_OUT="$(mktemp)"; TMPFILES+=("$SSHD_TEST_OUT")
if sshd -t 2>"$SSHD_TEST_OUT"; then
  if systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null \
     || service ssh reload 2>/dev/null || service sshd reload 2>/dev/null; then
    ok "sshd reloaded with the new configuration"
  else
    warn "config is valid but reloading sshd failed -- reload it yourself (e.g. 'systemctl reload sshd') to apply the hardened block."
  fi
else
  warn "sshd -t failed; restoring backup:"; cat "$SSHD_TEST_OUT" >&2
  [ -f "${CFG}.sysible.bak" ] && cp -a "${CFG}.sysible.bak" "$CFG"
  die "config validation failed; running service left unchanged."
fi

# Two DIFFERENT addresses on a dual-homed jump box: the CONTROLLER-FACING NIC (relay_host,
# where the Controller dials the jump box) vs a PIVOT NIC (agent --bastion). On bootstrap
# the Controller reports the exact address it saw us connect from and auto-sets relay_host.
CTRL_FACING="${BOOT_SRC_IP:-<this box's IP on the Controller's network>}"
echo
echo "Done. This bastion is ready."
echo
if [ -n "$BOOT_SRC_IP" ]; then
  echo "Controller connects to this jump box at $CTRL_FACING (its NIC on the Controller's"
  echo "network). The Controller auto-set relay_host to this on bootstrap -- nothing to do."
else
  echo "If you did NOT bootstrap, set these in the console (Host Enrollment -> Relay Enrollment):"
  echo "    Relay / bastion host : $CTRL_FACING"
  echo "                           ^ this box's IP ON THE CONTROLLER'S NETWORK -- NOT a pivot IP."
  echo "    Relay SSH user       : $USER_NAME"
  echo "    Relay SSH port       : $PORT"
  echo "    Relay identity key   : (the PRIVATE key on the Controller, e.g. /opt/sysible/relay_keys/relay_ed25519)"
  echo "    Relay OS             : auto"
fi
echo
echo "This box's IPv4 addresses (pick the PIVOT-network one for agent --bastion):"
for _ip in $(hostname -I 2>/dev/null); do
  case "$_ip" in
    *:*) continue ;;   # skip IPv6
  esac
  if [ "$_ip" = "$BOOT_SRC_IP" ]; then
    echo "    $_ip  <- Controller-facing (relay_host)"
  else
    echo "    $_ip"
  fi
done
echo
echo "Enroll agents on the hosts BEHIND this bastion, pointing them at the PIVOT NIC:"
echo "    ...enroll... --bastion <this box's IP on the AGENTS' network>"
echo
echo "Verify the full forward path FROM THE CONTROLLER (use the controller-facing IP):"
echo "    sudo ssh -i /opt/sysible/relay_keys/relay_ed25519 -o BatchMode=yes \\"
echo "      -J $USER_NAME@$CTRL_FACING <user>@<internal-host> whoami"
