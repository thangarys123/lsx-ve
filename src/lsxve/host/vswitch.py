"""Standard virtual switches and VLAN port groups (the ESXi vSwitch / port group model on Linux bridges).

* A virtual switch is a Linux bridge with physical uplinks (br0 = the management bridge, like vSwitch0).
* A port group is a libvirt network in bridge mode. VLAN 0 attaches VMs straight to the vSwitch bridge.
  VLAN N uses a small backing bridge ``<vswitch>v<N>`` whose only port is the 802.1Q sub-interface
  ``<uplink>.<N>``, so VMs on the port group send and receive frames tagged N on the uplink.
* Backing bridges are NetworkManager connections named ``lsxve-pg-<bridge>`` / ``lsxve-pg-<bridge>-vlan``,
  created inside a checkpoint that rolls back if the management address disappears.

Listing reads sysfs only and runs unprivileged (API); creating/removing backings runs in lsxve-agent (root).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..common import log, proc, validate as v
from . import hardware, ops

LOG = log.get("lsxve.vswitch")
NMCLI = "/usr/bin/nmcli"
SYS = Path("/sys/class/net")
VM_PORT_PREFIX = ("vnet", "veth", "tap", "macvtap", "vif")
BACKING_RE = re.compile(r"^(?P<vs>[A-Za-z0-9_-]{1,9})v(?P<vid>\d{1,4})$")


def backing_name(vswitch: str, vlan_id: int) -> str:
    return f"{vswitch[:9]}v{vlan_id}"


def _read(p: Path, default: str = "") -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return default


def _ports(br: str) -> List[str]:
    d = SYS / br / "brif"
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _is_backing(br: str) -> Optional[dict]:
    m = BACKING_RE.match(br)
    if not m:
        return None
    vid = m.group("vid")
    ports = [p for p in _ports(br) if not p.startswith(VM_PORT_PREFIX)]
    if any(p.endswith("." + vid) for p in ports):
        return {"vswitch_prefix": m.group("vs"), "vlan": int(vid), "port": next(p for p in ports if p.endswith("." + vid))}
    return None


def uplinks(br: str) -> List[str]:
    """Physical uplinks of a bridge: Ethernet, bond or VLAN devices (not VM taps or veths)."""
    return [p for p in _ports(br) if not p.startswith(VM_PORT_PREFIX)]


def list_vswitches() -> List[dict]:
    nics = {n["name"]: n for n in hardware.physical_nics()}
    mg = hardware.management_addresses().get("interface")
    out = []
    if not SYS.is_dir():
        return out
    for d in sorted(SYS.iterdir()):
        if not (d / "bridge").exists() or d.name.startswith("virbr") or _is_backing(d.name):
            continue
        ups = uplinks(d.name)
        out.append({
            "name": d.name, "mtu": int(_read(d / "mtu", "0") or 0), "management": d.name == mg,
            "stp": _read(d / "bridge" / "stp_state") == "1",
            "uplinks": [{"name": u, "carrier": (nics.get(u) or {}).get("carrier", False),
                         "speed_mbps": (nics.get(u) or {}).get("speed_mbps"), "kind": (nics.get(u) or {}).get("kind")}
                        for u in ups],
            "vm_ports": len([p for p in _ports(d.name) if p.startswith(VM_PORT_PREFIX)]),
        })
    return out


def _uplink_for(vswitch: str) -> str:
    nics = {n["name"]: n for n in hardware.physical_nics()}
    for u in uplinks(vswitch):
        if (nics.get(u) or {}).get("kind") in ("physical", "bond"):
            return u
    raise v.ValidationError("vswitch", f"virtual switch {vswitch} has no physical uplink for tagged VLANs")


def _conn_exists(name: str) -> bool:
    cp = proc.run([NMCLI, "-g", "NAME", "connection", "show"], check=False)
    return name in cp.stdout.splitlines()


def ensure_backing(vswitch: str, vlan_id: int) -> dict:
    """Agent op: create the VLAN backing bridge of a port group (idempotent)."""
    vswitch = v.ifname(vswitch, "vswitch")
    vlan_id = v.integer(vlan_id, "vlan_id", 1, 4094)
    if not (SYS / vswitch / "bridge").exists():
        raise v.ValidationError("vswitch", f"{vswitch} is not a virtual switch (bridge)")
    br = backing_name(vswitch, vlan_id)
    if (SYS / br).exists():
        return {"bridge": br, "created": False}
    up = _uplink_for(vswitch)
    vdev = f"{up[:10]}.{vlan_id}"
    had_ip = bool(hardware.management_addresses()["ipv4"])
    cp = ops.Checkpoint.create(120)
    try:
        proc.run([NMCLI, "connection", "add", "type", "bridge", "ifname", br, "con-name", f"lsxve-pg-{br}",
                  "connection.autoconnect", "yes", "bridge.stp", "no", "bridge.forward-delay", "0",
                  "ipv4.method", "disabled", "ipv6.method", "disabled"], timeout=30)
        proc.run([NMCLI, "connection", "add", "type", "vlan", "ifname", vdev, "con-name", f"lsxve-pg-{br}-vlan",
                  "dev", up, "id", str(vlan_id), "connection.master", br, "connection.slave-type", "bridge",
                  "connection.autoconnect", "yes"], timeout=30)
        proc.run([NMCLI, "connection", "up", f"lsxve-pg-{br}"], timeout=60, check=False)
        proc.run([NMCLI, "connection", "up", f"lsxve-pg-{br}-vlan"], timeout=60)
        if had_ip and not hardware.management_addresses()["ipv4"]:
            raise RuntimeError("the management address disappeared")
    except Exception as e:
        try:
            ops.Checkpoint.rollback(cp)
        except Exception as e2:  # noqa: BLE001
            LOG.error("rollback failed: %s", e2)
        raise v.ValidationError("vlan_id", f"VLAN {vlan_id} on {vswitch} could not be created and was rolled "
                                           f"back: {e}")
    ops.Checkpoint.commit(cp)
    LOG.info("port group backing %s: VLAN %s on %s (%s)", br, vlan_id, up, vdev)
    return {"bridge": br, "created": True, "uplink": up, "vlan_device": vdev}


def remove_backing(vswitch: str, vlan_id: int) -> dict:
    """Agent op: remove a VLAN backing bridge (caller checks that no port group uses it any more)."""
    vswitch = v.ifname(vswitch, "vswitch")
    vlan_id = v.integer(vlan_id, "vlan_id", 1, 4094)
    br = backing_name(vswitch, vlan_id)
    removed = []
    for name in (f"lsxve-pg-{br}-vlan", f"lsxve-pg-{br}"):
        if _conn_exists(name):
            proc.run([NMCLI, "connection", "delete", name], timeout=60)
            removed.append(name)
    return {"bridge": br, "removed": removed}


def portgroup_bridges() -> Dict[str, dict]:
    return {d.name: b for d in (SYS.iterdir() if SYS.is_dir() else []) if (b := _is_backing(d.name))}
