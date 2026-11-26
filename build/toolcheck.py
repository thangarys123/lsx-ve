"""Check that every external program LSX-VE runs is installed on the target host.

The installed system is @core (mandatory + default packages of the comps group, minus the
kickstart's exclusions) plus the packages of build/packages.json and their dependencies. The
closure is computed from the repository metadata, so a missing tool fails the build (and
tests/verify_iso.py) instead of surfacing at run time on a host, as `tar` did for lxc-create.

REQUIRED lists the programs by name; one of /usr/bin, /usr/sbin, /bin or /sbin must provide it.
Keep it in sync when code starts calling a new program (tests/test_units.py checks the backend's
subprocess calls against it).
"""
import re
from typing import Dict, Iterable, List, Set, Tuple

# program -> who needs it (shown when it is missing)
REQUIRED: Dict[str, str] = {
    # LXC containers: lxc-* tools, and the "download" template (lxc-download checks tar, xz, wget)
    **{t: "LXC containers" for t in ("lxc-create", "lxc-start", "lxc-stop", "lxc-info", "lxc-ls", "lxc-destroy",
                                      "lxc-copy", "lxc-snapshot", "lxc-freeze", "lxc-unfreeze", "lxc-console")},
    "tar": "lxc-download template, rootfs tarball import",
    "xz": "lxc-download template (rootfs.tar.xz), .tar.xz import",
    "gzip": ".tar.gz import", "bzip2": ".tar.bz2 import", "zstd": ".tar.zst import",
    "wget": "lxc-download template",
    "getopt": "lxc-download template",
    "mktemp": "lxc-download template",
    # virtualization
    "qemu-img": "disk create/resize/convert, VM import, OVA export",
    "swtpm": "vTPM",
    "virt-host-validate": "hardware virtualization check",
    "dnsmasq": "libvirt NAT/isolated networks",
    # storage
    "lsblk": "disk inventory", "blkid": "disk inventory", "findmnt": "mount inventory",
    "parted": "disk formatting", "partprobe": "disk formatting", "sfdisk": "disk formatting",
    "wipefs": "disk formatting", "mkfs.xfs": "disk formatting", "mkfs.ext4": "disk formatting",
    "lvm": "LVM datastores", "pvs": "LVM", "vgs": "LVM", "lvs": "LVM", "pvcreate": "LVM", "vgcreate": "LVM",
    "mount": "NFS/iSCSI datastores", "umount": "datastores", "mount.nfs": "NFS datastores",
    "iscsiadm": "iSCSI datastores", "smartctl": "SMART", "udevadm": "disk formatting",
    # host and network
    "nmcli": "network configuration", "ip": "network inventory", "ping": "network test",
    "firewall-cmd": "firewall", "firewall-offline-cmd": "install script", "hostnamectl": "host name", "timedatectl": "time zone",
    "chronyc": "NTP status", "systemctl": "services", "journalctl": "logs",
    "modprobe": "KVM module", "dmidecode": "hardware inventory", "lspci": "PCI devices",
    "openssl": "TLS certificate", "chpasswd": "root password", "tuned-adm": "power policy",
    "snmpd": "SNMP agent", "rsyslogd": "remote syslog, LSX-VE logs", "logrotate": "log rotation",
    "restorecon": "SELinux labels", "semanage": "SELinux file contexts", "sestatus": "SELinux status",
    "rpm": "support bundle", "setsid": "agent", "sync": "power operations", "chown": "first boot",
    "sshd": "SSH",
    # install script (%post) and units
    "python3": "LSX-VE services", "systemd-sysusers": "install script", "systemd-tmpfiles": "install script",
    "usermod": "install script", "groupadd": "install script", "getent": "install script",
    "install": "install script", "find": "install script, log purge", "truncate": "log purge",
    "awk": "kickstart %post", "sed": "kickstart %post", "grep": "install script",
    "cat": "install script", "cp": "install script", "rm": "install script", "chmod": "install script",
}

BIN_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")


def ks_excludes(ks_text: str) -> List[str]:
    """Package exclusions ("-name" lines) of a kickstart's %packages section."""
    body = ks_text.split("%packages", 1)[1].split("%end", 1)[0]
    return [l.strip()[1:] for l in body.splitlines() if l.strip().startswith("-")]


def core_packages(comps_xml: str, ks_excludes: Iterable[str] = ()) -> List[str]:
    g = re.search(r"<group>\s*<id>core</id>.*?</group>", comps_xml, re.S)
    if not g:
        raise ValueError("comps has no core group")
    names = [n for t, n in re.findall(r'<packagereq type="(\w+)"[^>]*>([^<]+)<', g.group(0))
             if t in ("mandatory", "default")]
    pats = [re.compile(x.replace("*", ".*")) for x in ks_excludes]
    return [n for n in names if not any(p.fullmatch(n) for p in pats)]


def closure(pool, start: Iterable[str], exclude: Iterable[str] = ()) -> Tuple[Dict[str, object], List[str]]:
    """Approximate the set dnf would install: start names plus requirements, transitively.
    Newer packages (remote/extra repo) win over ISO ones with the same name."""
    from repotool import evr_cmp  # same directory
    excl = [re.compile(x) for x in exclude]
    installed: Dict[str, object] = {}
    missing: List[str] = []

    def by_name(name):
        cands = [p for p in pool.by_name.get(name, []) if p.arch in ("x86_64", "noarch")]
        best = None
        for p in cands:
            if best is None or evr_cmp(p.evr, best.evr) > 0:
                best = p
        return best

    queue = []
    for n in start:
        p = by_name(n)
        if p is None:
            missing.append(f"package {n} not in any repository")
        else:
            queue.append(p)
    while queue:
        p = queue.pop()
        if p.name in installed:
            continue
        installed[p.name] = p
        for r in p.requires:
            if r.name.startswith(("rpmlib(", "config(", "(")):
                continue  # rich/conditional dependencies: not needed for the binaries checked here
            provs = [q for q in pool.providers(r) if not any(x.match(q.name) for x in excl)]
            if not provs:
                continue
            if any(q.name in installed for q in provs):
                continue
            exact = [q for q in provs if q.name == r.name]
            pick = exact[0] if exact else sorted(provs, key=lambda q: (len(q.name), q.name))[0]
            queue.append(by_name(pick.name) or pick)
    return installed, missing


def missing_tools(installed: Dict[str, object], required: Dict[str, str] = REQUIRED) -> List[str]:
    have: Set[str] = set()
    for p in installed.values():
        for f in p.files:
            if f.startswith(BIN_DIRS):
                have.add(f.rsplit("/", 1)[1])
    return [f"{t} ({why})" for t, why in sorted(required.items()) if t not in have]
