"""REPOSITORY MANAGEMENT dual-host command builders - split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_repo import *` at the bottom of client/api.py.

Add writes a predictable, alias-named config file on zypper and
Debian/Ubuntu (so Enable/Disable/Remove afterward have something
reliable to target), but uses the native `dnf/yum config-manager
--add-repo <url>` on the RPM/dnf family instead of synthesizing a
file.
"""
import shlex

from client._api_automation import _pkgmgr_dispatch, _validate_repo_alias


def cmd_list_repositories() -> str:
    return _pkgmgr_dispatch(
        rpm_cmd='"$PKGMGR" repolist all',
        zypper_cmd='zypper repos --details',
        apt_cmd=(
            'for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do '
            '[ -f "$f" ] || continue; '
            'echo "--- $f ---"; cat "$f"; echo; '
            'done'
        ),
    )


def cmd_add_repository(url: str, alias: str = "") -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Repository URL or source line is required.")
    q_url = shlex.quote(url)
    alias = (alias or "").strip()

    rpm_cmd = (
        'if [ "$PKGMGR" = "dnf" ]; then '
        f'dnf config-manager --add-repo {q_url} 2>&1 || '
        'echo "Failed - is dnf-plugins-core installed? (Install Packages: dnf-plugins-core)"; '
        'else '
        f'yum-config-manager --add-repo {q_url} 2>&1 || '
        'echo "Failed - is yum-utils installed? (Install Packages: yum-utils)"; '
        'fi'
    )

    if alias:
        alias_safe = _validate_repo_alias(alias)
        q_alias = shlex.quote(alias_safe)
        apt_path = shlex.quote(f"/etc/apt/sources.list.d/{alias_safe}.list")
        zypper_cmd = f'zypper --non-interactive addrepo {q_url} {q_alias}'
        apt_cmd = f'echo {q_url} > {apt_path} && apt-get update'
    else:
        zypper_cmd = 'echo "zypper requires the Alias / Repository ID field - fill it in and retry."'
        apt_cmd = 'echo "Debian/Ubuntu requires the Alias / Repository ID field (used as the filename) - fill it in and retry."'

    return _pkgmgr_dispatch(rpm_cmd=rpm_cmd, zypper_cmd=zypper_cmd, apt_cmd=apt_cmd)


def cmd_enable_repository(alias: str) -> str:
    """dnf and yum take genuinely different command lines here, not
    just a "$PKGMGR" substitution - branches on $PKGMGR explicitly."""
    alias_safe = _validate_repo_alias(alias)
    q_alias = shlex.quote(alias_safe)
    base = f"/etc/apt/sources.list.d/{alias_safe}"
    list_path = shlex.quote(base + ".list")
    disabled_path = shlex.quote(base + ".list.disabled")

    rpm_cmd = (
        f'if [ "$PKGMGR" = "dnf" ]; then dnf config-manager --set-enabled {q_alias}; '
        f'else yum-config-manager --enable {q_alias}; fi'
    )

    return _pkgmgr_dispatch(
        rpm_cmd=rpm_cmd,
        zypper_cmd=f'zypper modifyrepo --enable {q_alias}',
        apt_cmd=(
            f'if [ -f {disabled_path} ]; then mv {disabled_path} {list_path} && apt-get update; '
            f'elif [ -f {list_path} ]; then echo "Already enabled."; '
            f'else echo "No such repository file: {base}.list (or .list.disabled)"; fi'
        ),
    )


def cmd_disable_repository(alias: str) -> str:
    """See cmd_enable_repository's docstring - dnf and yum need
    genuinely different command lines."""
    alias_safe = _validate_repo_alias(alias)
    q_alias = shlex.quote(alias_safe)
    base = f"/etc/apt/sources.list.d/{alias_safe}"
    list_path = shlex.quote(base + ".list")
    disabled_path = shlex.quote(base + ".list.disabled")

    rpm_cmd = (
        f'if [ "$PKGMGR" = "dnf" ]; then dnf config-manager --set-disabled {q_alias}; '
        f'else yum-config-manager --disable {q_alias}; fi'
    )

    return _pkgmgr_dispatch(
        rpm_cmd=rpm_cmd,
        zypper_cmd=f'zypper modifyrepo --disable {q_alias}',
        apt_cmd=(
            f'if [ -f {list_path} ]; then mv {list_path} {disabled_path} && apt-get update; '
            f'elif [ -f {disabled_path} ]; then echo "Already disabled."; '
            f'else echo "No such repository file: {base}.list (or .list.disabled)"; fi'
        ),
    )


def cmd_remove_repository(alias: str) -> str:
    alias_safe = _validate_repo_alias(alias)
    q_alias = shlex.quote(alias_safe)
    base = f"/etc/apt/sources.list.d/{alias_safe}"
    list_path = shlex.quote(base + ".list")
    disabled_path = shlex.quote(base + ".list.disabled")

    rpm_cmd = (
        f'f=$(grep -lF "[{alias_safe}]" /etc/yum.repos.d/*.repo 2>/dev/null | head -n1); '
        f'if [ -n "$f" ]; then rm -f "$f"; echo "Removed $f"; '
        f'else echo "No .repo file found defining [{alias_safe}]."; fi'
    )

    return _pkgmgr_dispatch(
        rpm_cmd=rpm_cmd,
        zypper_cmd=f'zypper --non-interactive removerepo {q_alias}',
        apt_cmd=f'rm -f {list_path} {disabled_path} && apt-get update && echo "Removed."',
    )


