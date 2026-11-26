"""Guest operating system catalogue used by the VM wizard.

Each entry maps a guest OS version to a device-model family (see
domxml.OS_TYPES) and to recommended virtual hardware. Recommendations are
defaults only; the wizard caps them to what the host has.
"""
from __future__ import annotations

from typing import Dict, List


def _e(id_: str, label: str, family: str, vcpus: int, mem_gib: float, disk_gib: int, firmware: str = "uefi",
       secure_boot: bool = False, tpm: bool = False) -> dict:
    return {"id": id_, "label": label, "family": family, "vcpus": vcpus, "memory_mib": int(mem_gib * 1024),
            "disk_gib": disk_gib, "firmware": firmware, "secure_boot": secure_boot, "tpm": tpm}


CATALOG: List[dict] = [
    # Microsoft Windows (Windows 11 / Server 2022+ require UEFI + Secure Boot + TPM 2.0)
    _e("win11", "Microsoft Windows 11 (64-bit)", "windows", 2, 4, 64, secure_boot=True, tpm=True),
    _e("win10", "Microsoft Windows 10 (64-bit)", "windows", 2, 4, 64),
    _e("win2k25", "Microsoft Windows Server 2025", "windows", 2, 4, 64, secure_boot=True, tpm=True),
    _e("win2k22", "Microsoft Windows Server 2022", "windows", 2, 4, 64, secure_boot=True, tpm=True),
    _e("win2k19", "Microsoft Windows Server 2019", "windows", 2, 4, 64),
    _e("win2k16", "Microsoft Windows Server 2016", "windows", 2, 4, 64),
    # Linux
    _e("rocky10", "Rocky Linux 10", "linux", 2, 2, 32),
    _e("rocky9", "Rocky Linux 9", "linux", 2, 2, 32),
    _e("rocky8", "Rocky Linux 8", "linux", 2, 2, 32),
    _e("alma10", "AlmaLinux 10", "linux", 2, 2, 32),
    _e("alma9", "AlmaLinux 9", "linux", 2, 2, 32),
    _e("rhel10", "Red Hat Enterprise Linux 10", "linux", 2, 2, 32),
    _e("rhel9", "Red Hat Enterprise Linux 9", "linux", 2, 2, 32),
    _e("rhel8", "Red Hat Enterprise Linux 8", "linux", 2, 2, 32),
    _e("ol9", "Oracle Linux 9", "linux", 2, 2, 32),
    _e("centos-stream10", "CentOS Stream 10", "linux", 2, 2, 32),
    _e("centos-stream9", "CentOS Stream 9", "linux", 2, 2, 32),
    _e("fedora", "Fedora Linux", "linux", 2, 4, 32),
    _e("debian13", "Debian 13 (trixie)", "linux", 2, 2, 20),
    _e("debian12", "Debian 12 (bookworm)", "linux", 2, 2, 20),
    _e("ubuntu24.04", "Ubuntu 24.04 LTS", "linux", 2, 4, 32),
    _e("ubuntu22.04", "Ubuntu 22.04 LTS", "linux", 2, 4, 32),
    _e("ubuntu", "Ubuntu (other release)", "linux", 2, 4, 32),
    _e("sles15", "SUSE Linux Enterprise Server 15", "linux", 2, 4, 32),
    _e("opensuse-leap", "openSUSE Leap", "linux", 2, 2, 32),
    _e("alpine", "Alpine Linux", "linux", 1, 0.5, 8),
    _e("arch", "Arch Linux", "linux", 2, 2, 20),
    _e("linux-other", "Other Linux (64-bit, kernel 4.x or later)", "linux", 1, 1, 16),
    # BSD
    _e("freebsd14", "FreeBSD 14", "bsd", 2, 2, 20),
    _e("freebsd13", "FreeBSD 13", "bsd", 2, 2, 20),
    _e("opnsense", "OPNsense / pfSense (FreeBSD)", "bsd", 2, 4, 20),
    _e("openbsd", "OpenBSD", "bsd", 1, 1, 16),
    _e("netbsd", "NetBSD", "bsd", 1, 1, 16),
    # Other
    _e("other64", "Other (64-bit)", "other", 1, 1, 16),
    _e("other-bios", "Other (legacy BIOS)", "other", 1, 1, 16, firmware="bios"),
]

BY_LABEL: Dict[str, dict] = {e["label"]: e for e in CATALOG}
