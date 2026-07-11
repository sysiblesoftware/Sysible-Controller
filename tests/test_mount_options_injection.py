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
