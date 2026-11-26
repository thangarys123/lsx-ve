"""Bridged management network: move the host's IP configuration onto a Linux bridge.

VMs and containers attached to the bridge are then ordinary hosts on the
physical LAN and get their addresses from its DHCP server, as with a VMware
ESXi "VM Network" port group.

Runs once inside lsxve-agent (root) at start-up:

1. find the interface holding the default route and its active NetworkManager
   connection (Ethernet or VLAN);
2. create bridge ``br0`` with the same IPv4/IPv6 settings, DNS, firewall zone
   and the NIC's MAC address (so a DHCP server hands out the same lease), and
   a port connection for the NIC;
3. switch over inside a NetworkManager checkpoint, then check that the bridge
   got an address and (if it answered before) that the gateway still answers;
4. commit, or roll back to the previous configuration automatically.

The result is recorded in /var/lib/lsxve/management-bridge.json so it is never
retried in a loop. Set "management_bridge": "" in /etc/lsxve/lsxve.conf to
disable it.
"""
from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from ..common import config, log, proc
from . import ops

LOG = log.setup("lsxve.netbridge")

STATE_FILE = config.STATE_DIR / "management-bridge.json"
NMCLI = ops.NMCLI
IP = "/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else "/usr/bin/ip"
CHECKPOINT_SECONDS = 300


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({**d, "ts": time.time()}, indent=1))


def is_bridge(dev: str) -> bool:
    return bool(dev) and os.path.isdir(f"/sys/class/net/{dev}/bridge")


def _default_route_dev(wait: float) -> Optional[dict]:
    end = time.time() + wait
    while True:
        try:
            routes = json.loads(proc.run([IP, "-j", "-4", "route", "show", "default"], check=False).stdout or "[]")
        except ValueError:
            routes = []
        if routes and routes[0].get("dev"):
            return routes[0]
        if time.time() >= end:
            return None
        time.sleep(3)


def _global_ipv4(dev: str) -> List[str]:
    try:
        a = json.loads(proc.run([IP, "-j", "-4", "addr", "show", "dev", dev], check=False).stdout or "[]")
    except ValueError:
        return []
    return [f"{x['local']}/{x['prefixlen']}" for i in a for x in i.get("addr_info", []) if x.get("scope") == "global"]


def _pings(addr: Optional[str]) -> bool:
    if not addr:
        return False
    for _ in range(3):
        if proc.run(["/usr/bin/ping", "-c", "1", "-W", "2", addr], timeout=10, check=False).returncode == 0:
            return True
    return False


def _nm_get(uuid: str, field: str) -> str:
    val = proc.run([NMCLI, "-g", field, "connection", "show", uuid], check=False).stdout.strip()
    return "" if val in ("--",) else ops._nm_unescape(val)


def ip_args(detail: dict, dns_search: str) -> List[str]:
    """nmcli property arguments reproducing a connection's IP configuration on the bridge."""
    a: List[str] = []
    ip4 = detail.get("ipv4") or {}
    m4 = ip4.get("method") or "auto"
    a += ["ipv4.method", m4]
    if m4 == "manual":
        a += ["ipv4.addresses", ",".join(ip4.get("addresses") or [])]
        if ip4.get("gateway"):
            a += ["ipv4.gateway", ip4["gateway"]]
    if ip4.get("dns"):
        a += ["ipv4.dns", " ".join(ip4["dns"])]
    if dns_search:
        a += ["ipv4.dns-search", dns_search]
    ip6 = detail.get("ipv6") or {}
    m6 = ip6.get("method") or "auto"
    a += ["ipv6.method", m6]
    if m6 == "manual":
        a += ["ipv6.addresses", ",".join(ip6.get("addresses") or [])]
        if ip6.get("gateway"):
            a += ["ipv6.gateway", ip6["gateway"]]
    return a


def port_args(bridge: str, dev: str, detail: dict) -> List[str]:
    common = ["con-name", f"{bridge}-port-{dev}", "master", bridge, "slave-type", "bridge",
              "connection.autoconnect", "yes"]
    if detail["type"] == "vlan":
        vl = detail.get("vlan") or {}
        return ["connection", "add", "type", "vlan", "ifname", dev, "dev", vl.get("parent") or "",
                "id", str(vl.get("id") or ""), *common]
    return ["connection", "add", "type", "ethernet", "ifname", dev, *common]


