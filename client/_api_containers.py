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

# A domain that is ALREADY in the state you asked for is not a failure.
#
# `virsh destroy` on a stopped domain says "Requested operation is not valid:
# domain is not running" and exits non-zero. Ask for it across a fleet that is
# already powered off and every row comes back red -- fifteen identical errors
# whose actual meaning is "they were all already off". The one thing the
# operator wants to know, whether the fleet is now in the state they asked for,
# is the one thing that output does not say.
#
# So the already-there cases are recognised and reported as such. They do not
# set rc: a real failure still has to stay visible, which is the whole reason
# these are worth separating out.
_ALREADY = {
    # action: (grep -E pattern on virsh's stderr, what to say instead)
    "destroy": ("domain is not running", "already stopped"),
    "shutdown": ("domain is not running", "already stopped"),
    "start": ("domain is already (running|active)", "already running"),
    "suspend": ("domain is not running", "not running, so nothing to suspend"),
    "resume": ("domain is not running", "not running, so nothing to resume"),
    "reboot": ("domain is not running", "not running, so nothing to reboot"),
}


def _vm_step(action: str) -> str:
    """Shell for ONE domain in `$vm`: run it, and tell 'already there' from a
    real error. Written once; both the named-list and the 'all' loop use it."""
    pat, said = _ALREADY.get(action, ("", ""))
    run = f'out=$(virsh {action} "$vm" 2>&1); st=$?'
    if not pat:
        return (f'{run}; [ "$st" -eq 0 ] || rc=1; '
                f'echo "== {action} $vm =="; [ -n "$out" ] && echo "$out"; :')
    return (
        f'{run}; '
        f'if [ "$st" -eq 0 ]; then echo "== {action} $vm =="; '
        f'[ -n "$out" ] && echo "$out"; :; '
        # already in the requested state: say so, and do NOT fail the run
        f'elif printf "%s" "$out" | grep -Eqi {shlex.quote(pat)}; then '
        f'skipped=$((skipped+1)); echo "-- {said}: $vm"; '
        f'else rc=1; echo "== {action} $vm =="; echo "$out"; fi')


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
            f'rc=0; for c in $ids; do echo "== {action} $c =="; "$rt" {action} "$c" 2>&1 || rc=1; done; '
            f'echo "{action}: requested on all containers."; exit "$rc"'
        )
    targets = _split_targets(name)
    if len(targets) == 1:
        return _RT + f'"$rt" {action} {shlex.quote(targets[0])} && echo "{action}: done."'
    quoted = " ".join(shlex.quote(t) for t in targets)
    return _RT + (
        f'rc=0; for c in {quoted}; do echo "== {action} $c =="; "$rt" {action} "$c" 2>&1 || rc=1; done; '
        f'echo "{action}: requested on {len(targets)} container(s)."; exit "$rc"'
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
        # The while-loop is the RHS of the pipe (a subshell), so `exit "$rc"` must
        # be INSIDE the brace group for the pipeline's status to reflect it.
        return _VIRSH + (
            'virsh list --all --name 2>/dev/null | { rc=0; n=0; skipped=0; '
            'while IFS= read -r vm; do '
            '[ -n "$vm" ] || continue; n=$((n+1)); '
            + _vm_step(action) + '; '
            f'done; echo "{action}: $n VM(s)'
            '$([ "$skipped" -gt 0 ] && echo ", $skipped already there")'
            '$([ "$rc" -ne 0 ] && echo ", some failed")."; exit "$rc"; }'
        )
    targets = _split_targets(name)
    quoted = " ".join(shlex.quote(t) for t in targets)
    # ONE COMMAND GOES TO EVERY SELECTED HOST, and the named VMs are spread
    # across them. Acting blind made each host fail for every VM it does not
    # own: pick three VMs off two hypervisors and both hosts report failures
    # for the other's, so a run that did exactly what was asked comes back red.
    #
    # So each host acts on the names IT has and says plainly which it skipped.
    # The list is read once per host rather than per VM -- `virsh dominfo` in
    # the loop is a process per name, and these lists run to hundreds.
    #
    # grep -Fxq: fixed string, whole line, so `web` never matches `web-1`.
    # `--` so a domain whose name begins with a dash is not read as a flag.
    return _VIRSH + (
        f'have=$(virsh list --all --name 2>/dev/null); rc=0; n=0; skipped=0; '
        f'for vm in {quoted}; do '
        f'if printf "%s\\n" "$have" | grep -Fxq -- "$vm"; then '
        f'n=$((n+1)); ' + _vm_step(action) + '; '
        f'else echo "-- not on this host: $vm"; fi; '
        f'done; '
        # Nothing matched is a failure, not a quiet success: the operator asked
        # for something and this host did none of it.
        f'[ "$n" -gt 0 ] || rc=1; '
        f'echo "{action}: $n of {len(targets)} VM(s) on this host'
        '$([ "$skipped" -gt 0 ] && echo \", $skipped already there\")'
        '$([ \"$rc\" -ne 0 ] && echo \", some failed\")."; '
        f'exit "$rc"'
    )


def cmd_vm_delete(name: str, remove_storage: bool = False) -> str:
    """Delete VMs: `virsh undefine`, optionally with their disks.

    Asked for as "I should be able to delete the VMs from the VM panel". The
    panel could start, stop, force-off and inspect a VM but not remove one, so
    the last step of decommissioning a host meant going to a shell.

    Three things this has to get right, none of them obvious from the verb:

    RUNNING DOMAINS. `undefine` on a running domain does NOT stop it. It
    removes the persistent definition and leaves the domain running as a
    TRANSIENT one -- so it vanishes from `virsh list --all`, keeps its RAM and
    CPU, and disappears for good at the next host reboot with no config to
    recreate it from. That is the worst outcome available here, so a running
    domain is refused and told to stop first rather than quietly half-deleted.

    UEFI. A domain with an nvram file fails to undefine without --nvram, with
    an error most people read as a permissions problem. Retried with the flag
    rather than handed back.

    STORAGE. Disks are kept unless asked for. `--remove-all-storage` deletes
    every writable volume libvirt can see for that domain, which is not
    something to default to -- and not something to do quietly either, so each
    deleted disk is named in the output.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("VM (domain) name is required (one or more, or 'all').")
    targets = _split_targets(name)
    flags = " --remove-all-storage" if remove_storage else ""
    # The disks are listed BEFORE the undefine: afterwards there is no domain
    # left to ask, so an operator who wanted to know what went has no way back.
    show = ('echo "   disks:"; virsh domblklist "$vm" --details 2>/dev/null '
            '| awk \'$2=="disk"{print "     " $4}\'; ') if remove_storage else ""
    body = (
        'state=$(virsh domstate "$vm" 2>/dev/null); '
        'case "$state" in *running*|*paused*) '
        'rc=1; echo "-- still $state: $vm (stop it first — undefining a running '
        'domain leaves it running with no definition to restart it from)"; '
        'continue;; esac; '
        f'echo "== delete $vm =="; {show}'
        f'out=$(virsh undefine "$vm"{flags} 2>&1); st=$?; '
        # UEFI domains need --nvram; the bare error does not say so.
        'if [ "$st" -ne 0 ] && printf "%s" "$out" | grep -qi nvram; then '
        f'out=$(virsh undefine "$vm"{flags} --nvram 2>&1); st=$?; fi; '
        'if [ "$st" -eq 0 ]; then n=$((n+1)); echo "   deleted"; '
        'else rc=1; echo "$out"; fi')
    if name.lower() in ("all", "*"):
        return _VIRSH + (
            'virsh list --all --name 2>/dev/null | { rc=0; n=0; '
            'while IFS= read -r vm; do [ -n "$vm" ] || continue; '
            + body + '; done; '
            'echo "delete: $n VM(s) removed'
            '$([ "$rc" -ne 0 ] && echo ", some skipped or failed")."; exit "$rc"; }')
    quoted = " ".join(shlex.quote(t) for t in targets)
    return _VIRSH + (
        f'have=$(virsh list --all --name 2>/dev/null); rc=0; n=0; '
        f'for vm in {quoted}; do '
        f'if printf "%s\\n" "$have" | grep -Fxq -- "$vm"; then ' + body + '; '
        f'else echo "-- not on this host: $vm"; fi; done; '
        f'[ "$n" -gt 0 ] || rc=1; '
        f'echo "delete: $n of {len(targets)} VM(s) removed on this host'
        '$([ "$rc" -ne 0 ] && echo \", some skipped or failed\")."; '
        f'exit "$rc"')


def cmd_vm_info(name: str) -> str:
    """Domain info for one VM or several.

    It took exactly one name, which made the VM-name field a single-select for
    the whole Containers & VMs group -- the form renders ONE field for a param
    shared by several actions, so the narrowest action decided what everybody
    got. Ticking three VMs and asking for info is a reasonable thing to want,
    and nothing about dominfo objects to being run three times.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("VM (domain) name is required.")
    targets = _split_targets(name)
    if len(targets) == 1 and targets[0].lower() not in ("all", "*"):
        return _VIRSH + f'virsh dominfo {shlex.quote(targets[0])} 2>&1'
    if name.lower() in ("all", "*"):
        return _VIRSH + (
            'virsh list --all --name 2>/dev/null | { rc=0; while IFS= read -r vm; do '
            '[ -n "$vm" ] || continue; echo "== $vm =="; virsh dominfo "$vm" 2>&1 || rc=1; '
            'done; exit "$rc"; }')
    quoted = " ".join(shlex.quote(t) for t in targets)
    # Same shape as cmd_vm_action: each host answers for the domains it has and
    # says which it skipped, rather than erroring once per VM it never owned.
    return _VIRSH + (
        f'have=$(virsh list --all --name 2>/dev/null); rc=0; n=0; '
        f'for vm in {quoted}; do '
        f'if printf "%s\\n" "$have" | grep -Fxq -- "$vm"; then '
        f'n=$((n+1)); echo "== $vm =="; virsh dominfo "$vm" 2>&1 || rc=1; '
        f'else echo "-- not on this host: $vm"; fi; done; '
        f'[ "$n" -gt 0 ] || rc=1; exit "$rc"')
