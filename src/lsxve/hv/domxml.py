"""Domain / volume / pool / network XML generation and parsing.

All XML is produced with ElementTree from validated, typed values - never by
string interpolation of user input.
"""
from __future__ import annotations

import secrets
import string
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

NS = "https://lsx-ve.org/xmlns/vm/1.0"
AS_NS = "https://lsx-ve.org/xmlns/autostart/1.0"   # per-VM autostart settings (own element: setMetadata)
PG_NS = "https://lsx-ve.org/xmlns/portgroup/1.0"   # port group (vSwitch + VLAN) of a libvirt network
ET.register_namespace("lsx", NS)
ET.register_namespace("lsxas", AS_NS)
ET.register_namespace("lsxpg", PG_NS)
VIDEO_MODELS = ("virtio", "vga", "bochs")
STOP_ACTIONS = ("shutdown", "poweroff", "suspend")
CPU_PERIOD_US = 100000
MEM_SLOTS = 16          # DIMM slots for memory hot add
MEM_HOTADD_STEP = 128   # MiB: Linux memory block size; hot-added DIMMs are multiples of it
SERIAL_MAX = 4          # isa-serial ports 0..3 (COM1..COM4); port 0 is the console

DISK_BUSES = ("virtio", "scsi", "sata")
NIC_MODELS = ("virtio", "e1000e", "rtl8139")
OS_TYPES = {
    "linux": {"label": "Linux (virtio)", "disk_bus": "virtio", "nic": "virtio", "video": "virtio"},
    "windows": {"label": "Microsoft Windows", "disk_bus": "sata", "nic": "e1000e", "video": "vga"},
    "bsd": {"label": "BSD", "disk_bus": "virtio", "nic": "virtio", "video": "vga"},
    "other": {"label": "Other", "disk_bus": "sata", "nic": "e1000e", "video": "vga"},
}


