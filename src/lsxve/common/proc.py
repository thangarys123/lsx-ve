"""Safe subprocess execution.

All system commands in LSX-VE are executed through ``run``:
  * argv lists only - never a shell, so no shell metacharacter interpretation;
  * the executable must be an absolute path from a fixed allow-list;
  * a minimal, fixed environment;
  * mandatory timeout.
User input may only appear as individual, pre-validated argv elements.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional, Sequence

ALLOWED = {
    "/usr/bin/nmcli", "/usr/bin/hostnamectl", "/usr/bin/timedatectl", "/usr/bin/systemctl",
    "/usr/bin/firewall-cmd", "/usr/bin/lsblk", "/usr/bin/findmnt", "/usr/sbin/smartctl",
    "/usr/sbin/wipefs", "/usr/sbin/parted", "/usr/sbin/mkfs.xfs", "/usr/sbin/mkfs.ext4",
    "/usr/sbin/pvcreate", "/usr/sbin/vgcreate", "/usr/sbin/vgs", "/usr/sbin/pvs", "/usr/sbin/lvs",
    "/usr/bin/mount", "/usr/bin/umount", "/usr/sbin/blkid", "/usr/bin/journalctl",
    "/usr/sbin/chpasswd", "/usr/bin/udevadm", "/usr/sbin/partprobe", "/usr/bin/chronyc",
    "/usr/sbin/ip", "/usr/bin/ip", "/usr/sbin/dmidecode", "/usr/bin/openssl", "/usr/bin/virt-host-validate",
    "/usr/sbin/restorecon", "/usr/sbin/semanage", "/usr/bin/sync", "/usr/sbin/modprobe",
    "/usr/bin/chown", "/usr/sbin/tuned-adm", "/usr/bin/tuned-adm", "/usr/bin/ping",
    # LXC containers (lsxve.ct.lxc)
    "/usr/bin/lxc-create", "/usr/bin/lxc-destroy", "/usr/bin/lxc-info", "/usr/bin/lxc-stop",
    "/usr/bin/lxc-freeze", "/usr/bin/lxc-unfreeze", "/usr/bin/lxc-copy", "/usr/bin/lxc-snapshot",
    "/usr/bin/tar", "/usr/bin/setsid", "/usr/bin/lxc-ls",
    # VM import / export, support bundles
    "/usr/bin/qemu-img", "/usr/bin/rpm", "/usr/sbin/sestatus",
}
# commands that may be started on a terminal (interactive container console)
TTY_ALLOWED = {("/usr/bin/setsid", "--ctty", "--wait", "/usr/bin/lxc-console")}

_ENV = {"PATH": "/usr/sbin:/usr/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int, stderr: str):
        self.argv = list(argv)
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"{os.path.basename(argv[0])} failed ({code}): {self.stderr[:400]}")


def run(argv: Sequence[str], timeout: float = 30, input_text: Optional[str] = None,
        check: bool = True, ok_codes: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
    if not argv or argv[0] not in ALLOWED:
        raise PermissionError(f"command not allowed: {argv[0] if argv else '<empty>'}")
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise ValueError("invalid argument")
    cp = subprocess.run(list(argv), input=input_text, capture_output=True, text=True,
                        timeout=timeout, env=_ENV, shell=False, close_fds=True)
    if check and cp.returncode not in ok_codes:
        raise CommandError(argv, cp.returncode, cp.stderr or cp.stdout)
    return cp


def spawn_on_tty(argv: Sequence[str], tty_fd: int) -> subprocess.Popen:
    """Start an allow-listed interactive command with stdin/stdout/stderr on a pty slave."""
    if tuple(argv[:4]) not in TTY_ALLOWED:
        raise PermissionError(f"command not allowed on a terminal: {argv[:4]}")
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise ValueError("invalid argument")
    env = dict(_ENV, TERM="xterm-256color")
    return subprocess.Popen(list(argv), stdin=tty_fd, stdout=tty_fd, stderr=tty_fd, env=env, shell=False,
                            close_fds=True)


def which(path: str) -> bool:
    return os.path.exists(path) and os.access(path, os.X_OK)
