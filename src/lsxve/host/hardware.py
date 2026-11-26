"""Hardware detection from /proc and /sys (read-only, unprivileged)."""
from __future__ import annotations

import json
import os
import platform
import socket
from pathlib import Path
from typing import Dict, List, Optional

from ..common import proc


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def cpu_info() -> dict:
    model, flags, vendor = "", set(), ""
    sockets, cores_per_socket = set(), {}
    threads = 0
    mhz = 0.0
    phys = None
    for block in _read("/proc/cpuinfo").split("\n\n"):
        d = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.strip()] = v.strip()
        if "processor" not in d:
            continue
        threads += 1
        model = model or d.get("model name", "")
        vendor = vendor or d.get("vendor_id", "")
        flags = flags or set(d.get("flags", "").split())
        phys = d.get("physical id", "0")
        sockets.add(phys)
        cores_per_socket[phys] = int(d.get("cpu cores", "1") or 1)
        try:
            mhz = max(mhz, float(d.get("cpu MHz", "0")))
        except ValueError:
            pass
    cores = sum(cores_per_socket.values()) or threads
    if threads == 0:  # non-Linux dev machine
        threads = os.cpu_count() or 1
        cores = threads
        model = platform.processor() or "Unknown CPU"
    virt = "vmx" in flags and "Intel VT-x" or ("svm" in flags and "AMD-V" or None)
    return {
        "model": model, "vendor": vendor, "sockets": max(1, len(sockets)), "cores": cores, "threads": threads,
        "mhz": round(mhz), "virtualization": virt,
        "features": sorted(f for f in flags if f in {"vmx", "svm", "ept", "npt", "aes", "avx", "avx2", "avx512f",
                                                       "x2apic", "pdpe1gb", "hypervisor", "sse4_2"}),
        "nested_hypervisor": "hypervisor" in flags,
    }


def memory_info() -> dict:
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            m[parts[0].rstrip(":")] = int(parts[1]) * 1024
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    return {"total": total, "available": avail, "used": max(0, total - avail),
            "hugepages_total": m.get("HugePages_Total", 0), "swap_total": m.get("SwapTotal", 0),
            "swap_free": m.get("SwapFree", 0)}


def dmi() -> dict:
    base = "/sys/class/dmi/id/"
    keys = ["sys_vendor", "product_name", "product_serial", "product_uuid", "board_vendor", "board_name",
            "bios_vendor", "bios_version", "bios_date", "chassis_type"]
    out = {k: _read(base + k) for k in keys}
    # product_serial / product_uuid are root-only on most systems; leave empty if unreadable
    return out


LAPTOP_CHASSIS = {8, 9, 10, 11, 14, 30, 31, 32}  # SMBIOS: portable, laptop, notebook, hand held, sub notebook,
#                                                 tablet, convertible, detachable


def is_laptop() -> bool:
    """A notebook: SMBIOS chassis type, or a lid switch / internal battery as fallback."""
    try:
        if int(_read("/sys/class/dmi/id/chassis_type", "0") or 0) in LAPTOP_CHASSIS:
            return True
    except ValueError:
        pass
    if os.path.isdir("/proc/acpi/button/lid") and os.listdir("/proc/acpi/button/lid"):
        return True
    try:
        return any(n.startswith("BAT") for n in os.listdir("/sys/class/power_supply"))
    except OSError:
        return False


def backlights() -> List[str]:
    try:
        return [os.path.join("/sys/class/backlight", n) for n in sorted(os.listdir("/sys/class/backlight"))]
    except OSError:
        return []


def firmware_mode() -> str:
    return "UEFI" if os.path.isdir("/sys/firmware/efi") else "BIOS"


def secure_boot() -> Optional[bool]:
    try:
        for p in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
            return p.read_bytes()[-1] == 1
    except OSError:
        pass
    return None


def iommu_enabled() -> bool:
    try:
        return any(Path("/sys/kernel/iommu_groups").iterdir())
    except OSError:
        return False


