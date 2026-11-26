"""OVF 1.x descriptors: read what an OVA/OVF from VMware ESXi, Workstation or VirtualBox describes, and write
an OVF that ESXi / vSphere can deploy (streamOptimized VMDK disks)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional

OVF = "http://schemas.dmtf.org/ovf/envelope/1"
RASD = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
VSSD = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
VMW = "http://www.vmware.com/schema/ovf"
NSMAP = {"ovf": OVF, "rasd": RASD, "vssd": VSSD, "vmw": VMW}
MAX_OVF = 4 * 1024 * 1024

# OVF OperatingSystemSection ids (CIM_OperatingSystem) -> (family, label)
CIM_OS = {
    1: ("other", "Other"), 36: ("linux", "Linux"), 101: ("linux", "Linux 64-bit"), 80: ("linux", "Red Hat Enterprise Linux"),
    79: ("linux", "Red Hat Enterprise Linux 64-bit"), 94: ("linux", "Ubuntu"), 93: ("linux", "Ubuntu 64-bit"),
    96: ("linux", "Debian"), 95: ("linux", "Debian 64-bit"), 107: ("linux", "CentOS 64-bit"),
    108: ("linux", "CentOS"), 103: ("windows", "Windows Server 2008 64-bit"), 105: ("windows", "Windows 7 64-bit"),
    112: ("windows", "Windows Server 2012 64-bit"), 116: ("windows", "Windows 8 64-bit"),
    42: ("bsd", "FreeBSD"), 78: ("bsd", "FreeBSD 64-bit"),
}


def _q(ns: str, tag: str) -> str:
    return f"{{{NSMAP[ns]}}}{tag}"


def _units(text: Optional[str]) -> int:
    """Multiplier of an OVF allocation unit ("byte * 2^20", "MegaBytes", "GB")."""
    t = (text or "byte").replace(" ", "").lower()
    m = re.match(r"^byte\*2\^(\d+)$", t)
    if m:
        return 2 ** int(m.group(1))
    return {"byte": 1, "bytes": 1, "kilobytes": 1024, "kb": 1024, "megabytes": 2 ** 20, "mb": 2 ** 20,
            "gigabytes": 2 ** 30, "gb": 2 ** 30}.get(t, 2 ** 20)


def parse(text: str) -> dict:
    """Hardware described by an OVF: name, CPUs, memory, firmware, guest OS, disks (in order) and NICs."""
    if len(text) > MAX_OVF:
        raise ValueError("OVF descriptor too large")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("OVF descriptors with DTDs/entities are not accepted")
    root = ET.fromstring(text)
    files = {f.get(_q("ovf", "id")): f.get(_q("ovf", "href")) for f in root.iter(_q("ovf", "File"))}
    disks = {}
    for d in root.iter(_q("ovf", "Disk")):
        cap = int(d.get(_q("ovf", "capacity"), "0") or 0) * _units(d.get(_q("ovf", "capacityAllocationUnits")))
        disks[d.get(_q("ovf", "diskId"))] = {"file": files.get(d.get(_q("ovf", "fileRef"))), "capacity": cap}
    vs = next(root.iter(_q("ovf", "VirtualSystem")), None)
    if vs is None:
        raise ValueError("no VirtualSystem in the OVF")
    out = {"name": vs.findtext(_q("ovf", "Name")) or vs.get(_q("ovf", "id")) or "imported-vm",
           "vcpus": 1, "memory_mib": 1024, "firmware": "bios", "os_family": "other", "os_label": "",
           "disks": [], "nics": [], "cdrom": False}
    os_el = vs.find(_q("ovf", "OperatingSystemSection"))
    if os_el is not None:
        vmw_type = (os_el.get(_q("vmw", "osType")) or "").lower()
        desc = os_el.findtext(_q("ovf", "Description")) or ""
        try:
            fam, label = CIM_OS.get(int(os_el.get(_q("ovf", "id"), "1")), ("other", "Other"))
        except ValueError:
            fam, label = "other", "Other"
        probe = f"{vmw_type} {desc}".lower()
        if "windows" in probe or vmw_type.startswith("win"):
            fam = "windows"
        elif any(k in probe for k in ("linux", "ubuntu", "debian", "centos", "rhel", "rocky", "alma", "suse",
                                      "fedora", "oracle", "photon")):
            fam = "linux"
        elif "bsd" in probe:
            fam = "bsd"
        out["os_family"], out["os_label"] = fam, (desc or label)[:64]
    hw = vs.find(_q("ovf", "VirtualHardwareSection"))
    if hw is not None:
        for c in hw.iter(_q("vmw", "Config")):
            if c.get(_q("vmw", "key")) == "firmware" and (c.get(_q("vmw", "value")) or "").lower() == "efi":
                out["firmware"] = "uefi"
        for item in list(hw.findall(_q("ovf", "Item"))) + list(hw.findall(_q("ovf", "StorageItem"))) + \
                list(hw.findall(_q("ovf", "EthernetPortItem"))):
            def r(tag):
                el = item.find(_q("rasd", tag))
                if el is None:  # OVF 2 / DSP8023 items use the same local names in other namespaces
                    el = next((e for e in item if e.tag.endswith("}" + tag)), None)
                return el.text.strip() if el is not None and el.text else ""
            try:
                rt = int(r("ResourceType") or 0)
            except ValueError:
                continue
            if rt == 3:
                out["vcpus"] = max(1, int(r("VirtualQuantity") or 1))
            elif rt == 4:
                out["memory_mib"] = max(128, int(r("VirtualQuantity") or 0) * _units(r("AllocationUnits")) // 2 ** 20)
            elif rt == 17:
                ref = r("HostResource")
                m = re.search(r"(?:ovf:)?/disk/(.+)$", ref)
                if m and m.group(1) in disks:
                    out["disks"].append(dict(disks[m.group(1)]))
            elif rt == 10:
                out["nics"].append({"model": r("ResourceSubType") or "E1000"})
            elif rt in (15, 16):
                out["cdrom"] = True
    if not out["disks"]:  # descriptors without hardware references: take the disk section in order
        out["disks"] = list(disks.values())
    for d in out["disks"]:
        if not d.get("file"):
            raise ValueError("a disk in the OVF has no file reference")
    return out


def nic_model(ovf_model: str, os_family: str) -> str:
    m = ovf_model.lower()
    if "vmxnet" in m or "virtio" in m:
        return "virtio" if os_family != "windows" else "e1000e"
    return "e1000e"


def build(vm: dict, disk_files: List[dict]) -> str:
    """OVF 1.0 envelope for ``vm`` (VM detail as returned by the driver). disk_files: [{file, size, capacity}]"""
    ET.register_namespace("ovf", OVF)
    ET.register_namespace("rasd", RASD)
    ET.register_namespace("vssd", VSSD)
    ET.register_namespace("vmw", VMW)
    env = ET.Element(_q("ovf", "Envelope"), {_q("ovf", "version"): "1.0"})
    refs = ET.SubElement(env, _q("ovf", "References"))
    for i, d in enumerate(disk_files):
        ET.SubElement(refs, _q("ovf", "File"), {_q("ovf", "id"): f"file{i + 1}", _q("ovf", "href"): d["file"],
                                                _q("ovf", "size"): str(d["size"])})
    ds = ET.SubElement(env, _q("ovf", "DiskSection"))
    ET.SubElement(ds, _q("ovf", "Info")).text = "Virtual disk information"
    for i, d in enumerate(disk_files):
        ET.SubElement(ds, _q("ovf", "Disk"), {
            _q("ovf", "capacity"): str(d["capacity"]), _q("ovf", "capacityAllocationUnits"): "byte",
            _q("ovf", "diskId"): f"vmdisk{i + 1}", _q("ovf", "fileRef"): f"file{i + 1}",
            _q("ovf", "format"): "http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"})
    nets = sorted({n["source"] for n in vm["hardware"]["nics"] if n.get("source")})
    ns = ET.SubElement(env, _q("ovf", "NetworkSection"))
    ET.SubElement(ns, _q("ovf", "Info")).text = "The list of logical networks"
    for n in nets:
        e = ET.SubElement(ns, _q("ovf", "Network"), {_q("ovf", "name"): n})
        ET.SubElement(e, _q("ovf", "Description")).text = f"The {n} network"
    vs = ET.SubElement(env, _q("ovf", "VirtualSystem"), {_q("ovf", "id"): vm["name"]})
    ET.SubElement(vs, _q("ovf", "Info")).text = "A virtual machine exported from LSX-VE"
    ET.SubElement(vs, _q("ovf", "Name")).text = vm["name"]
    fam = vm.get("os_type", "other")
    oss = ET.SubElement(vs, _q("ovf", "OperatingSystemSection"), {
        _q("ovf", "id"): {"windows": "1", "linux": "101", "bsd": "78"}.get(fam, "1"),
        _q("vmw", "osType"): {"windows": "windows9Server64Guest", "linux": "otherLinux64Guest",
                              "bsd": "freebsd64Guest"}.get(fam, "otherGuest64")})
    ET.SubElement(oss, _q("ovf", "Info")).text = "The operating system installed"
    ET.SubElement(oss, _q("ovf", "Description")).text = vm.get("os_variant") or fam
    hw = ET.SubElement(vs, _q("ovf", "VirtualHardwareSection"))
    ET.SubElement(hw, _q("ovf", "Info")).text = "Virtual hardware requirements"
    sysel = ET.SubElement(hw, _q("ovf", "System"))
    for tag, val in (("ElementName", "Virtual Hardware Family"), ("InstanceID", "0"),
                     ("VirtualSystemIdentifier", vm["name"]), ("VirtualSystemType", "vmx-14")):
        ET.SubElement(sysel, _q("vssd", tag)).text = val
    iid = [0]

    def item(fields: dict):
        iid[0] += 1
        it = ET.SubElement(hw, _q("ovf", "Item"))
        fields = {**fields, "InstanceID": str(iid[0])}
        for k in sorted(fields):
            ET.SubElement(it, _q("rasd", k)).text = str(fields[k])
        return iid[0]
    item({"AllocationUnits": "hertz * 10^6", "Description": "Number of Virtual CPUs",
          "ElementName": f"{vm['vcpus']} virtual CPU(s)", "ResourceType": 3, "VirtualQuantity": vm["vcpus"]})
    item({"AllocationUnits": "byte * 2^20", "Description": "Memory Size", "ElementName": f"{vm['memory_mib']}MB of memory",
          "ResourceType": 4, "VirtualQuantity": vm["memory_mib"]})
    ctl = item({"Address": 0, "Description": "SCSI Controller", "ElementName": "SCSI controller 0",
                "ResourceSubType": "lsilogicsas" if fam == "windows" else "VirtualSCSI", "ResourceType": 6})
    for i in range(len(disk_files)):
        item({"AddressOnParent": i, "ElementName": f"Hard disk {i + 1}", "HostResource": f"ovf:/disk/vmdisk{i + 1}",
              "Parent": ctl, "ResourceType": 17})
    for i, n in enumerate(vm["hardware"]["nics"]):
        item({"AddressOnParent": 7 + i, "AutomaticAllocation": "true", "Connection": n.get("source") or "VM Network",
              "Description": f"VmxNet3 ethernet adapter on \"{n.get('source')}\"", "ElementName": f"Network adapter {i + 1}",
              "ResourceSubType": "VmxNet3", "ResourceType": 10})
    if vm["hardware"].get("firmware") == "uefi":
        ET.SubElement(hw, _q("vmw", "Config"), {_q("ovf", "required"): "false", _q("vmw", "key"): "firmware",
                                                _q("vmw", "value"): "efi"})
    if vm.get("description"):
        ann = ET.SubElement(vs, _q("ovf", "AnnotationSection"))
        ET.SubElement(ann, _q("ovf", "Info")).text = "A human-readable annotation"
        ET.SubElement(ann, _q("ovf", "Annotation")).text = vm["description"]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(env, encoding="unicode")