def _sub(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k.rstrip("_"): str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def tostring(el: ET.Element) -> str:
    return ET.tostring(el, encoding="unicode")


def vnc_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def disk_target_names(bus: str, used: List[str], count: int) -> List[str]:
    prefix = "vd" if bus == "virtio" else "sd"
    out = []
    for i in range(26 * 27):
        n = prefix + (chr(97 + i) if i < 26 else chr(96 + i // 26) + chr(97 + i % 26))
        if n not in used and n not in out:
            out.append(n)
            if len(out) == count:
                break
    return out


def build_disk(path: str, block: bool, fmt: str, target: str, bus: str, boot: Optional[int] = None) -> ET.Element:
    d = ET.Element("disk", {"type": "block" if block else "file", "device": "disk"})
    attrs = {"name": "qemu", "type": fmt, "discard": "unmap"}
    if bus == "virtio":
        attrs["io"] = "native" if block else "threads"
        attrs["cache"] = "none"
    _sub(d, "driver", **attrs)
    _sub(d, "source", **({"dev": path} if block else {"file": path}))
    _sub(d, "target", dev=target, bus=bus)
    if boot:
        _sub(d, "boot", order=boot)
    return d


def build_cdrom(path: Optional[str], target: str, boot: Optional[int] = None) -> ET.Element:
    d = ET.Element("disk", {"type": "file", "device": "cdrom"})
    _sub(d, "driver", name="qemu", type="raw")
    if path:
        _sub(d, "source", file=path)
    _sub(d, "target", dev=target, bus="sata")
    _sub(d, "readonly")
    if boot:
        _sub(d, "boot", order=boot)
    return d


def build_nic(kind: str, source: str, model: str, mac: Optional[str] = None, boot: Optional[int] = None) -> ET.Element:
    i = ET.Element("interface", {"type": kind})
    if mac:
        _sub(i, "mac", address=mac)
    _sub(i, "source", **({"network": source} if kind == "network" else {"bridge": source}))
    _sub(i, "model", type=model)
    if boot:
        _sub(i, "boot", order=boot)
    return i


def build_domain(spec: dict, domain_type: str, disks: List[dict], cdrom_path: Optional[str],
                 tpm_available: bool, created_by: str) -> ET.Element:
    """spec is the validated create-VM request; disks: [{path, block, format, bus}]"""
    os_type = spec["os_type"]
    prof = OS_TYPES[os_type]
    dom = ET.Element("domain", {"type": domain_type})
    _sub(dom, "name", spec["name"])
    if spec.get("description"):
        _sub(dom, "description", spec["description"])
    md = _sub(dom, "metadata")
    lsx = ET.SubElement(md, f"{{{NS}}}vm")
    ET.SubElement(lsx, f"{{{NS}}}os-type").text = os_type
    ET.SubElement(lsx, f"{{{NS}}}os-variant").text = spec.get("os_variant", "")
    ET.SubElement(lsx, f"{{{NS}}}template").text = "false"
    ET.SubElement(lsx, f"{{{NS}}}created-by").text = created_by

    if spec.get("mem_hotadd") and spec.get("mem_max_mib", 0) > spec["memory_mib"]:
        _sub(dom, "maxMemory", spec["mem_max_mib"], slots=MEM_SLOTS, unit="MiB")
    _sub(dom, "memory", spec["memory_mib"], unit="MiB")
    _sub(dom, "currentMemory", spec["memory_mib"], unit="MiB")
    vmax = spec.get("vcpus_max") if spec.get("cpu_hotadd") else None
    if vmax and vmax > spec["vcpus"]:
        _sub(dom, "vcpu", vmax, placement="static", current=spec["vcpus"])
    else:
        _sub(dom, "vcpu", spec["vcpus"], placement="static")
        vmax = None

    uefi = spec["firmware"] == "uefi"
    os_el = _sub(dom, "os", **({"firmware": "efi"} if uefi else {}))
    _sub(os_el, "type", "hvm", arch="x86_64", machine="q35")
    if uefi:
        fw = _sub(os_el, "firmware")
        sb = "yes" if spec.get("secure_boot") else "no"
        _sub(fw, "feature", enabled=sb, name="secure-boot")
        _sub(fw, "feature", enabled=sb, name="enrolled-keys")
        if spec.get("secure_boot"):
            _sub(os_el, "loader", secure="yes")
    _sub(os_el, "bootmenu", enable="yes", timeout="3000")

    feats = _sub(dom, "features")
    _sub(feats, "acpi")
    _sub(feats, "apic")
    if uefi and spec.get("secure_boot"):
        _sub(feats, "smm", state="on")
    if os_type == "windows":
        hv = _sub(feats, "hyperv", mode="custom")
        for f in ("relaxed", "vapic", "vpindex", "runtime", "synic", "reset", "frequencies"):
            _sub(hv, f, state="on")
        _sub(hv, "spinlocks", state="on", retries="8191")
        _sub(hv, "stimer", state="on")

    if domain_type == "kvm":
        cpu = _sub(dom, "cpu", mode="host-passthrough", check="none", migratable="on")
    else:
        cpu = _sub(dom, "cpu", mode="custom", match="exact")
        _sub(cpu, "model", "qemu64", fallback="allow")
    if vmax:  # CPU hot add: one core per socket so vCPUs can be plugged one at a time
        _sub(cpu, "topology", sockets=vmax, dies=1, cores=1, threads=1)
    else:
        _sub(cpu, "topology", sockets=1, dies=1, cores=spec["vcpus"], threads=1)
    if dom.find("maxMemory") is not None:
        _sync_numa(dom)

    clock = _sub(dom, "clock", offset="localtime" if os_type == "windows" else "utc")
    _sub(clock, "timer", name="rtc", tickpolicy="catchup")
    _sub(clock, "timer", name="pit", tickpolicy="delay")
    _sub(clock, "timer", name="hpet", present="no")
    if os_type == "windows":
        _sub(clock, "timer", name="hypervclock", present="yes")
    _sub(dom, "on_poweroff", "destroy")
    _sub(dom, "on_reboot", "restart")
    _sub(dom, "on_crash", "destroy")
    pm = _sub(dom, "pm")
    _sub(pm, "suspend-to-mem", enabled="no")
    _sub(pm, "suspend-to-disk", enabled="no")

    dev = _sub(dom, "devices")
    order = {k: i + 1 for i, k in enumerate(spec.get("boot_order") or ["cdrom", "disk", "network"])}
    used: List[str] = []
    for idx, d in enumerate(disks):
        tgt = disk_target_names(d["bus"], used, 1)[0]
        used.append(tgt)
        dev.append(build_disk(d["path"], d["block"], d["format"], tgt, d["bus"],
                              order.get("disk") if idx == 0 else None))
    cd_tgt = disk_target_names("sata", used, 1)[0]
    used.append(cd_tgt)
    dev.append(build_cdrom(cdrom_path, cd_tgt, order.get("cdrom")))
    if any(d["bus"] == "scsi" for d in disks):
        _sub(dev, "controller", type="scsi", model="virtio-scsi")
    _sub(dev, "controller", type="usb", model="qemu-xhci", ports="15")
    for idx, n in enumerate(spec["nics"]):
        dev.append(build_nic(n["type"], n["source"], n["model"], n.get("mac"),
                             order.get("network") if idx == 0 else None))
    ch = _sub(dev, "channel", type="unix")
    _sub(ch, "target", type="virtio", name="org.qemu.guest_agent.0")
    ser = _sub(dev, "serial", type="pty")
    _sub(ser, "target", type="isa-serial", port="0")
    con = _sub(dev, "console", type="pty")
    _sub(con, "target", type="serial", port="0")
    _sub(dev, "input", type="tablet", bus="usb")
    _sub(dev, "input", type="keyboard", bus="ps2")
    g = _sub(dev, "graphics", type="vnc", port="-1", autoport="yes", listen="127.0.0.1", passwd=vnc_password())
    _sub(g, "listen", type="address", address="127.0.0.1")
    video = _sub(dev, "video")
    _sub(video, "model", type=prof["video"], heads="1", primary="yes",
         **({"vram": "16384"} if prof["video"] == "vga" else {}))
    if spec.get("tpm") and tpm_available:
        tpm = _sub(dev, "tpm", model="tpm-crb")
        _sub(tpm, "backend", type="emulator", version="2.0")
    mb = _sub(dev, "memballoon", model="virtio")
    _sub(mb, "stats", period="5")
    rng = _sub(dev, "rng", model="virtio")
    _sub(rng, "backend", "/dev/urandom", model="random")
    for h in spec.get("hostdevs", []):
        dev.append(build_hostdev(h))
    set_tuning(dom, spec.get("cpu_shares"), spec.get("cpu_quota_us"), spec.get("mem_lock"))
    return dom


def set_tuning(dom: ET.Element, cpu_shares: Optional[int] = None, cpu_quota_us: Optional[int] = None,
               mem_lock: Optional[bool] = None) -> None:
    """Resource allocation: CPU shares, CPU limit (quota per 100 ms for the whole VM; 0 = unlimited),
    and "reserve all guest memory" (locked pages). None leaves a setting unchanged."""
    ct = dom.find("cputune")
    if (cpu_shares is not None or cpu_quota_us is not None) and ct is None:
        ct = _sub(dom, "cputune")
    if cpu_shares is not None:
        el = ct.find("shares")
        if el is None:
            el = _sub(ct, "shares")
        el.text = str(cpu_shares)
    if cpu_quota_us is not None:
        for tag in ("global_period", "global_quota"):
            el = ct.find(tag)
            if el is not None:
                ct.remove(el)
        if cpu_quota_us > 0:
            _sub(ct, "global_period", CPU_PERIOD_US)
            _sub(ct, "global_quota", cpu_quota_us)
    if ct is not None and len(ct) == 0:
        dom.remove(ct)
    if mem_lock is not None:
        mb = dom.find("memoryBacking")
        if mem_lock:
            if mb is None:
                mb = _sub(dom, "memoryBacking")
            if mb.find("locked") is None:
                _sub(mb, "locked")
        elif mb is not None:
            el = mb.find("locked")
            if el is not None:
                mb.remove(el)
            if len(mb) == 0:
                dom.remove(mb)


def set_disk_iotune(disk: ET.Element, iops: int, bps: int) -> None:
    """Per-disk I/O limits (0 = unlimited)."""
    old = disk.find("iotune")
    if old is not None:
        disk.remove(old)
    if iops or bps:
        io = _sub(disk, "iotune")
        if bps:
            _sub(io, "total_bytes_sec", bps)
        if iops:
            _sub(io, "total_iops_sec", iops)


def set_video(dom: ET.Element, model: str, vram_mib: Optional[int]) -> None:
    m = dom.find("devices/video/model")
    if m is None:
        v = _sub(dom.find("devices"), "video")
        m = _sub(v, "model", heads="1", primary="yes")
    for a in ("vram", "vgamem", "ram", "vram64"):
        m.attrib.pop(a, None)
    for child in list(m):
        m.remove(child)
    m.set("type", model)
    if model in ("vga", "bochs") and vram_mib:
        m.set("vram", str(vram_mib * 1024))


def _mib(el: Optional[ET.Element]) -> int:
    if el is None or not el.text:
        return 0
    mult = {"KiB": 1 / 1024, "k": 1 / 1024, "MiB": 1, "M": 1, "GiB": 1024, "G": 1024, "b": 1 / 1048576,
            "bytes": 1 / 1048576}.get(el.get("unit", "KiB"), 1 / 1024)
    return int(int(el.text) * mult)


def _dimms(dom: ET.Element) -> List[ET.Element]:
    return [m for m in dom.findall("devices/memory") if m.get("model") == "dimm"]


def _sync_numa(dom: ET.Element, base_mib: Optional[int] = None) -> None:
    """Memory hot add needs one guest NUMA cell that holds every vCPU and the boot memory
    (total memory minus the hot-added DIMMs)."""
    cpu = dom.find("cpu")
    if cpu is None:
        cpu = _sub(dom, "cpu", mode="host-passthrough", check="none")
    numa = cpu.find("numa")
    if numa is None:
        numa = _sub(cpu, "numa")
    cells = numa.findall("cell")
    for c in cells[1:]:
        numa.remove(c)
    cell = cells[0] if cells else _sub(numa, "cell", id="0")
    v = dom.find("vcpu")
    n = int(v.text) if v is not None and v.text else 1
    if base_mib is None:
        base_mib = _mib(dom.find("memory")) - sum(_mib(m.find("target/size")) for m in _dimms(dom))
    cell.set("id", "0")
    cell.set("cpus", "0" if n == 1 else f"0-{n - 1}")
    cell.set("memory", str(max(MEM_HOTADD_STEP, base_mib)))
    cell.set("unit", "MiB")


def set_memory(dom: ET.Element, memory_mib: int, hotadd: bool, max_mib: int) -> None:
    """Configured (power-on) memory. With memory hot add the domain gets maxMemory with DIMM slots and a
    NUMA cell; hot-added DIMMs are folded back into the boot memory when the configuration is rewritten."""
    devs = dom.find("devices")
    for m in _dimms(dom):
        devs.remove(m)
    for tag in ("memory", "currentMemory"):
        el = dom.find(tag)
        if el is None:
            el = ET.SubElement(dom, tag)
        el.attrib.clear()
        el.set("unit", "MiB")
        el.text = str(memory_mib)
    mx = dom.find("maxMemory")
    cpu = dom.find("cpu")
    if hotadd and max_mib > memory_mib:
        if mx is None:
            mx = ET.Element("maxMemory")
            dom.insert(list(dom).index(dom.find("memory")), mx)
        mx.attrib.clear()
        mx.set("slots", str(MEM_SLOTS))
        mx.set("unit", "MiB")
        mx.text = str(max_mib)
        _sync_numa(dom, memory_mib)
    else:
        if mx is not None:
            dom.remove(mx)
        if cpu is not None and cpu.find("numa") is not None:
            cpu.remove(cpu.find("numa"))


def build_dimm(size_mib: int) -> ET.Element:
    m = ET.Element("memory", {"model": "dimm"})
    t = _sub(m, "target")
    _sub(t, "size", size_mib, unit="MiB")
    _sub(t, "node", 0)
    return m


def parse_serials(root: ET.Element) -> List[dict]:
    out = []
    for el in root.findall("devices/serial"):
        t = el.find("target")
        src = el.find("source")
        port = int(t.get("port", "0")) if t is not None else 0
        out.append({"port": port, "type": el.get("type"), "path": src.get("path") if src is not None else None,
                    "console": el.get("type") == "pty" and port == 0})
    return sorted(out, key=lambda x: x["port"])


def build_serial_file(port: int, path: str) -> ET.Element:
    """A virtual serial port (COMn) whose output is appended to a file on a datastore."""
    el = ET.Element("serial", {"type": "file"})
    _sub(el, "source", path=path, append="on")
    t = _sub(el, "target", type="isa-serial", port=port)
    _sub(t, "model", name="isa-serial")
    return el


def set_vcpus(dom: ET.Element, vcpus: int, hotadd: bool, vmax: int) -> None:
    v = dom.find("vcpu")
    topo = dom.find("cpu/topology")
    if hotadd and vmax > vcpus:
        v.text = str(vmax)
        v.set("current", str(vcpus))
        n = vmax
        if topo is not None:
            topo.set("sockets", str(vmax)); topo.set("cores", "1")
    else:
        v.text = str(vcpus)
        v.attrib.pop("current", None)
        if topo is not None:
            topo.set("sockets", "1"); topo.set("cores", str(vcpus))
    if topo is not None:
        topo.set("dies", "1"); topo.set("threads", "1")
    if dom.find("cpu/numa") is not None:
        _sync_numa(dom)


def hostdev_id(h: dict) -> str:
    return f"pci:{h['address']}" if h["type"] == "pci" else f"usb:{h['vendor']}:{h['product']}"


def build_hostdev(h: dict) -> ET.Element:
    """h: {"type": "pci", "address": "0000:01:00.0"} or {"type": "usb", "vendor": "046d", "product": "c52b"}"""
    el = ET.Element("hostdev", {"mode": "subsystem", "type": h["type"], "managed": "yes"})
    src = _sub(el, "source")
    if h["type"] == "pci":
        dom_, bus, rest = h["address"].split(":")
        slot, fn = rest.split(".")
        _sub(src, "address", domain=f"0x{dom_}", bus=f"0x{bus}", slot=f"0x{slot}", function=f"0x{fn}")
    else:
        _sub(src, "vendor", id=f"0x{h['vendor']}")
        _sub(src, "product", id=f"0x{h['product']}")
    return el


def _parse_hostdev(el: ET.Element) -> Optional[dict]:
    t = el.get("type")
    src = el.find("source")
    if src is None:
        return None
    try:
        if t == "pci":
            a = src.find("address")
            h = {"type": "pci", "address": "%04x:%02x:%02x.%x" % (int(a.get("domain", "0"), 16), int(a.get("bus"), 16),
                                                                 int(a.get("slot"), 16), int(a.get("function"), 16))}
        elif t == "usb":
            h = {"type": "usb", "vendor": "%04x" % int(src.find("vendor").get("id"), 16),
                 "product": "%04x" % int(src.find("product").get("id"), 16)}
        else:
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    h["id"] = hostdev_id(h)
    return h


def build_autostart(a: dict) -> str:
    el = ET.Element(f"{{{AS_NS}}}autostart", {k: str(a[k]) for k in ("enabled", "order", "start_delay",
                                                                     "stop_action", "stop_delay", "wait_heartbeat")
                                             if k in a and a[k] is not None})
    return tostring(el)


def parse_autostart(root: ET.Element) -> Optional[dict]:
    el = root.find(f"metadata/{{{AS_NS}}}autostart")
    if el is None:
        return None

    def num(k, d):
        try:
            return int(el.get(k, d))
        except ValueError:
            return d
    return {"enabled": el.get("enabled") in ("1", "true", "True"), "order": num("order", 0),
            "start_delay": num("start_delay", -1), "stop_action": el.get("stop_action") or "",
            "stop_delay": num("stop_delay", -1), "wait_heartbeat": el.get("wait_heartbeat") in ("1", "true", "True")}


def build_volume(name: str, capacity: int, fmt: str, thick: bool) -> str:
    v = ET.Element("volume")
    _sub(v, "name", name)
    _sub(v, "capacity", capacity, unit="bytes")
    _sub(v, "allocation", capacity if thick else 0, unit="bytes")
    t = _sub(v, "target")
    if fmt:
        _sub(t, "format", type=fmt)
    return tostring(v)


def build_pool(spec: dict) -> str:
    p = ET.Element("pool", {"type": spec["type"]})
    _sub(p, "name", spec["name"])
    src = _sub(p, "source")
    tgt = _sub(p, "target")
    t = spec["type"]
    if t == "dir":
        _sub(tgt, "path", spec["path"])
    elif t == "netfs":
        _sub(src, "host", name=spec["host"])
        _sub(src, "dir", path=spec["export"])
        _sub(src, "format", type="nfs")
        _sub(tgt, "path", spec["path"])
    elif t == "logical":
        _sub(src, "name", spec["vg"])
        _sub(src, "format", type="lvm2")
        _sub(tgt, "path", f"/dev/{spec['vg']}")
    elif t == "iscsi":
        _sub(src, "host", name=spec["host"], port=spec.get("port", 3260))
        _sub(src, "device", path=spec["iqn"])
        _sub(tgt, "path", "/dev/disk/by-path")
    return tostring(p)


def build_network(spec: dict) -> str:
    import ipaddress
    n = ET.Element("network")
    _sub(n, "name", spec["name"])
    mode = spec["mode"]
    if spec.get("vswitch"):
        md = _sub(n, "metadata")
        ET.SubElement(md, f"{{{PG_NS}}}portgroup", {"vswitch": spec["vswitch"], "vlan": str(spec.get("vlan", 0))})
    if mode == "bridge":
        _sub(n, "forward", mode="bridge")
        _sub(n, "bridge", name=spec["bridge"])
        return tostring(n)
    if mode in ("nat", "route"):
        _sub(n, "forward", mode=mode)
    _sub(n, "bridge", stp="on", delay="0")
    net = ipaddress.IPv4Interface(spec["ipv4"])
    ip = _sub(n, "ip", address=str(net.ip), netmask=str(net.network.netmask))
    if spec.get("dhcp_start") and spec.get("dhcp_end"):
        dh = _sub(ip, "dhcp")
        _sub(dh, "range", start=spec["dhcp_start"], end=spec["dhcp_end"])
    return tostring(n)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_domain(xml: str) -> dict:
    root = ET.fromstring(xml)

    def txt(path: str, default: str = "") -> str:
        el = root.find(path)
        return el.text if el is not None and el.text is not None else default

    mem = root.find("memory")
    mem_kib = int(mem.text) * {"KiB": 1, "MiB": 1024, "GiB": 1024 ** 2, "b": 1 / 1024, "bytes": 1 / 1024}.get(
        mem.get("unit", "KiB"), 1) if mem is not None else 0
    os_el = root.find("os")
    firmware = "uefi" if (os_el is not None and (os_el.get("firmware") == "efi" or
                                                 (os_el.find("loader") is not None and
                                                  os_el.find("loader").get("type") == "pflash"))) else "bios"
    secure = False
    if os_el is not None:
        for f in os_el.findall("firmware/feature"):
            if f.get("name") == "secure-boot" and f.get("enabled") == "yes":
                secure = True
        ld = os_el.find("loader")
        if ld is not None and ld.get("secure") == "yes":
            secure = True
    md = root.find(f"metadata/{{{NS}}}vm")

    def mdv(tag: str, default: str = "") -> str:
        if md is None:
            return default
        el = md.find(f"{{{NS}}}{tag}")
        return el.text if el is not None and el.text else default

    disks, cdroms, nics = [], [], []
    boot = []
    graphics = None
    tpm = False
    for d in root.findall("devices/disk"):
        dev = d.get("device", "disk")
        src = d.find("source")
        path = None
        if src is not None:
            path = src.get("file") or src.get("dev") or src.get("volume")
        tgt = d.find("target")
        drv = d.find("driver")
        b = d.find("boot")
        io = d.find("iotune")
        entry = {"target": tgt.get("dev") if tgt is not None else "", "bus": tgt.get("bus") if tgt is not None else "",
                 "path": path, "format": drv.get("type") if drv is not None else "",
                 "boot_order": int(b.get("order")) if b is not None else None, "readonly": d.find("readonly") is not None,
                 "iops_limit": int(io.findtext("total_iops_sec") or 0) if io is not None else 0,
                 "bps_limit": int(io.findtext("total_bytes_sec") or 0) if io is not None else 0}
        if b is not None:
            boot.append((int(b.get("order")), "cdrom" if dev == "cdrom" else "disk"))
        (cdroms if dev == "cdrom" else disks).append(entry)
    for i in root.findall("devices/interface"):
        src = i.find("source")
        mac = i.find("mac")
        model = i.find("model")
        tgt = i.find("target")
        b = i.find("boot")
        link = i.find("link")
        if b is not None:
            boot.append((int(b.get("order")), "network"))
        nics.append({"type": i.get("type"), "mac": mac.get("address") if mac is not None else None,
                     "source": (src.get("network") or src.get("bridge")) if src is not None else None,
                     "model": model.get("type") if model is not None else None,
                     "device": tgt.get("dev") if tgt is not None else None,
                     "link": link.get("state") if link is not None else "up"})
    g = root.find("devices/graphics")
    if g is not None:
        graphics = {"type": g.get("type"), "port": int(g.get("port", "-1") or -1),
                    "listen": g.get("listen", "127.0.0.1"), "passwd": g.get("passwd")}
    if root.find("devices/tpm") is not None:
        tpm = True
    agent = None  # None: no guest agent channel; else the channel state libvirt reports (live XML only)
    for ch in root.findall("devices/channel"):
        t = ch.find("target")
        if t is not None and t.get("name") == "org.qemu.guest_agent.0":
            agent = t.get("state") or "configured"
    vcpu = root.find("vcpu")
    vmax = int(vcpu.text) if vcpu is not None else 1
    vcur = int(vcpu.get("current", vmax)) if vcpu is not None else 1
    ct = root.find("cputune")
    vm_ = root.find("devices/video/model")
    try:
        vram = int(vm_.get("vram")) // 1024 if vm_ is not None and vm_.get("vram") else None
    except ValueError:
        vram = None
    hostdevs = [h for h in (_parse_hostdev(e) for e in root.findall("devices/hostdev")) if h]
    mx = root.find("maxMemory")
    return {
        "name": txt("name"), "uuid": txt("uuid"), "description": txt("description"), "title": txt("title"),
        "domain_type": root.get("type"), "vcpus": vcur, "vcpus_max": vmax,
        "cpu_hotadd": vcpu is not None and vcpu.get("current") is not None,
        "cpu_shares": int(ct.findtext("shares") or 0) if ct is not None else 0,
        "cpu_quota_us": int(ct.findtext("global_quota") or 0) if ct is not None else 0,
        "mem_locked": root.find("memoryBacking/locked") is not None,
        "mem_hotadd": mx is not None, "mem_max_mib": _mib(mx) if mx is not None else 0,
        "mem_dimms": len(_dimms(root)), "serials": parse_serials(root),
        "usb_controllers": [c.get("model") or "default" for c in root.findall("devices/controller")
                            if c.get("type") == "usb" and c.get("model") != "none"],
        "video": {"model": vm_.get("type") if vm_ is not None else None, "vram_mib": vram},
        "hostdevs": hostdevs, "autostart_cfg": parse_autostart(root),
        "memory_mib": int(mem_kib // 1024), "firmware": firmware, "secure_boot": secure, "tpm": tpm,
        "machine": (os_el.find("type").get("machine") if os_el is not None and os_el.find("type") is not None else ""),
        "os_type": mdv("os-type", "other"), "os_variant": mdv("os-variant"), "template": mdv("template") == "true",
        "created_by": mdv("created-by"), "disks": disks, "cdroms": cdroms, "nics": nics, "graphics": graphics,
        "boot_order": [k for _, k in sorted(boot)], "guest_agent": agent,
    }


def set_metadata(root: ET.Element, key: str, value: str) -> None:
    md = root.find("metadata")
    if md is None:
        md = ET.SubElement(root, "metadata")
    vm = md.find(f"{{{NS}}}vm")
    if vm is None:
        vm = ET.SubElement(md, f"{{{NS}}}vm")
    el = vm.find(f"{{{NS}}}{key}")
    if el is None:
        el = ET.SubElement(vm, f"{{{NS}}}{key}")
    el.text = value


def apply_boot_order(root: ET.Element, order: List[str]) -> None:
    """Rewrite per-device <boot order>; first device of each kind gets the slot."""
    rank = {k: i + 1 for i, k in enumerate(order)}
    os_el = root.find("os")
    if os_el is not None:
        for b in os_el.findall("boot"):
            os_el.remove(b)
    seen = set()
    devices = root.find("devices")
    for el in list(devices):
        kind = None
        if el.tag == "disk":
            kind = "cdrom" if el.get("device") == "cdrom" else ("disk" if el.get("device") == "disk" else None)
        elif el.tag == "interface":
            kind = "network"
        if kind is None:
            continue
        for b in el.findall("boot"):
            el.remove(b)
        if kind in rank and kind not in seen:
            seen.add(kind)
            ET.SubElement(el, "boot", {"order": str(rank[kind])})


def parse_pool(xml: str) -> dict:
    r = ET.fromstring(xml)
    src = r.find("source")
    info = {"type": r.get("type"), "path": (r.findtext("target/path") or "")}
    if src is not None:
        h = src.find("host")
        if h is not None:
            info["host"] = h.get("name")
        d = src.find("dir")
        if d is not None:
            info["export"] = d.get("path")
        dv = src.find("device")
        if dv is not None:
            info["device"] = dv.get("path")
        nm = src.findtext("name")
        if nm:
            info["vg"] = nm
    return info


def parse_volume(xml: str) -> dict:
    r = ET.fromstring(xml)
    fmt = r.find("target/format")
    return {"format": fmt.get("type") if fmt is not None else "", "path": r.findtext("target/path") or "",
            "backing": r.findtext("backingStore/path")}


def parse_network(xml: str) -> dict:
    r = ET.fromstring(xml)
    fwd = r.find("forward")
    br = r.find("bridge")
    ip = r.find("ip")
    out = {"mode": fwd.get("mode", "nat") if fwd is not None else "isolated",
           "bridge": br.get("name") if br is not None else None, "ipv4": None, "dhcp": None,
           "vswitch": None, "vlan": None}
    pg = r.find(f"metadata/{{{PG_NS}}}portgroup")
    if pg is not None:
        out["vswitch"] = pg.get("vswitch")
        try:
            out["vlan"] = int(pg.get("vlan", "0"))
        except ValueError:
            out["vlan"] = 0
    if ip is not None and ip.get("address"):
        import ipaddress
        try:
            pfx = ip.get("prefix") or ipaddress.IPv4Network(f"0.0.0.0/{ip.get('netmask')}").prefixlen
            out["ipv4"] = f"{ip.get('address')}/{pfx}"
        except ValueError:
            out["ipv4"] = ip.get("address")
        rng = ip.find("dhcp/range")
        if rng is not None:
            out["dhcp"] = {"start": rng.get("start"), "end": rng.get("end")}
    return out


def parse_snapshot(xml: str) -> dict:
    r = ET.fromstring(xml)
    return {"name": r.findtext("name") or "", "description": r.findtext("description") or "",
            "created": int(r.findtext("creationTime") or 0), "state": r.findtext("state") or "",
            "parent": r.findtext("parent/name")}


def build_snapshot(name: str, description: str) -> str:
    s = ET.Element("domainsnapshot")
    _sub(s, "name", name)
    if description:
        _sub(s, "description", description)
    return tostring(s)
