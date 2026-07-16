"""Mount options must not allow /etc/fstab-line injection.

The mount command inlines options via shlex.quote (shell-safe), but on persist the
options are written into an /etc/fstab line, which is whitespace-delimited and
newline-terminated — a newline in the options would append an attacker-controlled
fstab entry, mounted at every boot as root. Options are always comma-separated
tokens, so whitespace/newlines are rejected outright.
"""
import pytest

from client import _api_filesystem_mount as m


def test_nfs_options_reject_newline():
    with pytest.raises(ValueError):
        m.cmd_mount_nfs("nfs.example.com", "/exports/data", "/mnt/data",
                        options="defaults 0 0\n/dev/sda /mnt/evil ext4 defaults",
                        persist=True)


def test_cifs_options_reject_newline():
    with pytest.raises(ValueError):
        m.cmd_mount_cifs("smb.example.com", "share", "/mnt/s",
                         options="rw\nmalicious", persist=True)


def test_nfs_options_reject_space():
    with pytest.raises(ValueError):
        m.cmd_mount_nfs("nfs.example.com", "/exports/data", "/mnt/data",
                        options="rw noexec", persist=False)


def test_normal_options_accepted():
    cmd = m.cmd_mount_nfs("nfs.example.com", "/exports/data", "/mnt/data",
                          options="rw,noexec,vers=3", persist=True)
    assert "rw,noexec,vers=3" in cmd


# --- fstab-line injection via OTHER fields (fstype / options / export path / share) ---

def test_add_fstab_entry_rejects_newline_in_fstype():
    # fstype is written verbatim into the /etc/fstab line; a newline would append a
    # second, attacker-controlled entry mounted at every boot as root.
    with pytest.raises(ValueError):
        m.cmd_add_fstab_entry("/dev/sdz", "/mnt/ok",
                              fstype="ext4\ntmpfs /mnt/inject tmpfs defaults 0 0",
                              options="defaults")


def test_add_fstab_entry_rejects_newline_in_options():
    with pytest.raises(ValueError):
        m.cmd_add_fstab_entry("/dev/sdz", "/mnt/ok", fstype="ext4",
                              options="defaults\ntmpfs /mnt/inject tmpfs defaults 0 0")


def test_add_fstab_entry_normal_accepted():
    cmd = m.cmd_add_fstab_entry("/dev/sdb1", "/mnt/data", fstype="ext4", options="defaults,noatime")
    assert "ext4" in cmd and "defaults,noatime" in cmd


def test_nfs_export_path_rejects_newline():
    # export_path flows into the persisted fstab line via `src`.
    with pytest.raises(ValueError):
        m.cmd_mount_nfs("nfs.example.com", "/e\n/dev/sdz /mnt/inject ext4 defaults 0 0",
                        "/mnt/data", persist=True)


def test_cifs_share_rejects_newline():
    with pytest.raises(ValueError):
        m.cmd_mount_cifs("smb.example.com", "share\n//x/y /mnt/z cifs guest 0 0",
                         "/mnt/s", persist=True)
