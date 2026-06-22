"""Containers & VMs command builders (dual-host).

Docker/Podman containers and images, plus libvirt virtual machines.
Plain POSIX sh, shlex.quote() on interpolated names, explicit messages
when a runtime/tool is absent, and real exit codes for the banner.
"""
import shlex

# Container runtime: prefer docker, fall back to podman (CLI-compatible).
_RT = (
    'rt=$(command -v docker 2>/dev/null || command -v podman 2>/dev/null); '
    'if [ -z "$rt" ]; then echo "No container runtime found (docker or podman)." >&2; exit 1; fi; '
)
_VIRSH = (
    'if ! command -v virsh >/dev/null 2>&1; then '
    'echo "virsh (libvirt) is not installed on this host." >&2; exit 1; fi; '
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


def cmd_container_action(action: str, name: str) -> str:
    action = (action or "").strip()
    name = (name or "").strip()
    if action not in _CONTAINER_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(sorted(_CONTAINER_ACTIONS))}.")
    if not name:
        raise ValueError("Container name or ID is required.")
    qn = shlex.quote(name)
    return _RT + f'"$rt" {action} {qn} && echo "{action} {name}: done."'


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


def cmd_vm_action(action: str, name: str) -> str:
    action = (action or "").strip()
    name = (name or "").strip()
    if action not in _VM_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(sorted(_VM_ACTIONS))}.")
    if not name:
        raise ValueError("VM (domain) name is required.")
    qn = shlex.quote(name)
    return _VIRSH + f'virsh {action} {qn} 2>&1 && echo "{action} {name}: requested."'


def cmd_vm_info(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("VM (domain) name is required.")
    qn = shlex.quote(name)
    return _VIRSH + f'virsh dominfo {qn} 2>&1'
