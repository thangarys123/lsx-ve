"""LXC system containers.

Runs inside lsxve-agent (root). Every operation validates its arguments and
calls the LXC command-line tools through common.proc (fixed binaries, argv
lists, no shell). Containers live in the LXC path from /etc/lxc/lxc.conf
(lsxve-init points it at datastore1 when that exists).

* Unprivileged by default: root's subordinate range 100000-165535 is mapped
  to uid/gid 0-65535 in the container.
* Each running container is an instance of the template unit
  lsxve-ct@<name>.service (lxc-start -F, Delegate=yes, clean lxc-stop on
  host shutdown), so restarting the agent never touches running containers.
* Sources: an image from images.linuxcontainers.org (LXC "download"
  template, fetched over HTTPS; this template version has no GPG check) or a rootfs tarball from the ISO
  library (/lsxve/iso).
* LSX-VE owns a small set of config keys (memory, CPU, autostart, network);
  everything else in the container's config file is left untouched.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import random
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..common import config, log, proc
from ..common import validate as v
from ..common.validate import ValidationError

LOG = log.setup("lsxve.ct")

LXC_CONF = Path("/etc/lxc/lxc.conf")
DEFAULT_PATH = "/var/lib/lxc"
IDMAP_BASE, IDMAP_SIZE = 100000, 65536
IMAGE_SERVER = "https://images.linuxcontainers.org"
IMAGE_INDEX = IMAGE_SERVER + "/meta/1.0/index-system"
ARCH = "amd64"
RE_CT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
RE_IMAGE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
RE_SNAP = re.compile(r"^snap[0-9]{1,4}$")
TARBALL_EXT = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2")
ACTIONS = ("start", "shutdown", "stop", "reboot", "freeze", "unfreeze")
STATES = {"RUNNING": "running", "STOPPED": "stopped", "FROZEN": "frozen", "STARTING": "starting",
          "STOPPING": "stopping", "ABORTING": "stopping", "FREEZING": "frozen", "THAWED": "running"}
META = "lsxve.json"
LXC = "/usr/bin/"
CONSOLE_MAX_SECONDS = 12 * 3600

# config keys LSX-VE manages; set_keys replaces exactly these
K_MEM = "lxc.cgroup2.memory.max"
K_SWAP = "lxc.cgroup2.memory.swap.max"
K_CPU = "lxc.cgroup2.cpu.max"
K_AUTO = "lxc.start.auto"
K_LINK = "lxc.net.0.link"
K_IP4 = "lxc.net.0.ipv4.address"
K_GW4 = "lxc.net.0.ipv4.gateway"


class CtError(Exception):
    """kind: not_found | conflict | unavailable"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# --------------------------------------------------------------------------- helpers
def available() -> bool:
    return proc.which(LXC + "lxc-start") and proc.which(LXC + "lxc-create")


def _require_lxc() -> None:
    if not available():
        raise CtError("unavailable", "LXC is not installed on this host")


def lxcpath() -> str:
    try:
        for line in LXC_CONF.read_text().splitlines():
            k, _, val = line.partition("=")
            if k.strip() == "lxc.lxcpath" and val.strip():
                return val.strip()
    except OSError:
        pass
    return DEFAULT_PATH


RESERVED = {"status", "images"}  # API sub-resources under /api/v1/containers/


def ct_name(n, field: str = "name") -> str:
    n = v.string(n, field, 1, 63, RE_CT)
    if n.lower() in RESERVED:
        raise ValidationError(field, f"'{n}' is reserved")
    return n


def _dir(name: str) -> Path:
    return Path(lxcpath()) / name


def _exists(name: str) -> bool:
    return (_dir(name) / "config").is_file()


def _need(name) -> str:
    name = ct_name(name)
    _require_lxc()
    if not _exists(name):
        raise CtError("not_found", f"Container '{name}' does not exist")
    return name


def _lxc(tool: str, name: str, *args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    return proc.run([LXC + tool, "-P", lxcpath(), "-n", name, *args], timeout=timeout, check=check)


def unit_name(name: str) -> str:
    return f"lsxve-ct@{name}.service"


def _cpus(items: List[Tuple[str, str]]) -> float:
    parts = (cfg_get(items, K_CPU, "") or "").split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]):
        return round(int(parts[0]) / int(parts[1]), 2)
    return 0.0


# --------------------------------------------------------------------------- config file
def parse_config(text: str) -> List[Tuple[str, str]]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, val = s.partition("=")
        out.append((k.strip(), val.strip()))
    return out


def cfg_get(items: List[Tuple[str, str]], key: str, default: Optional[str] = None) -> Optional[str]:
    val = default
    for k, x in items:
        if k == key:
            val = x
    return val


def rewrite_config(text: str, changes: Dict[str, Optional[str]]) -> str:
    """Drop every line whose key is in changes, then append the non-None values."""
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.partition("=")[0].strip() in changes:
            continue
        keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    added = [f"{k} = {val}" for k, val in changes.items() if val is not None]
    if added:
        keep += ["", "# managed by LSX-VE"] if "# managed by LSX-VE" not in keep else []
        keep += added
    return "\n".join(keep) + "\n"


