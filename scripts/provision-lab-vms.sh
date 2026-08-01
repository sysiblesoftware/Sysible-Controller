#!/usr/bin/env bash
#
# provision-lab-vms.sh — create the 9 Sysible lab VMs on a KVM/libvirt host
# (e.g. sysible-virt) in one shot, from current cloud images, via virt-install.
#
#   3x openSUSE   opensuse-01 opensuse-02 opensuse-03
#   3x Rocky      rocky-01    rocky-02    rocky-03
#   3x Ubuntu     ubuntu-01   ubuntu-02   ubuntu-03
#
# Each VM boots a current cloud image, is seeded with cloud-init (hostname, a
# login user with your SSH key + a password, qemu-guest-agent, grown root disk),
# is attached to the libvirt "default" NAT network, and has a serial console so
# `virsh console <name>` works. Runs against qemu:///system.
#
# Usage (on the hypervisor, as root):
#   sudo ./provision-lab-vms.sh                 # create all 9 (skips existing)
#   sudo ./provision-lab-vms.sh --recreate      # destroy+undefine existing first
#   sudo ./provision-lab-vms.sh --only rocky-01,ubuntu-02
#   sudo ./provision-lab-vms.sh --dry-run
#
# Override anything via env, e.g.:
#   VM_VCPUS=4 VM_RAM_MB=4096 VM_DISK_GB=40 SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" \
#     LOGIN_USER=admin LOGIN_PASSWORD='changeme' NETWORK=default sudo -E ./provision-lab-vms.sh
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Config (override via env). URLs point at the CURRENT stable release of each OS
# — verify/override them if a newer point release has shipped.
# --------------------------------------------------------------------------- #
: "${IMG_UBUNTU_URL:=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"       # Ubuntu 24.04 LTS
: "${IMG_ROCKY_URL:=https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2}" # Rocky 9 (latest 9.x)
: "${IMG_SUSE_URL:=https://download.opensuse.org/repositories/Cloud:/Images:/Leap_15.6/images/openSUSE-Leap-15.6.x86_64-NoCloud.qcow2}" # openSUSE Leap 15.6

: "${VM_VCPUS:=2}"
: "${VM_RAM_MB:=2048}"
: "${VM_DISK_GB:=20}"
: "${NETWORK:=default}"                 # libvirt network name (NAT). See `virsh net-list`.
: "${POOL_DIR:=/var/lib/libvirt/images}" # where per-VM disks live
: "${WORK_DIR:=/var/lib/libvirt/sysible-lab}" # cache for base images + seeds
: "${LOGIN_USER:=admin}"
: "${LOGIN_PASSWORD:=sysible}"          # console/SSH password — CHANGE THIS for anything real
: "${OS_VARIANT_UBUNTU:=ubuntu24.04}"
: "${OS_VARIANT_ROCKY:=rocky9}"
: "${OS_VARIANT_SUSE:=opensuseleap15.6}"

# host:os for each of the 9 VMs.
LAB_VMS="opensuse-01:suse opensuse-02:suse opensuse-03:suse \
rocky-01:rocky rocky-02:rocky rocky-03:rocky \
ubuntu-01:ubuntu ubuntu-02:ubuntu ubuntu-03:ubuntu"

RECREATE=0; DRY_RUN=0; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --recreate) RECREATE=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    --only)     ONLY="$2"; shift ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '\033[1;36m[lab]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[lab] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "  + $*"; else "$@"; fi; }

# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
[ "$(id -u)" = 0 ] || die "run as root (needs qemu:///system + $POOL_DIR)."
for bin in virsh virt-install qemu-img; do
  command -v "$bin" >/dev/null 2>&1 || die "missing '$bin'. Install: libvirt-clients, virt-install, qemu-utils (Debian/Ubuntu) or libvirt-client, virt-install, qemu-img (RHEL/SUSE)."
done
DL=""; command -v curl >/dev/null 2>&1 && DL="curl -fL -o"; [ -z "$DL" ] && command -v wget >/dev/null 2>&1 && DL="wget -O"
[ -n "$DL" ] || die "need curl or wget to fetch cloud images."
# cloud-init seed builder: prefer cloud-localds, else genisoimage/mkisofs.
SEED_TOOL=""
command -v cloud-localds >/dev/null 2>&1 && SEED_TOOL="cloud-localds"
[ -z "$SEED_TOOL" ] && command -v genisoimage >/dev/null 2>&1 && SEED_TOOL="genisoimage"
[ -z "$SEED_TOOL" ] && command -v mkisofs >/dev/null 2>&1 && SEED_TOOL="mkisofs"
[ -n "$SEED_TOOL" ] || die "need cloud-localds (cloud-image-utils) or genisoimage/mkisofs to build the cloud-init seed."

if ! virsh -c qemu:///system net-info "$NETWORK" >/dev/null 2>&1; then
  die "libvirt network '$NETWORK' not found. Create/start it (e.g. 'virsh net-start default; virsh net-autostart default') or set NETWORK=."
fi

