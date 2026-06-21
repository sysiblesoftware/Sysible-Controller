"""STORAGE ADMINISTRATION dual-host command builders - split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_storage import *` at the bottom of client/api.py.

Covers everything below the filesystem layer that client/_api_filesystem.py
and client/_api_filesystem_mount.py don't: partition tables, raw
mkfs formatting, LVM (physical volumes / volume groups / logical
volumes), software RAID (mdadm), swap, and whole-disk health/
add/remove. Same rules as the rest of this split: plain POSIX sh,
shlex.quote() (or explicit validation) on anything interpolated, a
clear "X is not installed" message instead of a bare command-not-found,
and explicit guardrails before anything destructive or irreversible.
"""
import re
import shlex


def _validate_path(path: str, label: str = "Path") -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError(f"{label} is required.")
    if "\x00" in path:
        raise ValueError(f"{label} contains an invalid character.")
    return path


def _validate_int_range(value, lo: int, hi: int, label: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not (lo <= n <= hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


def _devices_list(text: str, label: str = "Device(s)") -> list:
    paths = (text or "").split()
    if not paths:
        raise ValueError(f"{label} is required (space-separated for more than one).")
    return [_validate_path(p, label) for p in paths]


_SAFE_LVM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


def _validate_lvm_name(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not _SAFE_LVM_NAME_RE.match(name):
        raise ValueError(
            f"{label} may only contain letters, numbers, dots, dashes, "
            "underscores, and plus signs, and must start with a letter or number."
        )
    return name


def _validate_parted_token(value: str, label: str, default: str = None) -> str:
    """parted's start/end/fs-type-hint args are bare tokens like '0%',
    '512GiB', 'ext4' - reject anything that looks like it would split
    into extra shell words instead of trying to fully validate the
    syntax parted itself accepts."""
    value = (value or "").strip()
    if not value:
        if default is not None:
            return default
        raise ValueError(f"{label} is required.")
    if any(c.isspace() for c in value):
        raise ValueError(f"{label} cannot contain spaces.")
    return value


# ---------------------------------------------------------
# Disks overview, health, add/remove
# ---------------------------------------------------------
def cmd_list_disks() -> str:
    return "lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,TRAN,ROTA,FSTYPE,MOUNTPOINT 2>&1"


def cmd_rescan_disks() -> str:
    """Triggers a SCSI bus rescan so a newly attached disk shows up
    without rebooting the host. Covers SCSI/SATA/virtio disks - NVMe
    devices are detected automatically by the kernel and need no
    rescan."""
    return r"""
found=0
for host in /sys/class/scsi_host/host*; do
    [ -w "$host/scan" ] || continue
    echo '- - -' > "$host/scan" 2>/dev/null && found=1
done
if [ "$found" -eq 0 ]; then
    echo "No rescannable SCSI hosts found on this host (NVMe disks need no rescan - the kernel detects them automatically)."
else
    echo "Rescan triggered."
fi
echo
echo "-- Block devices after rescan --"
lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT 2>&1
""".strip()


def cmd_remove_disk(device: str) -> str:
    """Safely offlines a whole disk before physical removal. Refuses
    if the disk (or any partition on it) is mounted, is an active LVM
    physical volume, or is a member of a software RAID array - clear
    each of those first. Once it prints success, the disk is safe to
    detach."""
    device = _validate_path(device, "Device")
    q_dev = shlex.quote(device)
    return rf"""
dev={q_dev}
name=$(basename "$dev")
mounts=$(lsblk -rno MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$')
if [ -n "$mounts" ]; then
    echo "Refusing - $dev (or a partition on it) is still mounted: $mounts" >&2
    exit 1
fi
if command -v pvs >/dev/null 2>&1 && pvs --noheadings -o pv_name 2>/dev/null | grep -qF "$dev"; then
    echo "Refusing - $dev is an active LVM physical volume. Remove it from its volume group first." >&2
    exit 1
fi
if grep -qF "$name" /proc/mdstat 2>/dev/null; then
    echo "Refusing - $dev appears to be a member of a software RAID array. Fail and remove it from the array first." >&2
    exit 1
fi
if [ ! -e "/sys/block/$name/device/delete" ]; then
    echo "The kernel does not expose /sys/block/$name/device/delete for $dev - it may not be hot-removable on this bus." >&2
    exit 1
fi
echo 1 > "/sys/block/$name/device/delete"
echo "Offlined $dev - it is now safe to physically remove."
""".strip()


def cmd_monitor_disk_health() -> str:
    """Combined HEALTH: OK/WARNING/CRITICAL verdict across every whole
    disk on the host, based on each disk's SMART overall-health
    self-assessment - same scoring convention as the System Health &
    Logs page's cmd_health_check()."""
    return r"""
if ! command -v smartctl >/dev/null 2>&1; then
    echo "HEALTH: UNKNOWN"
    echo
    echo "smartctl is not installed on this host (package: smartmontools) - install it to monitor disk health via SMART."
    echo
    echo "-- Block devices (lsblk) --"
    lsblk -o NAME,SIZE,TYPE,MODEL,FSTYPE,MOUNTPOINT 2>&1
    exit 0
fi
overall="OK"
detail=""
for dev in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}'); do
    path="/dev/$dev"
    health=$(smartctl -H "$path" 2>/dev/null | grep -i "overall-health" | awk -F: '{print $2}' | tr -d ' ')
    [ -z "$health" ] && health="UNKNOWN"
    case "$health" in
        PASSED) status=ok ;;
        FAILED*) status=critical ;;
        *) status=warning ;;
    esac
    if [ "$status" = "critical" ]; then
        overall="CRITICAL"
    elif [ "$status" = "warning" ] && [ "$overall" != "CRITICAL" ]; then
        overall="WARNING"
    fi
    detail="$detail$path: $health ($status)
"
done
echo "HEALTH: $overall"
echo
echo "-- Per-disk SMART overall-health --"
printf '%s' "$detail"
echo
echo "-- Block devices (lsblk) --"
lsblk -o NAME,SIZE,TYPE,MODEL,FSTYPE,MOUNTPOINT 2>&1
""".strip()


def cmd_check_smart_status(device: str) -> str:
    """Full SMART attribute report for one disk."""
    device = _validate_path(device, "Device")
    q_dev = shlex.quote(device)
    return (
        "if ! command -v smartctl >/dev/null 2>&1; then "
        "echo 'smartctl is not installed on this host (package: smartmontools).' >&2; exit 1; fi; "
        f"smartctl -a {q_dev} 2>&1"
    )


# ---------------------------------------------------------
# Partitions (parted)
# ---------------------------------------------------------
_VALID_LABEL_TYPES = {"gpt", "msdos"}


def cmd_list_partitions(device: str = "") -> str:
    """Partition table for one device (parted), or an lsblk overview
    of every disk/partition on the host if `device` is left blank."""
    device = (device or "").strip()
    if device:
        q_dev = shlex.quote(_validate_path(device, "Device"))
        return (
            "if ! command -v parted >/dev/null 2>&1; then "
            "echo 'parted is not installed on this host (package: parted).' >&2; exit 1; fi; "
            f"parted -s {q_dev} unit MiB print 2>&1"
        )
    return "lsblk -o NAME,SIZE,TYPE,FSTYPE,PARTLABEL,MOUNTPOINT 2>&1"


def cmd_create_partition_table(device: str, label_type: str = "gpt") -> str:
    """DESTROYS any existing partition table (and the data it
    describes) on `device`. Only run this on a disk you intend to
    wipe and start fresh."""
    device = _validate_path(device, "Device")
    label_type = (label_type or "gpt").strip().lower()
    if label_type not in _VALID_LABEL_TYPES:
        raise ValueError(f"Label type must be one of: {', '.join(sorted(_VALID_LABEL_TYPES))}")
    q_dev = shlex.quote(device)
    return (
        "if ! command -v parted >/dev/null 2>&1; then "
        "echo 'parted is not installed on this host (package: parted).' >&2; exit 1; fi; "
        f"parted -s {q_dev} mklabel {label_type} 2>&1 "
        f"&& echo 'Created a {label_type} partition table on {device}.'"
    )


def cmd_create_partition(device: str, fs_type: str = "ext4", start: str = "0%", end: str = "100%") -> str:
    """Adds one new partition to an existing partition table (create
    one first with Create Partition Table if the disk is blank).
    `start`/`end` accept parted's usual forms - percentages ("0%",
    "100%") or absolute sizes ("1MiB", "512GiB"). `fs_type` here is
    only the name/hint parted stores for the partition - it does not
    format it; use Format Filesystem afterward for that."""
    device = _validate_path(device, "Device")
    fs_type = _validate_parted_token(fs_type, "Filesystem type hint", default="ext4")
    start = _validate_parted_token(start, "Start")
    end = _validate_parted_token(end, "End")
    q_dev = shlex.quote(device)
    return (
        "if ! command -v parted >/dev/null 2>&1; then "
        "echo 'parted is not installed on this host (package: parted).' >&2; exit 1; fi; "
        f"parted -s {q_dev} mkpart primary {shlex.quote(fs_type)} {shlex.quote(start)} {shlex.quote(end)} 2>&1; "
        f"partprobe {q_dev} 2>/dev/null; "
        f"parted -s {q_dev} print 2>&1"
    )


def cmd_delete_partition(device: str, part_number) -> str:
    device = _validate_path(device, "Device")
    part_number = _validate_int_range(part_number, 1, 128, "Partition number")
    q_dev = shlex.quote(device)
    return (
        "if ! command -v parted >/dev/null 2>&1; then "
        "echo 'parted is not installed on this host (package: parted).' >&2; exit 1; fi; "
        f"parted -s {q_dev} rm {part_number} 2>&1; "
        f"partprobe {q_dev} 2>/dev/null; "
        f"echo 'Removed partition {part_number} from {device}.'"
    )


def cmd_resize_partition(device: str, part_number, end: str) -> str:
    """Resizes the *partition table entry* (the partition's
    boundaries) - run Resize Filesystem (File System Management)
    afterward to grow/shrink the filesystem inside it to match."""
    device = _validate_path(device, "Device")
    part_number = _validate_int_range(part_number, 1, 128, "Partition number")
    end = _validate_parted_token(end, "New end")
    q_dev = shlex.quote(device)
    return (
        "if ! command -v parted >/dev/null 2>&1; then "
        "echo 'parted is not installed on this host (package: parted).' >&2; exit 1; fi; "
        f"parted -s {q_dev} resizepart {part_number} {shlex.quote(end)} 2>&1; "
        f"partprobe {q_dev} 2>/dev/null; "
        f"echo 'Resized partition {part_number} on {device} - remember to grow/shrink the filesystem inside it next.'"
    )


# ---------------------------------------------------------
# Format filesystems (mkfs)
# ---------------------------------------------------------
_VALID_MKFS_TYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "ntfs", "swap"}
_MKFS_FORCE_FLAG = {"ext2": "-F", "ext3": "-F", "ext4": "-F", "xfs": "-f", "btrfs": "-f", "ntfs": "-F"}
_MKFS_LABEL_FLAG = {"ext2": "-L", "ext3": "-L", "ext4": "-L", "xfs": "-L", "btrfs": "-L", "vfat": "-n", "ntfs": "-L"}


def cmd_format_filesystem(device: str, fs_type: str, label: str = "", force: bool = True) -> str:
    """Creates a brand-new filesystem on `device`, destroying whatever
    was there before. `fs_type` "swap" delegates to mkswap instead of
    mkfs."""
    device = _validate_path(device, "Device")
    fs_type = (fs_type or "").strip().lower()
    if fs_type not in _VALID_MKFS_TYPES:
        raise ValueError(
            f"Unsupported filesystem type '{fs_type}'. Supported: {', '.join(sorted(_VALID_MKFS_TYPES))}"
        )
    label = (label or "").strip()
    if "\n" in label or "\r" in label:
        raise ValueError("Label cannot contain a newline.")
    q_dev = shlex.quote(device)

    if fs_type == "swap":
        flags = f"-L {shlex.quote(label)} " if label else ""
        return f"mkswap {flags}{q_dev} 2>&1"

    flags = ""
    if force and fs_type in _MKFS_FORCE_FLAG:
        flags += f"{_MKFS_FORCE_FLAG[fs_type]} "
    if label:
        flags += f"{_MKFS_LABEL_FLAG[fs_type]} {shlex.quote(label)} "
    return f"mkfs.{fs_type} {flags}{q_dev} 2>&1"


# ---------------------------------------------------------
# LVM - physical volumes
# ---------------------------------------------------------
def cmd_create_physical_volume(devices: str) -> str:
    dev_list = _devices_list(devices, "Device(s)")
    q_devs = " ".join(shlex.quote(d) for d in dev_list)
    return (
        "if ! command -v pvcreate >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"pvcreate {q_devs} 2>&1"
    )


def cmd_list_physical_volumes() -> str:
    return (
        "if ! command -v pvs >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        "pvs -o pv_name,vg_name,pv_size,pv_free 2>&1"
    )


# ---------------------------------------------------------
# LVM - volume groups
# ---------------------------------------------------------
def cmd_create_volume_group(vg_name: str, devices: str) -> str:
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    dev_list = _devices_list(devices, "Physical volume device(s)")
    q_vg = shlex.quote(vg_name)
    q_devs = " ".join(shlex.quote(d) for d in dev_list)
    return (
        "if ! command -v vgcreate >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"vgcreate {q_vg} {q_devs} 2>&1"
    )


def cmd_list_volume_groups() -> str:
    return (
        "if ! command -v vgs >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        "vgs -o vg_name,vg_size,vg_free,pv_count,lv_count 2>&1"
    )


def cmd_extend_volume_group(vg_name: str, devices: str) -> str:
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    dev_list = _devices_list(devices, "Physical volume device(s) to add")
    q_vg = shlex.quote(vg_name)
    q_devs = " ".join(shlex.quote(d) for d in dev_list)
    return (
        "if ! command -v vgextend >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"vgextend {q_vg} {q_devs} 2>&1"
    )


def cmd_reduce_volume_group(vg_name: str, devices: str) -> str:
    """Removes physical volume(s) from a volume group. Each device
    must already be empty of logical-volume data (pvmove it off
    first) - vgreduce refuses otherwise."""
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    dev_list = _devices_list(devices, "Physical volume device(s) to remove")
    q_vg = shlex.quote(vg_name)
    q_devs = " ".join(shlex.quote(d) for d in dev_list)
    return (
        "if ! command -v vgreduce >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"vgreduce {q_vg} {q_devs} 2>&1"
    )


# ---------------------------------------------------------
# LVM - logical volumes
# ---------------------------------------------------------
def cmd_create_logical_volume(vg_name: str, lv_name: str, size: str) -> str:
    """`size` accepts lvcreate's usual forms - an absolute size (e.g.
    "20G") or a percentage of the volume group ("100%FREE")."""
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    lv_name = _validate_lvm_name(lv_name, "Logical volume name")
    size = (size or "").strip()
    if not size:
        raise ValueError("Size is required (e.g. 20G, or 100%FREE).")
    q_vg = shlex.quote(vg_name)
    q_lv = shlex.quote(lv_name)
    if size.upper().endswith("%FREE") or size.upper().endswith("%VG"):
        size_flag = f"-l {shlex.quote(size)}"
    else:
        size_flag = f"-L {shlex.quote(size)}"
    return (
        "if ! command -v lvcreate >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"lvcreate -n {q_lv} {size_flag} {q_vg} 2>&1"
    )


def cmd_list_logical_volumes() -> str:
    return (
        "if ! command -v lvs >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        "lvs -o vg_name,lv_name,lv_size,lv_path,lv_active 2>&1"
    )


def cmd_extend_logical_volume(vg_name: str, lv_name: str, new_size: str, resize_fs: bool = True) -> str:
    """Grows the LV (`new_size` accepts an absolute size like "20G" or
    a relative one like "+5G"), then - unless resize_fs is False -
    grows whatever filesystem is on it to match (auto-detects ext2/3/4
    via resize2fs, xfs via xfs_growfs while mounted, btrfs via `btrfs
    filesystem resize` while mounted)."""
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    lv_name = _validate_lvm_name(lv_name, "Logical volume name")
    new_size = (new_size or "").strip()
    if not new_size:
        raise ValueError("New size is required (e.g. 20G, or +5G to grow by 5G).")
    lv_path = f"/dev/{vg_name}/{lv_name}"
    q_lv = shlex.quote(lv_path)
    q_size = shlex.quote(new_size)

    cmd = (
        "if ! command -v lvextend >/dev/null 2>&1; then "
        "echo 'LVM tools are not installed on this host (package: lvm2).' >&2; exit 1; fi; "
        f"lvextend -L {q_size} {q_lv} 2>&1"
    )
    if resize_fs:
        cmd += (
            f" && _fst=$(lsblk -no FSTYPE {q_lv} 2>/dev/null); "
            'case "$_fst" in '
            f"ext2|ext3|ext4) resize2fs {q_lv} 2>&1;; "
            f'xfs) _mnt=$(findmnt -no TARGET {q_lv} 2>/dev/null); '
            f'if [ -n "$_mnt" ]; then xfs_growfs "$_mnt" 2>&1; else echo "xfs_growfs needs {lv_path} mounted - mount it, then grow the filesystem separately." >&2; fi;; '
            f'btrfs) _mnt=$(findmnt -no TARGET {q_lv} 2>/dev/null); '
            f'if [ -n "$_mnt" ]; then btrfs filesystem resize max "$_mnt" 2>&1; else echo "btrfs resize needs {lv_path} mounted - mount it, then grow the filesystem separately." >&2; fi;; '
            f'*) echo "LV extended - no recognized filesystem to grow automatically (saw \\"$_fst\\").";; '
            "esac"
        )
    return cmd


def cmd_reduce_logical_volume(vg_name: str, lv_name: str, new_size: str) -> str:
    """Shrinks a logical volume. ext2/3/4 filesystems are unmounted,
    fsck'd, and shrunk (resize2fs) to the new size first, then the LV
    itself is reduced. xfs cannot be shrunk at all (an XFS limitation)
    - back up the data, recreate a smaller LV, and restore instead.
    btrfs shrink-in-place is not attempted here for the same reason."""
    vg_name = _validate_lvm_name(vg_name, "Volume group name")
    lv_name = _validate_lvm_name(lv_name, "Logical volume name")
    new_size = (new_size or "").strip()
    if not new_size:
        raise ValueError("New size is required (e.g. 5G).")
    lv_path = f"/dev/{vg_name}/{lv_name}"
    q_lv = shlex.quote(lv_path)
    q_size = shlex.quote(new_size)
    return rf"""
if ! command -v lvreduce >/dev/null 2>&1; then
    echo "LVM tools are not installed on this host (package: lvm2)." >&2
    exit 1
fi
_fst=$(lsblk -no FSTYPE {q_lv} 2>/dev/null)
case "$_fst" in
    ext2|ext3|ext4)
        _mnt=$(findmnt -no TARGET {q_lv} 2>/dev/null)
        if [ -n "$_mnt" ]; then
            echo "Unmount {lv_path} (currently on $_mnt) before shrinking an ext2/3/4 filesystem." >&2
            exit 1
        fi
        e2fsck -f {q_lv} 2>&1 && resize2fs {q_lv} {q_size} 2>&1 && lvreduce -L {q_size} {q_lv} -y 2>&1
        ;;
    xfs)
        echo "XFS filesystems cannot be shrunk - back up the data, recreate a smaller logical volume, and restore instead." >&2
        exit 1
        ;;
    "")
        lvreduce -L {q_size} {q_lv} -y 2>&1
        ;;
    *)
        echo "Unrecognized filesystem (\"$_fst\") on {lv_path} - shrink it manually first, then re-run." >&2
        exit 1
        ;;
esac
""".strip()


# ---------------------------------------------------------
# RAID (mdadm)
# ---------------------------------------------------------
_VALID_RAID_LEVELS = {"0", "1", "4", "5", "6", "10"}


def cmd_list_raid_arrays() -> str:
    return (
        "echo '-- /proc/mdstat --' && cat /proc/mdstat 2>&1; "
        "echo; echo '-- mdadm --detail --scan --' && "
        "(command -v mdadm >/dev/null 2>&1 && mdadm --detail --scan 2>&1 "
        "|| echo 'mdadm is not installed on this host (package: mdadm).')"
    )


def cmd_create_raid_array(raid_device: str, level: str, devices: str) -> str:
    raid_device = _validate_path(raid_device, "RAID device (e.g. /dev/md0)")
    level = (level or "").strip()
    if level not in _VALID_RAID_LEVELS:
        raise ValueError(f"Unsupported RAID level '{level}'. Supported: {', '.join(sorted(_VALID_RAID_LEVELS))}")
    dev_list = _devices_list(devices, "Member device(s)")
    if len(dev_list) < 2:
        raise ValueError("RAID requires at least 2 member devices.")
    q_raid = shlex.quote(raid_device)
    q_devs = " ".join(shlex.quote(d) for d in dev_list)
    return (
        "if ! command -v mdadm >/dev/null 2>&1; then "
        "echo 'mdadm is not installed on this host (package: mdadm).' >&2; exit 1; fi; "
        f"mdadm --create {q_raid} --level={level} --raid-devices={len(dev_list)} {q_devs} --run 2>&1 "
        f"&& echo 'Created {raid_device} (RAID{level}, {len(dev_list)} member(s)) - check RAID Status for sync progress.'"
    )


def cmd_raid_status(raid_device: str = "") -> str:
    raid_device = (raid_device or "").strip()
    if raid_device:
        q_raid = shlex.quote(raid_device)
        return (
            "if ! command -v mdadm >/dev/null 2>&1; then "
            "echo 'mdadm is not installed on this host (package: mdadm).' >&2; exit 1; fi; "
            f"mdadm --detail {q_raid} 2>&1"
        )
    return cmd_list_raid_arrays()


def cmd_replace_failed_disk(raid_device: str, failed_device: str, new_device: str) -> str:
    """Fails and removes `failed_device` from the array (a no-op if it
    has already dropped out on its own), adds `new_device` in its
    place, and prints the array's status so the rebuild can be
    tracked."""
    raid_device = _validate_path(raid_device, "RAID device")
    failed_device = _validate_path(failed_device, "Failed member device")
    new_device = _validate_path(new_device, "Replacement device")
    q_raid = shlex.quote(raid_device)
    q_failed = shlex.quote(failed_device)
    q_new = shlex.quote(new_device)
    return (
        "if ! command -v mdadm >/dev/null 2>&1; then "
        "echo 'mdadm is not installed on this host (package: mdadm).' >&2; exit 1; fi; "
        f"mdadm {q_raid} --fail {q_failed} 2>&1; "
        f"mdadm {q_raid} --remove {q_failed} 2>&1; "
        f"mdadm {q_raid} --add {q_new} 2>&1; "
        f"echo '-- Status after replacement --'; mdadm --detail {q_raid} 2>&1"
    )


# ---------------------------------------------------------
# Swap
# ---------------------------------------------------------
def cmd_list_swap() -> str:
    return (
        "echo '-- swapon --show --' && (swapon --show 2>&1 || echo '(no active swap)'); "
        "echo; echo '-- free -h --' && free -h 2>&1"
    )


def cmd_create_swap_file(path: str, size_mb, persist: bool = True) -> str:
    """Creates a new swap file of `size_mb` megabytes at `path`, locks
    it to root-only permissions, formats and activates it, and (by
    default) adds a matching /etc/fstab entry so it survives a
    reboot."""
    path = _validate_path(path, "Swap file path")
    size_mb = _validate_int_range(size_mb, 1, 1_048_576, "Size (MB)")
    q_path = shlex.quote(path)
    cmd = (
        f"(fallocate -l {size_mb}M {q_path} 2>/dev/null || dd if=/dev/zero of={q_path} bs=1M count={size_mb} 2>&1) "
        f"&& chmod 600 {q_path} "
        f"&& mkswap {q_path} 2>&1 "
        f"&& swapon {q_path} 2>&1 "
        f"&& echo 'Swap file {path} ({size_mb}MB) created and activated.'"
    )
    if persist:
        q_line = shlex.quote(f"{path}\tnone\tswap\tsw\t0\t0")
        q_path_match = shlex.quote(path)
        cmd += (
            f" && (grep -qF {q_path_match} /etc/fstab 2>/dev/null "
            f"|| (cp /etc/fstab /etc/fstab.bak.$(date +%s) && echo {q_line} >> /etc/fstab)) "
            "&& echo 'Added to /etc/fstab.'"
        )
    return cmd


def cmd_create_swap_partition(device: str, persist: bool = True) -> str:
    """Formats an existing partition/device as swap and activates it.
    The partition must already exist (Create Partition above)."""
    device = _validate_path(device, "Device")
    q_dev = shlex.quote(device)
    cmd = (
        f"mkswap {q_dev} 2>&1 "
        f"&& swapon {q_dev} 2>&1 "
        f"&& echo 'Swap partition {device} created and activated.'"
    )
    if persist:
        q_line = shlex.quote(f"{device}\tnone\tswap\tsw\t0\t0")
        q_dev_match = shlex.quote(device)
        cmd += (
            f" && (grep -qF {q_dev_match} /etc/fstab 2>/dev/null "
            f"|| (cp /etc/fstab /etc/fstab.bak.$(date +%s) && echo {q_line} >> /etc/fstab)) "
            "&& echo 'Added to /etc/fstab.'"
        )
    return cmd


def cmd_disable_swap(target: str, remove_fstab: bool = False) -> str:
    """Deactivates swap on `target` (file path or device). With
    remove_fstab, also strips any matching /etc/fstab line."""
    target = _validate_path(target, "Swap file or device")
    q_target = shlex.quote(target)
    cmd = f"swapoff {q_target} 2>&1 && echo 'Swap on {target} deactivated.'"
    if remove_fstab:
        cmd += (
            f" && cp /etc/fstab /etc/fstab.bak.$(date +%s) "
            f"&& awk -v t={q_target} '$1 != t' /etc/fstab > /tmp/fstab.new.$$ "
            f"&& mv /tmp/fstab.new.$$ /etc/fstab && echo 'Removed matching /etc/fstab entry.'"
        )
    return cmd