def _write_atomic(path: Path, data: str, mode: int = 0o640) -> None:
    tmp = path.with_name(path.name + ".lsxve-tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def set_keys(name: str, changes: Dict[str, Optional[str]]) -> None:
    p = _dir(name) / "config"
    _write_atomic(p, rewrite_config(p.read_text(), changes))


def _read_meta(name: str) -> dict:
    try:
        return json.loads((_dir(name) / META).read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(name: str, meta: dict) -> None:
    _write_atomic(_dir(name) / META, json.dumps(meta, indent=1), 0o600)


def _rootfs_spec(items: List[Tuple[str, str]]) -> Tuple[str, str]:
    """("dir", directory) or ("loop", image file) from lxc.rootfs.path."""
    r = cfg_get(items, "lxc.rootfs.path", "") or ""
    if r.startswith("loop:"):
        return "loop", r[5:]
    return "dir", r[4:] if r.startswith("dir:") else r


def _rootfs(items: List[Tuple[str, str]]) -> str:
    """The rootfs directory, or the disk image file of a container with a fixed disk size."""
    return _rootfs_spec(items)[1]


DISK_IMAGE = "rootdev"     # name LXC itself uses for loop-backed containers
MNT_BASE = config.RUN_DIR / "ct-mnt"


@contextlib.contextmanager
def _rootfs_dir(name: str):
    """Directory with the container's files, for changes made by the host (root password, network
    settings). A disk image (fixed disk size) is mounted for the duration; that is only done while
    the container is stopped, because an ext4 image must never be mounted twice."""
    items = parse_config((_dir(name) / "config").read_text())
    kind, path = _rootfs_spec(items)
    if kind != "loop":
        yield path
        return
    st = _state(name)
    if st != "stopped":
        raise CtError("conflict", f"{name} is {st}; its disk image can only be changed while it is stopped")
    MNT_BASE.mkdir(parents=True, exist_ok=True)
    os.chmod(MNT_BASE, 0o700)
    mnt = MNT_BASE / name
    mnt.mkdir(mode=0o700, exist_ok=True)
    proc.run(["/usr/bin/mount", "-t", "ext4", "-o", "loop,nosuid,nodev", path, str(mnt)], timeout=120)
    try:
        yield str(mnt)
    finally:
        proc.run(["/usr/bin/umount", str(mnt)], timeout=120, check=False)
        try:
            mnt.rmdir()
        except OSError:
            pass


def disk_gib(items: List[Tuple[str, str]]) -> Optional[float]:
    """Size of the disk image in GiB (None: directory rootfs without a size limit)."""
    kind, path = _rootfs_spec(items)
    if kind != "loop":
        return None
    try:
        return round(os.stat(path).st_size / 1024 ** 3, 1)
    except OSError:
        return None


def random_mac() -> str:
    b = [0x00, 0x16, 0x3E] + [random.SystemRandom().randint(0, 255) for _ in range(3)]
    return ":".join(f"{x:02x}" for x in b)


def _bridges() -> List[str]:
    try:
        return sorted(n for n in os.listdir("/sys/class/net") if os.path.isdir(f"/sys/class/net/{n}/bridge"))
    except OSError:
        return []


def _safe_file(root: str, rel: str) -> Optional[str]:
    """Path of root/rel if no component (inside the untrusted rootfs) is a symlink."""
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
    return cur if stat.S_ISREG(os.lstat(cur).st_mode) else None


def os_release(rootfs: str) -> str:
    for rel in ("etc/os-release", "usr/lib/os-release"):
        p = _safe_file(rootfs, rel) if rootfs else None
        if not p:
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                data = f.read(8192)
        except OSError:
            continue
        kv = {}
        for line in data.splitlines():
            k, _, val = line.partition("=")
            kv[k.strip()] = val.strip().strip('"').strip("'")
        name = kv.get("PRETTY_NAME") or " ".join(x for x in (kv.get("NAME"), kv.get("VERSION_ID")) if x)
        if name:
            return name[:80]
    return ""


# --------------------------------------------------------------------------- state / info
def parse_info(text: str) -> dict:
    out: dict = {"ips": []}
    for line in text.splitlines():
        k, _, val = line.partition(":")
        k, val = k.strip(), val.strip()
        if not _ and k in STATES:
            # "lxc-info -s -H" prints only the bare value ("RUNNING"), without "State:"
            out["state"] = STATES[k]
        elif k == "State":
            out["state"] = STATES.get(val, val.lower())
        elif k == "PID":
            out["pid"] = int(val) if val.isdigit() else None
        elif k == "IP":
            out["ips"].append(val)
        elif k == "CPU use":
            out["cpu_ns"] = int(val) if val.isdigit() else None
        elif k == "Memory use":
            out["memory_bytes"] = int(val) if val.isdigit() else None
        elif k == "TX bytes":
            out["tx_bytes"] = int(val) if val.isdigit() else None
        elif k == "RX bytes":
            out["rx_bytes"] = int(val) if val.isdigit() else None
    return out


def cgroup2_usage(pid: int, name: str, proc_root: str = "/proc", cg_root: str = "/sys/fs/cgroup") -> dict:
    """CPU time and memory of a running container on a cgroup2-only host. LXC 4.0's lxc-info reads
    the cgroup v1 files ("no useable cpuacct controller") and reports neither on Rocky Linux 9."""
    try:
        with open(f"{proc_root}/{int(pid)}/cgroup", encoding="utf-8") as f:
            line = next((l.strip() for l in f if l.startswith("0::")), "")
    except (OSError, ValueError):
        return {}
    parts = line[3:].strip("/").split("/")
    want = f"lxc.payload.{name}"
    if want not in parts:
        return {}
    base = os.path.join(cg_root, *parts[:parts.index(want) + 1])
    out: dict = {}
    try:
        with open(os.path.join(base, "memory.current"), encoding="utf-8") as f:
            out["memory_bytes"] = int(f.read().strip())
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(base, "cpu.stat"), encoding="utf-8") as f:
            for l in f:
                k, _, val = l.partition(" ")
                if k == "usage_usec" and val.strip().isdigit():
                    out["cpu_ns"] = int(val) * 1000
    except OSError:
        pass
    return out


def _info(name: str) -> dict:
    cp = _lxc("lxc-info", name, "-H", timeout=15, check=False)
    if cp.returncode != 0:
        return {"state": "unknown", "ips": []}
    info = parse_info(cp.stdout)
    if info.get("pid") and (info.get("cpu_ns") is None or info.get("memory_bytes") is None):
        for k, val in cgroup2_usage(info["pid"], name).items():
            if info.get(k) is None:
                info[k] = val
    return info


def _state(name: str) -> str:
    # no -H: with a single filter, -H makes lxc-info print only "RUNNING" (parse_info copes with both)
    cp = _lxc("lxc-info", name, "-s", timeout=15, check=False)
    return parse_info(cp.stdout).get("state", "unknown") if cp.returncode == 0 else "unknown"


def _summary(name: str, with_info: bool = True) -> dict:
    d = _dir(name)
    items = parse_config((d / "config").read_text())
    meta = _read_meta(name)
    rootfs = _rootfs(items)
    mem = cfg_get(items, K_MEM)
    cpus = _cpus(items)
    info = _info(name) if with_info else {"state": "unknown", "ips": []}
    return {
        "name": name,
        "state": info.get("state", "unknown"),
        "pid": info.get("pid"),
        "ips": info.get("ips", []),
        "cpu_ns": info.get("cpu_ns"),
        "memory_used": info.get("memory_bytes"),
        "memory_mib": int(mem) // 1048576 if mem and mem.isdigit() else 0,
        "cpus": cpus,
        "autostart": cfg_get(items, K_AUTO, "0") == "1",
        "unprivileged": any(k == "lxc.idmap" for k, _ in items),
        "bridge": cfg_get(items, K_LINK, "") or "",
        "mac": cfg_get(items, "lxc.net.0.hwaddr", "") or "",
        "ipv4": cfg_get(items, K_IP4, "") or "",
        "gateway": cfg_get(items, K_GW4, "") or "",
        "os": (os_release(rootfs) if _rootfs_spec(items)[0] == "dir" else meta.get("os", "")) or meta.get("source_label", ""),
        "disk_gib": disk_gib(items),
        "description": meta.get("description", ""),
        "source": meta.get("source_label", ""),
        "created": meta.get("created"),
        "created_by": meta.get("created_by", ""),
        "snapshots": len(list_snapshots_raw(name)),
        "rootfs": rootfs,
    }


def list_containers() -> List[dict]:
    if not available():
        return []
    base = Path(lxcpath())
    out = []
    try:
        names = sorted(p.name for p in base.iterdir() if (p / "config").is_file() and RE_CT.match(p.name))
    except OSError:
        names = []
    for n in names:
        try:
            out.append(_summary(n))
        except (OSError, proc.CommandError, subprocess.TimeoutExpired) as e:
            out.append({"name": n, "state": "unknown", "error": str(e)})
    return out


def get(name: str) -> dict:
    name = _need(name)
    s = _summary(name)
    s["snapshot_list"] = list_snapshots_raw(name)
    s["config"] = (_dir(name) / "config").read_text()[:20000]
    s["unit"] = unit_name(name)
    return s


def status() -> dict:
    ver = ""
    if available():
        cp = proc.run([LXC + "lxc-info", "--version"], timeout=10, check=False)
        ver = cp.stdout.strip()
    return {"available": available(), "version": ver, "lxcpath": lxcpath(), "bridges": _bridges(),
            "image_server": IMAGE_SERVER, "arch": ARCH, "idmap": f"{IDMAP_BASE}:{IDMAP_SIZE}",
            "templates": list_tarballs()}


# --------------------------------------------------------------------------- images
_index_cache: Dict[str, object] = {"ts": 0.0, "data": None}
_index_lock = threading.Lock()


def parse_index(text: str) -> List[dict]:
    out = []
    for line in text.splitlines():
        parts = line.strip().split(";")
        if len(parts) < 6 or parts[2] != ARCH:
            continue
        distro, release, _arch, variant, build = parts[:5]
        if all(RE_IMAGE_PART.match(x) for x in (distro, release, variant)):
            out.append({"distro": distro, "release": release, "variant": variant, "build": build})
    out.sort(key=lambda x: (x["distro"], x["release"], x["variant"]))
    return out


def images(refresh: bool = False) -> dict:
    with _index_lock:
        if refresh or not _index_cache["data"] or time.time() - float(_index_cache["ts"]) > 3600:
            try:
                req = urllib.request.Request(IMAGE_INDEX, headers={"User-Agent": "lsxve-agent"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    text = r.read(4 * 1024 * 1024).decode("utf-8", "replace")
            except OSError as e:
                raise CtError("unavailable", f"Cannot reach the image server {IMAGE_SERVER} ({e}). "
                                             "The host needs Internet access, or use a rootfs tarball instead.")
            _index_cache["data"] = parse_index(text)
            _index_cache["ts"] = time.time()
        return {"server": IMAGE_SERVER, "fetched": _index_cache["ts"], "images": _index_cache["data"]}


def list_tarballs() -> List[dict]:
    out = []
    try:
        for p in sorted(config.ISO_DIR.iterdir()):
            if p.name.lower().endswith(TARBALL_EXT) and p.is_file() and not p.is_symlink():
                out.append({"name": p.name, "size": p.stat().st_size})
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- create
def _base_config(name: str, bridge: str, unprivileged: bool, include_common: bool) -> str:
    lines = [f"# LXC container {name}, created by LSX-VE"]
    if include_common:
        lines.append("lxc.include = /usr/share/lxc/config/common.conf")
        if unprivileged:
            lines.append("lxc.include = /usr/share/lxc/config/userns.conf")
    if unprivileged:
        lines += [f"lxc.idmap = u 0 {IDMAP_BASE} {IDMAP_SIZE}", f"lxc.idmap = g 0 {IDMAP_BASE} {IDMAP_SIZE}"]
    lines += ["lxc.net.0.type = veth", f"lxc.net.0.link = {bridge}", "lxc.net.0.flags = up",
              f"lxc.net.0.hwaddr = {random_mac()}", "lxc.net.0.name = eth0"]
    return "\n".join(lines) + "\n"


def _shift_ownership(rootfs: str, offset: int) -> None:
    """Move every file into the container's mapped uid/gid range (chown clears set-id bits: restore them)."""
    def one(path: str) -> None:
        st = os.lstat(path)
        uid, gid = st.st_uid, st.st_gid
        if uid < IDMAP_SIZE or gid < IDMAP_SIZE:
            os.lchown(path, uid + offset if uid < IDMAP_SIZE else uid, gid + offset if gid < IDMAP_SIZE else gid)
            if not stat.S_ISLNK(st.st_mode):
                os.chmod(path, stat.S_IMODE(st.st_mode))
    one(rootfs)
    for dp, dirs, files in os.walk(rootfs, followlinks=False):
        for n in dirs + files:
            one(os.path.join(dp, n))


# --------------------------------------------------------------------------- guest network
# LXC sets the address on eth0 before the container's init runs, but most images start their own
# DHCP client (systemd-networkd, netplan, ifupdown, NetworkManager) and replace it. For a static
# IPv4 address LSX-VE therefore also writes the container's own network configuration (like
# Proxmox VE). Files written by LSX-VE start with GUEST_MARK; with DHCP only those are rewritten.
GUEST_MARK = "# managed by LSX-VE (container network settings)"


def host_dns(text: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Name servers and search domains of the host (containers on the same bridge use them)."""
    if text is None:
        try:
            text = Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    ns: List[str] = []
    search: List[str] = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == "nameserver":
            try:
                a = ipaddress.ip_address(f[1])
            except ValueError:
                continue
            if not a.is_loopback and str(a) not in ns:
                ns.append(str(a))
        elif f and f[0] in ("search", "domain"):
            search = [d for d in f[1:] if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", d)]
    return ns[:3], search[:6]


def guest_net_files(probe: Dict[str, object], ip: Optional[str], gw: Optional[str],
                    dns: List[str], search: List[str]) -> List[Tuple[str, str, int, str]]:
    """What to write into the container: [(directory, file, mode, content)].

    probe: {"etc/systemd/network": bool (dir exists), "etc/netplan": bool,
            "etc/NetworkManager/system-connections": bool,
            "etc/network/interfaces": str|None (content), "etc/sysconfig/network-scripts/ifcfg-eth0": str|None,
            "etc/resolv.conf": str|None (None also when it is a symlink, e.g. systemd-resolved),
            "managed": set of relative paths that currently carry GUEST_MARK}
    ip: "a.b.c.d/nn" for a static address, None for DHCP."""
    managed = probe.get("managed") or set()
    out: List[Tuple[str, str, int, str]] = []
    static = bool(ip)
    addr = mask = ""
    prefix = 0
    if static:
        iface = ipaddress.ip_interface(ip)
        addr, prefix, mask = str(iface.ip), iface.network.prefixlen, str(iface.netmask)

    def want(rel: str, present: bool) -> bool:
        return (static and present) or (not static and rel in managed)

    def add(d: str, f: str, mode: int, lines: List[str]) -> None:
        out.append((d, f, mode, GUEST_MARK + "\n" + "\n".join(lines) + "\n"))

    # systemd-networkd: the first matching .network file (lexical order) wins; "10-" sorts first
    if want("etc/systemd/network/10-lsxve-eth0.network", bool(probe.get("etc/systemd/network"))):
        body = ["[Match]", "Name=eth0", "", "[Network]"]
        if static:
            body += [f"Address={ip}"] + ([f"Gateway={gw}"] if gw else []) + [f"DNS={d}" for d in dns]
            body += [f"Domains={' '.join(search)}"] if search else []
        else:
            body += ["DHCP=true"]
        add("etc/systemd/network", "10-lsxve-eth0.network", 0o644, body)
    # netplan (Ubuntu): files are merged in lexical order, so 99- overrides the image's 10-lxc.yaml
    if want("etc/netplan/99-lsxve.yaml", bool(probe.get("etc/netplan"))):
        y = ["network:", "  version: 2", "  ethernets:", "    eth0:"]
        if static:
            y += ["      dhcp4: false", f"      addresses: [{ip}]"]
            y += ["      routes:", "        - to: default", f"          via: {gw}"] if gw else []
            if dns or search:
                y += ["      nameservers:"]
                y += [f"        addresses: [{', '.join(dns)}]"] if dns else []
                y += [f"        search: [{', '.join(search)}]"] if search else []
        else:
            y += ["      dhcp4: true"]
        add("etc/netplan", "99-lsxve.yaml", 0o600, y)
    # ifupdown (Debian without networkd, Alpine): one file, rewritten as a whole
    ifup = probe.get("etc/network/interfaces")
    if want("etc/network/interfaces", isinstance(ifup, str) and "eth0" in ifup):
        t = ["auto lo", "iface lo inet loopback", "", "auto eth0"]
        if static:
            t += ["iface eth0 inet static", f"    address {addr}", f"    netmask {mask}"]
            t += [f"    gateway {gw}"] if gw else []
        else:
            t += ["iface eth0 inet dhcp"]
        add("etc/network", "interfaces", 0o644, t)
    # network-scripts (EL images; also read by NetworkManager's ifcfg plugin)
    has_ifcfg = probe.get("etc/sysconfig/network-scripts/ifcfg-eth0") is not None
    if want("etc/sysconfig/network-scripts/ifcfg-eth0", has_ifcfg):
        t = ["DEVICE=eth0", "NAME=eth0", "TYPE=Ethernet", "ONBOOT=yes"]
        if static:
            t += ["BOOTPROTO=none", f"IPADDR={addr}", f"PREFIX={prefix}"] + ([f"GATEWAY={gw}"] if gw else [])
            t += [f"DNS{i}={d}" for i, d in enumerate(dns, 1)]
            t += [f'DOMAIN="{" ".join(search)}"'] if search else []
        else:
            t += ["BOOTPROTO=dhcp"]
        add("etc/sysconfig/network-scripts", "ifcfg-eth0", 0o644, t)
    # NetworkManager keyfile (newer EL/Fedora images without ifcfg)
    if want("etc/NetworkManager/system-connections/eth0.nmconnection",
            bool(probe.get("etc/NetworkManager/system-connections")) and not has_ifcfg):
        t = ["[connection]", "id=eth0", "type=ethernet", "interface-name=eth0", "", "[ipv4]"]
        if static:
            t += ["method=manual", f"address1={ip}" + (f",{gw}" if gw else "")]
            t += [f"dns={';'.join(dns)};"] if dns else []
            t += [f"dns-search={';'.join(search)};"] if search else []
        else:
            t += ["method=auto"]
        t += ["", "[ipv6]", "method=auto"]
        add("etc/NetworkManager/system-connections", "eth0.nmconnection", 0o600, t)
    # resolv.conf only when it is a plain file (a symlink means systemd-resolved, fed by networkd)
    if static and dns and probe.get("etc/resolv.conf") is not None:
        add("etc", "resolv.conf", 0o644, [f"nameserver {d}" for d in dns] + ([f"search {' '.join(search)}"] if search else []))
    return out


def _guest_dir(rootfs: str, rel: str) -> Optional[int]:
    """fd of rootfs/rel, opened component by component without following symlinks (the rootfs
    belongs to the container and may contain hostile links)."""
    fd = os.open(rootfs, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in [p for p in rel.split("/") if p]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except OSError:
        os.close(fd)
        return None


def _guest_read(rootfs: str, rel: str) -> Optional[str]:
    """Content of a regular file (not a symlink) in the container, or None."""
    d, _, name = rel.rpartition("/")
    dfd = _guest_dir(rootfs, d)
    if dfd is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read(65536).decode("utf-8", errors="replace")


def _guest_write(rootfs: str, d: str, name: str, mode: int, data: str, uid: int) -> None:
    dfd = _guest_dir(rootfs, d)
    if dfd is None:
        raise OSError(f"{d} is missing or a symlink in the container")
    tmp = f".{name}.lsxve-tmp"
    try:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fchown(f.fileno(), uid, uid)
            os.fchmod(f.fileno(), mode)
        # rename replaces a symlink itself, never its target
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def apply_guest_network(name: str) -> List[str]:
    """Write the container's own network configuration from the LSX-VE settings (static/DHCP).
    A container with a disk image gets it at the next start if it is running now."""
    items = parse_config((_dir(name) / "config").read_text())
    if _rootfs_spec(items)[0] == "loop" and _state(name) != "stopped":
        return []
    with _rootfs_dir(name) as rootfs:
        return _apply_guest_network_in(name, rootfs, items)


def _apply_guest_network_in(name: str, rootfs: str, items: List[Tuple[str, str]]) -> List[str]:
    if not rootfs or not os.path.isdir(rootfs):
        return []
    ip = cfg_get(items, K_IP4) or None
    gw = cfg_get(items, K_GW4) or None
    managed = set()
    probe: Dict[str, object] = {"managed": managed}
    for rel in ("etc/systemd/network", "etc/netplan", "etc/NetworkManager/system-connections"):
        fd = _guest_dir(rootfs, rel)
        probe[rel] = fd is not None
        if fd is not None:
            os.close(fd)
    for rel in ("etc/network/interfaces", "etc/sysconfig/network-scripts/ifcfg-eth0", "etc/resolv.conf",
                "etc/systemd/network/10-lsxve-eth0.network", "etc/netplan/99-lsxve.yaml",
                "etc/NetworkManager/system-connections/eth0.nmconnection"):
        text = _guest_read(rootfs, rel)
        probe[rel] = text
        if text is not None and text.startswith(GUEST_MARK):
            managed.add(rel)
    dns, search = host_dns()
    uid = IDMAP_BASE if any(k == "lxc.idmap" for k, _ in items) else 0
    written = []
    for d, fname, mode, data in guest_net_files(probe, ip, gw, dns, search):
        try:
            _guest_write(rootfs, d, fname, mode, data, uid)
            written.append(f"/{d}/{fname}")
        except OSError as e:
            LOG.warning("container %s: cannot write /%s/%s: %s", name, d, fname, e)
    if written:
        LOG.info("container %s: network configuration (%s) written to %s", name, ip or "DHCP", ", ".join(written))
    return written


def set_root_password(rootfs: str, password: str) -> None:
    import crypt  # Python 3.9 on the host
    p = _safe_file(rootfs, "etc/shadow")
    if not p:
        raise ValidationError("root_password", "the container image has no /etc/shadow")
    h = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    fd = os.open(p, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        lines = f.read().splitlines()
        done = False
        for i, line in enumerate(lines):
            parts = line.split(":")
            if parts and parts[0] == "root" and len(parts) >= 2:
                parts[1] = h
                parts[2:3] = [str(int(time.time() // 86400))] if len(parts) > 2 else []
                lines[i] = ":".join(parts)
                done = True
        if not done:
            raise ValidationError("root_password", "the container image has no root entry in /etc/shadow")
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValidationError("spec", "must be an object")
    src = spec.get("source")
    if not isinstance(src, dict):
        raise ValidationError("source", "must be an object")
    kind = v.choice(src.get("type"), "source.type", ("download", "tarball"))
    out = {"name": ct_name(spec.get("name")), "description": v.text(spec.get("description", ""), "description", 1000)}
    if kind == "download":
        out["source"] = {"type": "download",
                         "distro": v.string(src.get("distro"), "source.distro", 1, 40, RE_IMAGE_PART),
                         "release": v.string(src.get("release"), "source.release", 1, 40, RE_IMAGE_PART),
                         "variant": v.string(src.get("variant", "default"), "source.variant", 1, 40, RE_IMAGE_PART)}
    else:
        f = v.string(src.get("file"), "source.file", 1, 200, v.RE_FILENAME)
        if not f.lower().endswith(TARBALL_EXT):
            raise ValidationError("source.file", "must be a .tar, .tar.gz, .tar.xz, .tar.zst or .tar.bz2 rootfs archive")
        out["source"] = {"type": "tarball", "file": f}
    out["memory_mib"] = v.integer(spec.get("memory_mib", 512), "memory_mib", 0, 4 * 1024 * 1024)
    # 0: rootfs is a directory on the datastore (no limit); otherwise an ext4 disk image of that size
    out["disk_gib"] = v.integer(spec.get("disk_gib", 0) or 0, "disk_gib", 0, 64 * 1024)
    out["cpus"] = v.integer(spec.get("cpus", 1), "cpus", 0, 1024)
    out["bridge"] = v.ifname(spec.get("bridge"), "bridge")
    ip = spec.get("ipv4") or "dhcp"
    out["ipv4"] = "dhcp" if ip == "dhcp" else v.ipv4_interface(ip, "ipv4")
    out["gateway"] = v.ip_address(spec["gateway"], "gateway", 4) if spec.get("gateway") and out["ipv4"] != "dhcp" else ""
    out["unprivileged"] = v.boolean(spec.get("unprivileged", True), "unprivileged")
    out["autostart"] = v.boolean(spec.get("autostart", False), "autostart")
    out["start"] = v.boolean(spec.get("start", False), "start")
    out["root_password"] = v.password(spec["root_password"], "root_password") if spec.get("root_password") else ""
    out["created_by"] = v.string(spec.get("created_by", ""), "created_by", 0, 64)
    return out


def _resource_keys(memory_mib: int, cpus: int) -> Dict[str, Optional[str]]:
    return {K_MEM: str(memory_mib * 1048576) if memory_mib else None,
            K_SWAP: "0" if memory_mib else None,
            K_CPU: f"{cpus * 100000} 100000" if cpus else None}


def create(spec: dict) -> dict:
    s = validate_spec(spec)
    _require_lxc()
    name = s["name"]
    if _exists(name) or _dir(name).exists():
        raise CtError("conflict", f"A container named '{name}' already exists")
    if s["bridge"] not in _bridges():
        raise ValidationError("bridge", f"bridge {s['bridge']} does not exist on this host")
    base = Path(lxcpath())
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o755)
    if s["disk_gib"]:
        free = shutil.disk_usage(base).free
        if s["disk_gib"] * 1024 ** 3 > free:
            raise ValidationError("disk_gib", f"only {free / 1024 ** 3:.1f} GiB are free on the datastore of {base}")
    d = _dir(name)
    label = ""
    try:
        if s["source"]["type"] == "download":
            src = s["source"]
            known = images()["images"]
            if not any(i["distro"] == src["distro"] and i["release"] == src["release"] and i["variant"] == src["variant"]
                       for i in known):
                raise ValidationError("source", f"image {src['distro']}/{src['release']}/{src['variant']} ({ARCH}) "
                                                "is not offered by the image server")
            tmp = config.RUN_DIR / f"ct-{name}.conf"
            tmp.write_text(_base_config(name, s["bridge"], s["unprivileged"], include_common=False))
            try:
                backing = (["-B", "loop", "--fstype", "ext4", "--fssize", f"{s['disk_gib']}G"] if s["disk_gib"]
                           else ["-B", "dir"])
                _lxc("lxc-create", name, "-t", "download", *backing, "-f", str(tmp), "--",
                     "-d", src["distro"], "-r", src["release"], "-a", ARCH, "--variant", src["variant"],
                     timeout=1800)
            finally:
                tmp.unlink(missing_ok=True)
            label = f"{src['distro']} {src['release']} ({src['variant']})"
        else:
            f = config.ISO_DIR / s["source"]["file"]
            if not f.is_file() or f.is_symlink() or f.parent != config.ISO_DIR:
                raise ValidationError("source.file", "archive not found in the ISO library")
            d.mkdir(mode=0o755)
            cfg = _base_config(name, s["bridge"], s["unprivileged"], include_common=True)
            if s["disk_gib"]:
                img = d / DISK_IMAGE
                with open(img, "wb") as fh:          # sparse: blocks are allocated as the container writes
                    fh.truncate(s["disk_gib"] * 1024 ** 3)
                os.chmod(img, 0o600)
                proc.run(["/usr/sbin/mkfs.ext4", "-q", "-F", "-L", name[:16], str(img)], timeout=600)
                cfg += f"lxc.rootfs.path = loop:{img}\nlxc.uts.name = {name}\n"
            else:
                (d / "rootfs").mkdir(mode=0o755)
                cfg += f"lxc.rootfs.path = dir:{d / 'rootfs'}\nlxc.uts.name = {name}\n"
            _write_atomic(d / "config", cfg)
            with _rootfs_dir(name) as rootfs:
                proc.run(["/usr/bin/tar", "-xpf", str(f), "-C", rootfs, "--numeric-owner",
                          "--xattrs", "--xattrs-exclude=security.selinux"], timeout=1800)
                if s["unprivileged"]:
                    _shift_ownership(rootfs, IDMAP_BASE)
            label = s["source"]["file"]
        changes = _resource_keys(s["memory_mib"], s["cpus"])
        changes.update({K_AUTO: "1" if s["autostart"] else None,
                        K_IP4: s["ipv4"] if s["ipv4"] != "dhcp" else None,
                        K_GW4: s["gateway"] or None,
                        K_LINK: s["bridge"]})
        set_keys(name, changes)
        with _rootfs_dir(name) as rootfs:
            if s["root_password"]:
                set_root_password(rootfs, s["root_password"])
            os_name = os_release(rootfs)
        _write_meta(name, {"description": s["description"], "created": time.time(), "created_by": s["created_by"],
                           "source": s["source"], "source_label": label, "os": os_name,
                           "disk_gib": s["disk_gib"] or None})
    except BaseException:
        if d.exists() and d.parent == Path(lxcpath()):
            LOG.warning("container %s: creation failed, removing %s", name, d)
            proc.run([LXC + "lxc-destroy", "-P", lxcpath(), "-n", name], timeout=300, check=False)
            shutil.rmtree(d, ignore_errors=True)
        raise
    LOG.info("container %s created from %s", name, label)
    if s["start"]:
        action(name, "start")
    return {"message": f"Container {name} created", **_summary(name)}


# --------------------------------------------------------------------------- lifecycle
def _wait(name: str, states: Tuple[str, ...], timeout: float) -> str:
    end = time.time() + timeout
    st = _state(name)
    while st not in states and time.time() < end:
        time.sleep(0.5)
        st = _state(name)
    return st


def lxc_log(name: str) -> Path:
    """lxc-start's own log (warnings and errors), written by lsxve-ct@.service."""
    return Path("/var/log/lsxve") / f"lxc-{name}.log"


def _lxc_log_tail(name: str, lines: int = 12) -> str:
    try:
        text = lxc_log(name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    keep = [l for l in text.splitlines() if " ERROR " in l or " WARN " in l][-lines:]
    return (" LXC log: " + " | ".join(l.split(" - ", 1)[-1] for l in keep))[-800:] if keep else ""


def _start(name: str) -> None:
    unit = unit_name(name)
    try:
        apply_guest_network(name)
    except OSError as e:  # never block a start because of the guest's files
        LOG.warning("container %s: network configuration not written: %s", name, e)
    proc.run(["/usr/bin/systemctl", "reset-failed", unit], timeout=15, check=False)
    proc.run(["/usr/bin/systemctl", "start", "--no-block", unit], timeout=30)


def action(name: str, action: str) -> dict:
    name = _need(name)
    action = v.choice(action, "action", ACTIONS)
    st = _state(name)
    if action == "start":
        if st == "running":
            return {"state": st, "message": f"{name} is already running"}
        if st == "frozen":
            raise CtError("conflict", f"{name} is frozen; resume it instead")
        _start(name)
        st = _wait(name, ("running",), 30)
        if st != "running":
            cp = proc.run(["/usr/bin/journalctl", "-u", unit_name(name), "-n", "20", "--no-pager", "-o", "cat"],
                          timeout=15, check=False)
            raise CtError("conflict", f"{name} did not start (state {st}). {cp.stdout.strip()[-400:]}"
                                      f"{_lxc_log_tail(name)}")
    elif action in ("shutdown", "stop", "reboot"):
        if st == "stopped":
            return {"state": st, "message": f"{name} is not running"}
        if action == "shutdown":
            _lxc("lxc-stop", name, "-t", "90", timeout=120)
        elif action == "stop":
            _lxc("lxc-stop", name, "-k", timeout=60)
        else:
            # shut down and start through _start(): network settings changed meanwhile are written
            _lxc("lxc-stop", name, "-t", "90", timeout=120)
            _wait(name, ("stopped",), 30)
            _start(name)
            st = _wait(name, ("running",), 30)
            return {"state": st, "message": f"{name} restarted"}
        st = _wait(name, ("stopped",), 30)
    elif action == "freeze":
        if st != "running":
            raise CtError("conflict", f"{name} is not running")
        _lxc("lxc-freeze", name, timeout=60)
        st = _state(name)
    else:
        if st != "frozen":
            raise CtError("conflict", f"{name} is not frozen")
        _lxc("lxc-unfreeze", name, timeout=60)
        st = _state(name)
    return {"state": st, "message": f"{name}: {action} done, now {st}"}


def _need_stopped(name: str, what: str) -> None:
    st = _state(name)
    if st != "stopped":
        raise CtError("conflict", f"Stop container {name} before you {what} (it is {st})")


def update(name: str, changes: dict) -> dict:
    name = _need(name)
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("body", "no changes supplied")
    keys: Dict[str, Optional[str]] = {}
    items = parse_config((_dir(name) / "config").read_text())
    if "memory_mib" in changes or "cpus" in changes:
        mem = v.integer(changes.get("memory_mib", int(cfg_get(items, K_MEM, "0") or 0) // 1048576), "memory_mib",
                        0, 4 * 1024 * 1024)
        cpus = v.integer(changes.get("cpus", int(round(_cpus(items)))), "cpus", 0, 1024)
        keys.update(_resource_keys(mem, cpus))
    if "autostart" in changes:
        keys[K_AUTO] = "1" if v.boolean(changes["autostart"], "autostart") else None
    if "bridge" in changes:
        br = v.ifname(changes["bridge"], "bridge")
        if br not in _bridges():
            raise ValidationError("bridge", f"bridge {br} does not exist on this host")
        keys[K_LINK] = br
    if "ipv4" in changes:
        ip = changes["ipv4"] or "dhcp"
        keys[K_IP4] = None if ip == "dhcp" else v.ipv4_interface(ip, "ipv4")
        gw = changes.get("gateway")
        keys[K_GW4] = v.ip_address(gw, "gateway", 4) if gw and keys[K_IP4] else None
    if keys:
        set_keys(name, keys)
    if K_IP4 in keys or K_GW4 in keys:
        try:
            apply_guest_network(name)
        except OSError as e:
            LOG.warning("container %s: network configuration not written: %s", name, e)
    if "description" in changes:
        meta = _read_meta(name)
        meta["description"] = v.text(changes["description"], "description", 1000)
        _write_meta(name, meta)
    if "root_password" in changes:
        _need_stopped(name, "change the root password")
        pw = v.password(changes["root_password"], "root_password")
        with _rootfs_dir(name) as rootfs:
            set_root_password(rootfs, pw)
    running = _state(name) in ("running", "frozen")
    msg = f"Container {name} updated"
    if running and (set(keys) - {K_AUTO}):
        msg += " - restart it to apply resource and network changes"
    return {"message": msg, **_summary(name)}


def delete(name: str) -> dict:
    name = _need(name)
    _need_stopped(name, "delete it")
    _lxc("lxc-destroy", name, "-s", timeout=1800)
    proc.run(["/usr/bin/systemctl", "reset-failed", unit_name(name)], timeout=15, check=False)
    LOG.info("container %s deleted", name)
    return {"message": f"Container {name} deleted"}


def clone(name: str, new_name: str) -> dict:
    name = _need(name)
    new = ct_name(new_name, "new_name")
    if _exists(new) or _dir(new).exists():
        raise CtError("conflict", f"A container named '{new}' already exists")
    _need_stopped(name, "clone it")
    _lxc("lxc-copy", name, "-N", new, timeout=3600)
    meta = _read_meta(name)
    meta.update({"created": time.time(), "cloned_from": name, "description": meta.get("description", "")})
    _write_meta(new, meta)
    set_keys(new, {K_AUTO: None})
    return {"message": f"Container {name} cloned to {new}", **_summary(new)}


# --------------------------------------------------------------------------- snapshots
def list_snapshots_raw(name: str) -> List[dict]:
    d = _dir(name) / "snaps"
    out = []
    try:
        entries = sorted((p for p in d.iterdir() if RE_SNAP.match(p.name) and p.is_dir()),
                         key=lambda p: int(p.name[4:]))
    except OSError:
        return []
    for p in entries:
        def rd(f: str) -> str:
            try:
                return (p / f).read_text(errors="replace").strip()[:500]
            except OSError:
                return ""
        out.append({"name": p.name, "created": rd("ts"), "comment": rd("comment")})
    return out


def snapshots(name: str) -> List[dict]:
    return list_snapshots_raw(_need(name))


def snapshot_create(name: str, comment: str = "") -> dict:
    name = _need(name)
    comment = v.text(comment or "", "comment", 500)
    _need_stopped(name, "take a snapshot")
    args = []
    tmp = None
    if comment:
        tmp = config.RUN_DIR / f"ct-{name}.comment"
        tmp.write_text(comment + "\n")
        args = ["-c", str(tmp)]
    try:
        _lxc("lxc-snapshot", name, *args, timeout=3600)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    snaps = list_snapshots_raw(name)
    return {"message": f"Snapshot {snaps[-1]['name'] if snaps else ''} of {name} created", "snapshots": snaps}


def _snap(name: str, snap) -> str:
    snap = v.string(snap, "snapshot", 5, 8, RE_SNAP)
    if not any(s["name"] == snap for s in list_snapshots_raw(name)):
        raise CtError("not_found", f"Snapshot {snap} of {name} does not exist")
    return snap


def snapshot_restore(name: str, snapshot: str) -> dict:
    name = _need(name)
    snap = _snap(name, snapshot)
    _need_stopped(name, "restore a snapshot")
    _lxc("lxc-snapshot", name, "-r", snap, timeout=3600)
    return {"message": f"{name} restored to {snap}"}


def snapshot_delete(name: str, snapshot: str) -> dict:
    name = _need(name)
    snap = _snap(name, snapshot)
    _lxc("lxc-snapshot", name, "-d", snap, timeout=1800)
    return {"message": f"Snapshot {snap} of {name} deleted"}


# --------------------------------------------------------------------------- console
def open_console(name: str, cols: int = 80, rows: int = 24) -> Tuple[dict, int]:
    """Start lxc-console on a new pty; returns (info, master fd) - the caller passes the fd to the API."""
    import fcntl
    import termios
    name = _need(name)
    cols = v.integer(cols, "cols", 10, 1000)
    rows = v.integer(rows, "rows", 5, 500)
    if _state(name) != "running":
        raise CtError("conflict", f"Container {name} is not running")
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        p = proc.spawn_on_tty(["/usr/bin/setsid", "--ctty", "--wait", LXC + "lxc-console", "-P", lxcpath(), "-n", name,
                               "-t", "0"], slave)
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)

    def reap():
        try:
            p.wait(CONSOLE_MAX_SECONDS)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    threading.Thread(target=reap, name=f"console-{name}", daemon=True).start()
    LOG.info("console opened for container %s (pid %s)", name, p.pid)
    return {"name": name, "escape": "Ctrl+a q"}, master


# --------------------------------------------------------------------------- boot
def autostart_all() -> None:
    """Start containers with lxc.start.auto = 1 once per boot (called from a thread in lsxve-agent)."""
    marker = config.RUN_DIR / "ct-autostart.done"
    if not available() or marker.exists():
        return
    marker.write_text(str(time.time()))
    for c in list_containers():
        if not c.get("autostart") or c.get("state") != "stopped":
            continue
        br = c.get("bridge")
        end = time.time() + 120
        if br and br not in _bridges():
            proc.run(["/usr/bin/systemctl", "start", "virtnetworkd.service"], timeout=60, check=False)
        while br and br not in _bridges() and time.time() < end:
            time.sleep(2)
        try:
            action(c["name"], "start")
            LOG.info("autostarted container %s", c["name"])
        except Exception as e:  # noqa: BLE001 - keep going with the others
            LOG.error("autostart of container %s failed: %s", c["name"], e)