def kvm_status() -> dict:
    present = os.path.exists("/dev/kvm")
    module = None
    for m in ("kvm_intel", "kvm_amd"):
        if os.path.isdir(f"/sys/module/{m}"):
            module = m
    nested = _read(f"/sys/module/{module}/parameters/nested") if module else ""
    return {"dev_kvm": present, "module": module, "nested": nested in ("Y", "1"),
            "accelerated": present}


def kernel() -> dict:
    u = platform.uname()
    return {"system": u.system, "release": u.release, "machine": u.machine}


def os_release() -> dict:
    out = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip('"')
    return out


def uptime() -> float:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0.0


def boot_time() -> float:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime"):
            return float(line.split()[1])
    return 0.0


def hostname() -> str:
    return socket.gethostname()


def sensors() -> List[dict]:
    """Temperatures / fans / voltages from hwmon (hardware health where available)."""
    out = []
    root = Path("/sys/class/hwmon")
    if not root.is_dir():
        return out
    for hw in sorted(root.iterdir()):
        chip = _read(str(hw / "name"), hw.name)
        for f in sorted(hw.glob("*_input")):
            kind = f.name.split("_")[0].rstrip("0123456789")
            idx = f.name[len(kind):].split("_")[0]
            label = _read(str(hw / f"{kind}{idx}_label"), f"{kind}{idx}")
            raw = _read(str(f))
            try:
                v = int(raw)
            except ValueError:
                continue
            if kind == "temp":
                value, unit = v / 1000.0, "°C"
                crit = _read(str(hw / f"temp{idx}_crit"))
                maxv = _read(str(hw / f"temp{idx}_max"))
                limit = int(crit or maxv) / 1000.0 if (crit or maxv).lstrip("-").isdigit() else None
                status = "critical" if limit and value >= limit else ("warning" if limit and value >= limit - 10 else "normal")
            elif kind == "fan":
                value, unit, status, limit = float(v), "RPM", "normal", None
            elif kind == "in":
                value, unit, status, limit = v / 1000.0, "V", "normal", None
            elif kind == "power":
                value, unit, status, limit = v / 1e6, "W", "normal", None
            else:
                continue
            out.append({"chip": chip, "sensor": label, "type": kind, "value": round(value, 2), "unit": unit,
                        "limit": limit, "status": status})
    return out


def pci_devices() -> List[dict]:
    out = []
    root = Path("/sys/bus/pci/devices")
    if not root.is_dir():
        return out
    classes = {"01": "Storage controller", "02": "Network controller", "03": "Display controller",
               "04": "Multimedia", "06": "Bridge", "0c": "Serial bus"}
    for d in sorted(root.iterdir()):
        cls = _read(str(d / "class"))[2:4]
        if cls not in ("01", "02", "03", "0c"):
            continue
        drv = os.path.basename(os.readlink(d / "driver")) if (d / "driver").exists() else ""
        out.append({"address": d.name, "class": classes.get(cls, cls), "vendor_id": _read(str(d / "vendor")),
                    "device_id": _read(str(d / "device")), "driver": drv,
                    "iommu_group": os.path.basename(os.readlink(d / "iommu_group")) if (d / "iommu_group").exists() else None})
    return out


def physical_nics() -> List[dict]:
    out = []
    root = Path("/sys/class/net")
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        name = d.name
        if name == "lo":
            continue
        is_phys = (d / "device").exists()
        speed = _read(str(d / "speed"))
        try:
            speed_v = int(speed)
        except ValueError:
            speed_v = None
        drv = ""
        if (d / "device" / "driver").exists():
            drv = os.path.basename(os.readlink(d / "device" / "driver"))
        kind = "physical" if is_phys else ("bridge" if (d / "bridge").exists() else
                                           "bond" if (d / "bonding").exists() else
                                           "vlan" if Path(f"/proc/net/vlan/{name}").exists() else
                                           "tap" if (d / "tun_flags").exists() else "virtual")
        out.append({
            "name": name, "kind": kind, "mac": _read(str(d / "address")), "mtu": int(_read(str(d / "mtu"), "0") or 0),
            "operstate": _read(str(d / "operstate")), "carrier": _read(str(d / "carrier")) == "1",
            "speed_mbps": speed_v if speed_v and speed_v > 0 else None, "duplex": _read(str(d / "duplex")) or None,
            "driver": drv,
            "master": os.path.basename(os.readlink(d / "master")) if (d / "master").exists() else None,
        })
    return out