def mtu_of(value: str) -> Optional[int]:
    """A custom MTU of the old connection (copied to the bridge and its port); None for auto/default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 576 <= n <= 9216 and n != 1500 else None


def bond_port_args(bridge: str) -> List[str]:
    clear = [x for fam in ("ipv4", "ipv6") for k in ("addresses", "gateway", "dns", "dns-search")
             for x in (f"{fam}.{k}", "")]
    return ["connection.master", bridge, "connection.slave-type", "bridge", *clear,
            "ipv4.method", "disabled", "ipv6.method", "disabled"]


def ensure(bridge: Optional[str] = None) -> dict:
    """Convert the management interface to a bridge once. Returns the recorded state."""
    if bridge is None:
        bridge = config.load().get("management_bridge", "br0")
    st = _read_state()
    if not bridge:
        return {"status": "disabled"}
    if st.get("status") in ("done", "failed", "skipped"):
        return st
    if not proc.which(NMCLI):
        return {"status": "unavailable"}
    route = _default_route_dev(wait=90)
    if not route:
        LOG.warning("bridged networking: no default route yet - will try again at the next start")
        return {"status": "waiting"}
    dev, gw = route["dev"], route.get("gateway")
    if is_bridge(dev):
        st = {"status": "done", "bridge": dev, "note": "management interface already is a bridge"}
        _write_state(st)
        return st
    uuid = proc.run([NMCLI, "-g", "GENERAL.CON-UUID", "device", "show", dev], check=False).stdout.strip()
    if not uuid:
        st = {"status": "skipped", "reason": f"{dev} is not managed by NetworkManager"}
        _write_state(st)
        LOG.warning("bridged networking skipped: %s", st["reason"])
        return st
    ctype = _nm_get(uuid, "connection.type")
    kind = {"802-3-ethernet": "ethernet", "vlan": "vlan", "bond": "bond"}.get(ctype)
    if not kind:
        st = {"status": "skipped", "reason": f"management connection type {ctype or '?'} on {dev} is not "
                                             "converted automatically (Ethernet, VLAN and bond are)"}
        _write_state(st)
        LOG.warning("bridged networking skipped: %s", st["reason"])
        return st
    if os.path.exists(f"/sys/class/net/{bridge}"):
        st = {"status": "skipped", "reason": f"an interface named {bridge} already exists"}
        _write_state(st)
        return st
    name = _nm_get(uuid, "connection.id")
    detail = ops._connection_detail(uuid, name, ctype, dev, True)
    detail["type"] = kind
    mac = open(f"/sys/class/net/{dev}/address").read().strip()
    zone = _nm_get(uuid, "connection.zone")
    mtu = mtu_of(_nm_get(uuid, "802-3-ethernet.mtu"))
    dns_search = _nm_get(uuid, "ipv4.dns-search")
    had_ip = bool(_global_ipv4(dev))
    gw_answered = _pings(gw)
    LOG.info("bridged networking: moving %s (%s, connection %s) onto bridge %s", dev, ", ".join(_global_ipv4(dev)),
             name, bridge)

    cp = ops.Checkpoint.create(CHECKPOINT_SECONDS)
    try:
        proc.run([NMCLI, "connection", "add", "type", "bridge", "ifname", bridge, "con-name", bridge,
                  "connection.autoconnect", "yes", "connection.autoconnect-slaves", "yes",
                  "bridge.stp", "no", "bridge.forward-delay", "0", "bridge.mac-address", mac,
                  *(["connection.zone", zone] if zone else []), *(["ethernet.mtu", str(mtu)] if mtu else []),
                  *ip_args(detail, dns_search)], timeout=30)
        if kind == "bond":
            # the bond keeps its members; the bond itself becomes the bridge port (IP moves to the bridge)
            proc.run([NMCLI, "connection", "modify", uuid, *bond_port_args(bridge)], timeout=30)
            proc.run([NMCLI, "connection", "up", uuid], timeout=90, check=False)
        else:
            proc.run([NMCLI, *port_args(bridge, dev, detail), *(["ethernet.mtu", str(mtu)] if mtu else [])], timeout=30)
            proc.run([NMCLI, "connection", "modify", uuid, "connection.autoconnect", "no"], timeout=30)
            proc.run([NMCLI, "connection", "up", f"{bridge}-port-{dev}"], timeout=90, check=False)
        proc.run([NMCLI, "connection", "up", bridge], timeout=90, check=False)
        ok, why = True, ""
        end = time.time() + 60
        while had_ip and not _global_ipv4(bridge) and time.time() < end:
            time.sleep(2)
        if had_ip and not _global_ipv4(bridge):
            ok, why = False, f"{bridge} did not get an IPv4 address"
        elif gw_answered and not _pings(gw):
            ok, why = False, f"gateway {gw} does not answer through {bridge}"
    except Exception as e:  # noqa: BLE001 - anything unexpected: roll back
        ok, why = False, str(e)
    if ok:
        ops.Checkpoint.commit(cp)
        st = {"status": "done", "bridge": bridge, "port": dev, "previous_connection": name,
              "addresses": _global_ipv4(bridge)}
        LOG.info("bridged networking active: %s with port %s, %s", bridge, dev, ", ".join(st["addresses"]))
    else:
        try:
            ops.Checkpoint.rollback(cp)
        except Exception as e:  # noqa: BLE001
            LOG.error("rollback failed: %s", e)
        st = {"status": "failed", "reason": why, "bridge": bridge, "port": dev}
        LOG.error("bridged networking failed and was rolled back: %s", why)
    _write_state(st)
    return st


def status() -> dict:
    st = _read_state() or {"status": "pending"}
    br = st.get("bridge") or config.load().get("management_bridge", "br0")
    st["bridge_present"] = is_bridge(br) if br else False
    return st


def retry() -> dict:
    """Forget a failed/skipped attempt and try again now (F2 → Management network on the console)."""
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
    return ensure()
