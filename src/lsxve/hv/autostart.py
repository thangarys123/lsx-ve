"""Ordered VM autostart / autostop (ESXi "Autostart configuration"), run by lsxve-vm-autostart.service.

``start`` (at boot): powers on the VMs with autostart enabled in their order; after each VM waits its start
delay, or less when "continue when the guest agent is up" is set and the guest agent connects.
``stop`` (at host shutdown, before libvirt-guests): reverse order, per-VM stop action (shut down guest /
power off / suspend to disk) and stop delay. Whatever is still running afterwards is handled by
libvirt-guests. Nothing runs while the host is in maintenance mode.
"""
from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET

from ..common import config, log
from . import domxml

LOG = log.get("lsxve.autostart")
DEFAULTS = {"enabled": True, "start_delay": 30, "stop_delay": 120, "stop_action": "shutdown",
            "wait_heartbeat": False}


def settings() -> dict:
    s = dict(DEFAULTS)
    try:
        data = json.loads(config.AUTOSTART_FILE.read_text())
        s.update({k: data[k] for k in DEFAULTS if k in data and isinstance(data[k], type(DEFAULTS[k]))})
    except (OSError, ValueError):
        pass
    return s


def maintenance() -> bool:
    try:
        return bool(json.loads(config.MAINTENANCE_FILE.read_text()).get("enabled"))
    except (OSError, ValueError):
        return False


def plan(conn) -> list:
    """Autostart VMs as (dom, effective settings), sorted by order (unordered ones last, by name)."""
    import libvirt  # type: ignore
    g = settings()
    out = []
    for d in conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_PERSISTENT):
        try:
            root = ET.fromstring(d.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
            info = domxml.parse_domain(d.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        except libvirt.libvirtError:
            continue
        if info["template"]:
            continue
        a = domxml.parse_autostart(root) or {}
        legacy = bool(d.autostart())
        if not (a.get("enabled") or legacy):
            continue
        eff = {"order": a.get("order") or 0,
               "start_delay": a["start_delay"] if a.get("start_delay", -1) >= 0 else g["start_delay"],
               "stop_delay": a["stop_delay"] if a.get("stop_delay", -1) >= 0 else g["stop_delay"],
               "stop_action": a.get("stop_action") or g["stop_action"],
               "wait_heartbeat": a.get("wait_heartbeat") or g["wait_heartbeat"]}
        out.append((d, eff))
        if legacy:  # take over from libvirt's unordered autostart
            try:
                d.setAutostart(0)
            except libvirt.libvirtError:
                pass
    out.sort(key=lambda x: (x[1]["order"] or 10 ** 6, x[0].name().lower()))
    return out


def _agent_connected(dom) -> bool:
    try:
        return 'state="connected"' in dom.XMLDesc(0) and "org.qemu.guest_agent.0" in dom.XMLDesc(0)
    except Exception:  # noqa: BLE001
        return False


def start(conn) -> int:
    if maintenance():
        LOG.warning("host is in maintenance mode: VM autostart skipped")
        return 0
    if not settings()["enabled"]:
        LOG.info("VM autostart is disabled for this host")
        return 0
    items = plan(conn)
    for i, (d, eff) in enumerate(items):
        if d.isActive():
            continue
        try:
            LOG.info("autostart %s (order %s)", d.name(), eff["order"] or "any")
            d.create()
        except Exception as e:  # noqa: BLE001
            LOG.error("autostart of %s failed: %s", d.name(), e)
            continue
        if i == len(items) - 1:
            break
        end = time.monotonic() + eff["start_delay"]
        while time.monotonic() < end:
            if eff["wait_heartbeat"] and _agent_connected(d):
                LOG.info("%s: guest agent is up, continuing", d.name())
                break
            time.sleep(2)
    return len(items)


def stop(conn) -> int:
    if not settings()["enabled"]:
        return 0
    n = 0
    for d, eff in reversed(plan(conn)):
        if not d.isActive():
            continue
        n += 1
        act = eff["stop_action"]
        try:
            LOG.info("autostop %s: %s", d.name(), act)
            if act == "poweroff":
                d.destroy()
                continue
            if act == "suspend":
                d.managedSave(0)
                continue
            d.shutdown()
        except Exception as e:  # noqa: BLE001
            LOG.error("autostop of %s failed: %s", d.name(), e)
            continue
        end = time.monotonic() + eff["stop_delay"]
        while time.monotonic() < end and d.isActive():
            time.sleep(2)
        if d.isActive():
            LOG.warning("%s did not shut down within %ss; left to libvirt-guests", d.name(), eff["stop_delay"])
    return n


def main() -> None:
    import libvirt  # type: ignore
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("start", "stop"):
        print("usage: lsxve-vm-autostart start|stop", file=sys.stderr)
        sys.exit(2)
    for attempt in range(30):
        try:
            conn = libvirt.open("qemu:///system")
            break
        except libvirt.libvirtError as e:
            if cmd == "stop" or attempt == 29:
                LOG.error("cannot connect to libvirt: %s", e)
                sys.exit(0 if cmd == "stop" else 1)
            time.sleep(2)
    n = start(conn) if cmd == "start" else stop(conn)
    LOG.info("%s: %d virtual machine(s) handled", cmd, n)


if __name__ == "__main__":
    main()