def block_devices() -> List[dict]:
    """Physical disks and their partitions (lsblk JSON, bytes)."""
    if not proc.which("/usr/bin/lsblk"):
        return []
    cp = proc.run(["/usr/bin/lsblk", "-J", "-b", "-o",
                   "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,VENDOR,TRAN,ROTA,RO,RM,FSTYPE,LABEL,UUID,MOUNTPOINT,PKNAME,WWN,HCTL,STATE"],
                  timeout=15)
    data = json.loads(cp.stdout or "{}")
    return data.get("blockdevices", [])


def summary() -> dict:
    return {
        "hostname": hostname(), "cpu": cpu_info(), "memory": memory_info(), "dmi": dmi(),
        "firmware": firmware_mode(), "secure_boot": secure_boot(), "iommu": iommu_enabled(),
        "kvm": kvm_status(), "kernel": kernel(), "os": os_release(), "uptime": uptime(), "boot_time": boot_time(),
    }


def _ip_bin() -> str:
    return "/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else "/usr/bin/ip"


def management_addresses() -> dict:
    """IPv4/IPv6 addresses of the interface holding the default route (falls back to any global address)."""
    out = {"interface": None, "ipv4": [], "ipv6": [], "gateway": None}
    if not proc.which(_ip_bin()):
        return out
    try:
        routes = json.loads(proc.run([_ip_bin(), "-j", "route", "show", "default"], check=False).stdout or "[]")
        addrs = json.loads(proc.run([_ip_bin(), "-j", "addr", "show"], check=False).stdout or "[]")
    except ValueError:
        return out
    dev = routes[0].get("dev") if routes else None
    out["gateway"] = routes[0].get("gateway") if routes else None
    cands = [a for a in addrs if a.get("ifname") == dev] if dev else []
    if not cands:
        cands = [a for a in addrs if a.get("ifname") != "lo" and not a.get("ifname", "").startswith(("virbr", "vnet"))]
    for a in cands:
        for x in a.get("addr_info", []):
            if x.get("scope") != "global":
                continue
            (out["ipv4"] if x.get("family") == "inet" else out["ipv6"]).append(f"{x['local']}/{x['prefixlen']}")
        if out["ipv4"] or out["ipv6"]:
            out["interface"] = a.get("ifname")
            break
    return out


def pci_passthrough_blocker(addr: str) -> Optional[str]:
    """Why a PCI device must not be given to a VM (None = can be passed through).

    Refuses devices the host itself depends on: the boot display, PCI bridges, NICs carrying an address or
    a bridge/bond membership, and controllers of disks that are mounted or hold LVM/RAID/swap."""
    dev = Path("/sys/bus/pci/devices") / addr
    if not dev.is_dir():
        return f"PCI device {addr} does not exist"
    if not iommu_enabled():
        return "IOMMU is not enabled (enable VT-d / AMD-Vi in the firmware; intel_iommu=on on Intel hosts)"
    if _read(str(dev / "boot_vga")) == "1":
        return "it is the host's primary display adapter"
    if _read(str(dev / "class")).startswith("0x06"):
        return "PCI bridges cannot be passed through"
    real = os.path.realpath(dev)
    nics = {n["name"]: n for n in physical_nics()}
    mg = management_addresses().get("interface")
    try:
        net_names = os.listdir(dev / "net")
    except OSError:
        net_names = []
    for n in net_names:
        info = nics.get(n) or {}
        if info.get("master") or n == mg:
            return f"its network interface {n} is used by the host ({info.get('master') or 'management'})"
    used = set()
    for bd in block_devices():
        def walk(node, top):
            if node.get("mountpoint") or node.get("fstype") in ("LVM2_member", "linux_raid_member", "swap"):
                used.add(top)
            for c in node.get("children") or []:
                walk(c, top)
        walk(bd, bd.get("name"))
    for b in Path("/sys/block").iterdir() if Path("/sys/block").is_dir() else []:
        if os.path.realpath(b / "device").startswith(real + "/") and b.name in used:
            return f"it controls disk {b.name}, which the host uses"
    return None