def cmd_create_repository(
    alias: str,
    baseurl: str,
    name: str = "",
    gpgcheck: bool = True,
    gpgkey: str = "",
    distribution: str = "",
    components: str = "",
) -> str:
    """Builds a real repo definition from discrete fields:
      - dnf/yum: writes a real /etc/yum.repos.d/<alias>.repo file.
      - zypper: `zypper addrepo --name ...`
      - apt: a full first-class `deb [...] <baseurl> <distribution>
        <components>` line written to
        /etc/apt/sources.list.d/<alias>.list.

    GPG handling: when GPG Check is on and a key URL is supplied, the
    key is actively fetched and imported/trusted on the host - `rpm
    --import` for the RPM family, and download + `gpg --dearmor` into
    /etc/apt/keyrings (referenced via signed-by=) for apt, since
    apt-key is deprecated. Turning GPG Check off skips key handling
    entirely and marks the repo trusted/unverified instead.
    """
    alias_safe = _validate_repo_alias(alias)
    baseurl = (baseurl or "").strip()
    if not baseurl:
        raise ValueError("Base URL is required.")
    name = (name or "").strip() or alias_safe
    gpgkey = (gpgkey or "").strip()
    distribution = (distribution or "").strip() or "stable"
    components = (components or "").strip() or "main"

    for label, value in (
        ("Name", name), ("Base URL", baseurl), ("GPG Key URL", gpgkey),
        ("Distribution", distribution), ("Components", components),
    ):
        if "\n" in value or "\r" in value:
            raise ValueError(f"{label} cannot contain a newline.")

    q_alias = shlex.quote(alias_safe)
    q_name = shlex.quote(name)
    q_baseurl = shlex.quote(baseurl)
    q_gpgkey = shlex.quote(gpgkey) if gpgkey else ""

    delim = "SYSIBLE_REPO_EOF"

    # --- dnf / yum -------------------------------------------------
    repo_lines = [f"[{alias_safe}]", f"name={name}", f"baseurl={baseurl}", "enabled=1"]
    if gpgcheck:
        repo_lines.append("gpgcheck=1")
        if gpgkey:
            repo_lines.append(f"gpgkey={gpgkey}")
    else:
        repo_lines.append("gpgcheck=0")
    repo_file_body = "\n".join(repo_lines)

    rpm_import = f'rpm --import {q_gpgkey} 2>&1; ' if (gpgcheck and gpgkey) else ""
    rpm_cmd = (
        f"cat > /etc/yum.repos.d/{alias_safe}.repo <<'{delim}'\n"
        f"{repo_file_body}\n"
        f"{delim}\n"
        f"{rpm_import}"
        f'"$PKGMGR" makecache -y 2>&1; '
        f'echo "Created repository {alias_safe} (/etc/yum.repos.d/{alias_safe}.repo)."'
    )

    # --- zypper ------------------------------------------------------
    gpg_flag = "--gpgcheck" if gpgcheck else "--no-gpgcheck"
    zypper_import = f'rpm --import {q_gpgkey} 2>&1; ' if (gpgcheck and gpgkey) else ""
    zypper_cmd = (
        f'zypper --non-interactive addrepo --name {q_name} {gpg_flag} {q_baseurl} {q_alias} 2>&1; '
        f'{zypper_import}'
        f'zypper --non-interactive --gpg-auto-import-keys refresh {q_alias} 2>&1; '
        f'echo "Created repository {alias_safe}."'
    )

    # --- apt -----------------------------------------------------
    keyring_path = f"/etc/apt/keyrings/{alias_safe}.gpg"
    q_keyring_path = shlex.quote(keyring_path)
    list_path = f"/etc/apt/sources.list.d/{alias_safe}.list"

    if gpgcheck and gpgkey:
        deb_opts = f"signed-by={keyring_path}"
        key_setup = (
            f'mkdir -p /etc/apt/keyrings && '
            f'(curl -fsSL {q_gpgkey} || wget -qO- {q_gpgkey}) | '
            f'gpg --batch --yes --dearmor -o {q_keyring_path} 2>&1; '
        )
    elif gpgcheck:
        deb_opts = ""
        key_setup = ""
    else:
        deb_opts = "trusted=yes"
        key_setup = ""

    deb_prefix = f"deb [{deb_opts}] " if deb_opts else "deb "
    deb_line = f"{deb_prefix}{baseurl} {distribution} {components}"

    apt_cmd = (
        f"{key_setup}"
        f"cat > {shlex.quote(list_path)} <<'{delim}'\n"
        f"{deb_line}\n"
        f"{delim}\n"
        f'apt-get update 2>&1; '
        f'echo "Created repository {alias_safe} ({list_path})."'
    )

    return _pkgmgr_dispatch(rpm_cmd=rpm_cmd, zypper_cmd=zypper_cmd, apt_cmd=apt_cmd)
