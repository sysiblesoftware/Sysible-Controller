"""Containers & VMs command builders (dual-host).

Docker/Podman containers and images, plus libvirt virtual machines.
Plain POSIX sh, shlex.quote() on interpolated names, explicit messages
when a runtime/tool is absent, and real exit codes for the banner.
"""
import re
import shlex

# Container runtime: prefer docker, fall back to podman (CLI-compatible).
_RT = (
    'rt=$(command -v docker 2>/dev/null || command -v podman 2>/dev/null); '
    'if [ -z "$rt" ]; then echo "No container runtime found (docker or podman)." >&2; exit 1; fi; '
)
_VIRSH = (
    'if ! command -v virsh >/dev/null 2>&1; then '
    'echo "virsh (libvirt) is not installed on this host." >&2; exit 1; fi; '
    # Dispatched commands run non-interactively — no login shell, so a host's
    # LIBVIRT_DEFAULT_URI (commonly exported from /etc/profile.d) is never in
    # scope here. Bare virsh then defaults to the per-user qemu:///session
    # (empty) instead of qemu:///system where host VMs actually run, so
    # "List VMs" silently returns nothing. Pin the system instance unless an
    # operator has explicitly set a URI in the agent's own environment.
    ': "${LIBVIRT_DEFAULT_URI:=qemu:///system}"; export LIBVIRT_DEFAULT_URI; '
)

_CONTAINER_ACTIONS = {"start", "stop", "restart", "rm", "pause", "unpause"}
_VM_ACTIONS = {"start", "shutdown", "reboot", "destroy", "suspend", "resume"}


def cmd_container_runtime() -> str:
    return (
        'if command -v docker >/dev/null 2>&1; then echo "Runtime: docker"; docker version --format "{{.Server.Version}}" 2>/dev/null; '
        'elif command -v podman >/dev/null 2>&1; then echo "Runtime: podman"; podman version --format "{{.Version}}" 2>/dev/null; '
        'else echo "No container runtime found (docker or podman)."; fi'
    )


def cmd_list_containers(all_containers: bool = True) -> str:
    flag = "-a " if all_containers else ""
    return _RT + (
        f'"$rt" ps {flag}'
        '--format "table {{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}" 2>&1 '
        '|| "$rt" ps ' + flag.strip()
    )


def cmd_list_images() -> str:
    return _RT + '"$rt" images 2>&1'


def _split_targets(name: str) -> list:
    """Split a name field into individual targets on commas/whitespace (drops blanks)."""
    return [n for n in re.split(r"[,\s]+", name or "") if n]


def cmd_container_action(action: str, name: str) -> str:
    action = (action or "").strip()
    name = (name or "").strip()
    if action not in _CONTAINER_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(sorted(_CONTAINER_ACTIONS))}.")
    if not name:
        raise ValueError("Container name or ID is required (one or more, or 'all').")
    # Only the whitelisted `action` is ever interpolated into the shell; every
    # container name goes through a shlex-quoted list or the shell loop var `$c`,
    # never the raw name into a double-quoted echo (which could command-substitute).
    if name.lower() in ("all", "*"):
        # Act on every container (running + stopped); continue past per-container
        # errors (e.g. stop on an already-stopped one) and report each.
        return _RT + (
            f'ids=$("$rt" ps -aq 2>/dev/null); '
            f'[ -n "$ids" ] || {{ echo "No containers found."; exit 0; }}; '
            f'for c in $ids; do echo "== {action} $c =="; "$rt" {action} "$c" 2>&1; done; '
            f'echo "{action}: requested on all containers."'
        )
    targets = _split_targets(name)
    if len(targets) == 1:
        return _RT + f'"$rt" {action} {shlex.quote(targets[0])} && echo "{action}: done."'
    quoted = " ".join(shlex.quote(t) for t in targets)
    return _RT + (
        f'for c in {quoted}; do echo "== {action} $c =="; "$rt" {action} "$c" 2>&1; done; '
        f'echo "{action}: requested on {len(targets)} container(s)."'
    )


def cmd_container_logs(name: str, lines: int = 200) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Container name or ID is required.")
    lines = max(1, int(lines))
    qn = shlex.quote(name)
    return _RT + f'"$rt" logs --tail {lines} {qn} 2>&1'


def cmd_container_prune() -> str:
    return _RT + '"$rt" system prune -f 2>&1 && echo "Pruned stopped containers, unused networks, and dangling images."'


def cmd_list_vms() -> str:
    return _VIRSH + 'virsh list --all 2>&1'


def cmd_list_vm_names() -> str:
    """Bare domain names, one per line (no header/state columns). Used to
    populate the VM-name dropdown in the console — kept separate from
    cmd_list_vms so the picker gets a clean, parseable list."""
    return _VIRSH + 'virsh list --all --name 2>/dev/null'


def cmd_vm_action(action: str, name: str) -> str:
    action = (action or "").strip()
    name = (name or "").strip()
    if action not in _VM_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(sorted(_VM_ACTIONS))}.")
    if not name:
        raise ValueError("VM (domain) name is required (one or more, or 'all').")
    # Only the whitelisted `action` is interpolated; domain names go through a
    # shlex-quoted list or the shell loop var `$vm`, never the raw name into an echo.
    if name.lower() in ("all", "*"):
        # Every domain (list --all --name yields one per line). Continue past
        # per-VM errors (e.g. start on an already-running one) and report each.
        return _VIRSH + (
            'virsh list --all --name 2>/dev/null | while IFS= read -r vm; do '
            '[ -n "$vm" ] || continue; '
            f'echo "== {action} $vm =="; virsh {action} "$vm" 2>&1; '
            f'done; echo "{action}: requested on all VMs."'
        )
    targets = _split_targets(name)
    if len(targets) == 1:
        return _VIRSH + f'virsh {action} {shlex.quote(targets[0])} 2>&1 && echo "{action}: requested."'
    quoted = " ".join(shlex.quote(t) for t in targets)
    return _VIRSH + (
        f'for vm in {quoted}; do echo "== {action} $vm =="; virsh {action} "$vm" 2>&1; done; '
        f'echo "{action}: requested on {len(targets)} VM(s)."'
    )


def cmd_vm_info(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("VM (domain) name is required.")
    qn = shlex.quote(name)
    return _VIRSH + f'virsh dominfo {qn} 2>&1'