# SSH key: use $SSH_PUBKEY, else the first pubkey we can find for root / the sudo user.
if [ -z "${SSH_PUBKEY:-}" ]; then
  for k in /root/.ssh/*.pub "${SUDO_USER:+/home/$SUDO_USER/.ssh/}"*.pub; do
    [ -r "$k" ] && { SSH_PUBKEY="$(cat "$k")"; break; }
  done
fi
[ -n "${SSH_PUBKEY:-}" ] || log "no SSH public key found — VMs will have password login only (user '$LOGIN_USER')."

mkdir -p "$WORK_DIR" "$POOL_DIR"

# --------------------------------------------------------------------------- #
# Fetch base images once (cached in WORK_DIR)
# --------------------------------------------------------------------------- #
base_for() {
  case "$1" in
    ubuntu) echo "$WORK_DIR/base-ubuntu.qcow2  $IMG_UBUNTU_URL" ;;
    rocky)  echo "$WORK_DIR/base-rocky.qcow2   $IMG_ROCKY_URL" ;;
    suse)   echo "$WORK_DIR/base-suse.qcow2    $IMG_SUSE_URL" ;;
  esac
}
fetch_base() {
  set -- $(base_for "$1"); local dst="$1" url="$2"
  if [ -s "$dst" ]; then log "base image cached: $dst"; return; fi
  log "downloading $1 base image…"
  run $DL "$dst.part" "$url"
  [ "$DRY_RUN" = 1 ] || mv "$dst.part" "$dst"
}

# --------------------------------------------------------------------------- #
# Provision one VM
# --------------------------------------------------------------------------- #
selected() { [ -z "$ONLY" ] && return 0; case ",$ONLY," in *",$1,"*) return 0;; *) return 1;; esac; }

provision() {
  local vm="$1" os="$2"
  selected "$vm" || return 0
  set -- $(base_for "$os"); local base="$1"
  local variant; case "$os" in ubuntu) variant="$OS_VARIANT_UBUNTU";; rocky) variant="$OS_VARIANT_ROCKY";; suse) variant="$OS_VARIANT_SUSE";; esac
  local disk="$POOL_DIR/$vm.qcow2" seed="$WORK_DIR/$vm-seed.iso"
  local dir="$WORK_DIR/$vm"; mkdir -p "$dir"

  if virsh -c qemu:///system dominfo "$vm" >/dev/null 2>&1; then
    if [ "$RECREATE" = 1 ]; then
      log "$vm exists — destroying + undefining (--recreate)"
      run sh -c "virsh -c qemu:///system destroy '$vm' 2>/dev/null; virsh -c qemu:///system undefine '$vm' --remove-all-storage 2>/dev/null || true"
    else
      log "$vm already defined — skipping (use --recreate to rebuild)"; return 0
    fi
  fi

  log "creating $vm ($os, $variant)"
  # Per-VM disk: copy the base and grow it to the target size (independent disks,
  # so removing one VM never affects another).
  run cp --reflink=auto "$base" "$disk"
  run qemu-img resize "$disk" "${VM_DISK_GB}G"

  # cloud-init: hostname, login user (ssh key + password), guest agent, grow root.
  cat > "$dir/meta-data" <<EOF
instance-id: $vm
local-hostname: $vm
EOF
  {
    echo "#cloud-config"
    echo "hostname: $vm"
    echo "preserve_hostname: false"
    echo "ssh_pwauth: true"
    echo "disable_root: false"
    echo "users:"
    echo "  - name: $LOGIN_USER"
    echo "    sudo: ALL=(ALL) NOPASSWD:ALL"
    echo "    groups: [wheel, sudo]"
    echo "    shell: /bin/bash"
    echo "    lock_passwd: false"
    [ -n "${SSH_PUBKEY:-}" ] && { echo "    ssh_authorized_keys:"; echo "      - $SSH_PUBKEY"; }
    echo "chpasswd:"
    echo "  expire: false"
    echo "  list: |"
    echo "    $LOGIN_USER:$LOGIN_PASSWORD"
    echo "packages: [qemu-guest-agent]"
    echo "growpart: {mode: auto, devices: ['/']}"
    echo "runcmd:"
    echo "  - [systemctl, enable, --now, qemu-guest-agent]"
  } > "$dir/user-data"

  if [ "$DRY_RUN" = 1 ]; then echo "  + build seed $seed (cloud-init)"; else
    case "$SEED_TOOL" in
      cloud-localds) cloud-localds "$seed" "$dir/user-data" "$dir/meta-data" ;;
      *) "$SEED_TOOL" -output "$seed" -volid cidata -joliet -rock "$dir/user-data" "$dir/meta-data" >/dev/null 2>&1 ;;
    esac
  fi

  run virt-install --connect qemu:///system --name "$vm" \
    --memory "$VM_RAM_MB" --vcpus "$VM_VCPUS" \
    --disk "path=$disk,format=qcow2,bus=virtio" \
    --disk "path=$seed,device=cdrom" \
    --os-variant "$variant" --import \
    --network "network=$NETWORK,model=virtio" \
    --graphics none --console pty,target_type=serial --noautoconsole
  log "$vm defined and starting."
}

# --------------------------------------------------------------------------- #
# Go
# --------------------------------------------------------------------------- #
log "provisioning lab VMs on $(hostname) (qemu:///system, network=$NETWORK, ${VM_VCPUS}vCPU/${VM_RAM_MB}MB/${VM_DISK_GB}GB each)"
# Which OS families are actually needed given --only?
need_suse=0; need_rocky=0; need_ubuntu=0
for pair in $LAB_VMS; do
  vm="${pair%%:*}" os="${pair##*:}"; selected "$vm" || continue
  case "$os" in suse) need_suse=1;; rocky) need_rocky=1;; ubuntu) need_ubuntu=1;; esac
done
[ "$need_suse" = 1 ]   && fetch_base suse
[ "$need_rocky" = 1 ]  && fetch_base rocky
[ "$need_ubuntu" = 1 ] && fetch_base ubuntu

for pair in $LAB_VMS; do provision "${pair%%:*}" "${pair##*:}"; done

log "done. Check: virsh -c qemu:///system list --all"
log "console into one: virsh -c qemu:///system console <name>   (login: $LOGIN_USER / $LOGIN_PASSWORD)"
log "first boot runs cloud-init (grow disk, guest agent) — give each a minute."
