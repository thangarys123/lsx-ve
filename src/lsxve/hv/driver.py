"""libvirt-backed hypervisor driver for LSX-VE.

All methods are blocking and are called from worker threads (tasks) or via
``run_in_executor``. The libvirt connection is shared and re-established on demand.
"""
from __future__ import annotations

import os
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional

from ..common import log
from . import domxml

LOG = log.get("lsxve.hv")

try:
    import libvirt  # type: ignore
    HAVE_LIBVIRT = True
except ImportError:  # dev machine without libvirt bindings
    libvirt = None
    HAVE_LIBVIRT = False

STATE_NAMES = {0: "nostate", 1: "running", 2: "running", 3: "paused", 4: "shutting-down", 5: "shutoff",
               6: "crashed", 7: "suspended"}
POOL_STATES = {0: "inactive", 1: "building", 2: "running", 3: "degraded", 4: "inaccessible"}
ISO_POOL = "iso"
DEFAULT_POOL = "default"
EVENT_NAMES = {0: "defined", 1: "undefined", 2: "started", 3: "suspended", 4: "resumed", 5: "stopped",
               6: "shutdown", 7: "pmsuspended", 8: "crashed"}


class HypervisorError(RuntimeError):
    pass


class Conflict(HypervisorError):
    pass


class NotFound(HypervisorError):
    pass


def _gib(n: int) -> int:
    return n * 1024 ** 3


class Hypervisor:
    def __init__(self, uri: str, on_domain_event: Optional[Callable[[dict], None]] = None):
        self.uri = uri
        self.on_domain_event = on_domain_event
        self._conn = None
        self._lock = threading.RLock()
        self._event_thread = None
        self._stats_prev: Dict[str, tuple] = {}
        self._guest_cache: Dict[str, tuple] = {}
        self.last_error: Optional[str] = None
        self._caps_cache: Optional[dict] = None
        if HAVE_LIBVIRT:
            libvirt.registerErrorHandler(lambda *a: None, None)  # silence stderr spam
            try:
                libvirt.virEventRegisterDefaultImpl()
                self._event_thread = threading.Thread(target=self._event_loop, name="libvirt-events", daemon=True)
                self._event_thread.start()
            except libvirt.libvirtError as e:  # pragma: no cover
                LOG.warning("libvirt event loop unavailable: %s", e)

    # ------------------------------------------------------------------ conn
    def _event_loop(self) -> None:
        while True:
            try:
                libvirt.virEventRunDefaultImpl()
            except Exception:  # pragma: no cover
                time.sleep(1)

    @property
    def available(self) -> bool:
        try:
            self.conn()
            return True
        except HypervisorError:
            return False

    def conn(self):
        if not HAVE_LIBVIRT:
            raise HypervisorError("libvirt Python bindings are not installed")
        with self._lock:
            if self._conn is not None:
                try:
                    if self._conn.isAlive():
                        return self._conn
                except libvirt.libvirtError:
                    pass
                self._conn = None
            try:
                c = libvirt.open(self.uri)
            except libvirt.libvirtError as e:
                self.last_error = e.get_error_message()
                raise HypervisorError(f"Cannot connect to hypervisor: {self.last_error}")
            try:
                c.setKeepAlive(5, 3)
                c.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self._lifecycle_cb, None)
                if hasattr(libvirt, "VIR_DOMAIN_EVENT_ID_AGENT_LIFECYCLE"):
                    c.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_AGENT_LIFECYCLE, self._agent_cb, None)
            except libvirt.libvirtError as e:  # pragma: no cover
                LOG.warning("cannot register domain events: %s", e)
            self._conn = c
            self.last_error = None
            self._caps_cache = None
            return c

    def close(self) -> None:
        """Close the libvirt connection cleanly (on shutdown). An abandoned connection makes
        virtqemud log "End of file while reading data: Input/output error"."""
        with self._lock:
            c, self._conn = self._conn, None
        if c is None:
            return
        try:
            c.close()
        except Exception:  # pragma: no cover - libvirt errors on an already broken connection
            pass

    def _agent_cb(self, conn, dom, state, reason, opaque):
        """Guest agent connected/disconnected: refresh guest info (hostname, IPs) in open clients."""
        try:
            u = dom.UUIDString()
            self._guest_cache.pop(u, None)
            if self.on_domain_event:
                self.on_domain_event({"uuid": u, "name": dom.name(), "event": "agent",
                                      "detail": state, "state": STATE_NAMES.get(dom.info()[0], "unknown")})
        except Exception:  # pragma: no cover
            LOG.exception("agent event callback failed")

    def _lifecycle_cb(self, conn, dom, event, detail, opaque):
        if self.on_domain_event:
            try:
                state = STATE_NAMES.get(dom.info()[0], "unknown")
            except libvirt.libvirtError:
                state = "shutoff" if event in (1, 5) else "unknown"
            try:
                self.on_domain_event({"uuid": dom.UUIDString(), "name": dom.name(),
                                      "event": EVENT_NAMES.get(event, str(event)), "detail": detail, "state": state})
            except Exception:  # pragma: no cover
                LOG.exception("domain event callback failed")

    # --------------------------------------------------------------- capabilities
    def capabilities(self) -> dict:
        if self._caps_cache:
            return self._caps_cache
        c = self.conn()
        caps = ET.fromstring(c.getCapabilities())
        domain_type = "kvm" if os.path.exists("/dev/kvm") else "qemu"
        try:
            dcaps = ET.fromstring(c.getDomainCapabilities(None, "x86_64", "q35", domain_type, 0))
        except libvirt.libvirtError:
            dcaps = None
        uefi = secure = tpm = False
        max_vcpus = 0
        if dcaps is not None:
            fw_values = [v.text for v in dcaps.findall("os/enum[@name='firmware']/value")]
            uefi = "efi" in fw_values or bool(dcaps.findall("os/loader/value"))
            secure = "yes" in [v.text for v in dcaps.findall("os/loader/enum[@name='secure']/value")]
            tpm_models = [v.text for v in dcaps.findall("devices/tpm/enum[@name='backendModel']/value")]
            tpm = dcaps.find("devices/tpm") is not None and dcaps.find("devices/tpm").get("supported") == "yes" \
                and "emulator" in tpm_models
            mv = dcaps.find("vcpu")
            max_vcpus = int(mv.get("max")) if mv is not None else 0
        lv = c.getLibVersion()
        hv = c.getVersion()
        self._caps_cache = {
            "domain_type": domain_type, "accelerated": domain_type == "kvm",
            "libvirt_version": f"{lv // 1000000}.{lv // 1000 % 1000}.{lv % 1000}",
            "qemu_version": f"{hv // 1000000}.{hv // 1000 % 1000}.{hv % 1000}",
            "uefi": uefi, "secure_boot": secure, "tpm": tpm and os.path.exists("/usr/bin/swtpm"),
            "max_vcpus": max_vcpus, "host_uuid": caps.findtext("host/uuid"),
            "hostname": c.getHostname(),
        }
        return self._caps_cache

    def status(self) -> dict:
        try:
            caps = self.capabilities()
            return {"available": True, **caps}
        except HypervisorError as e:
            return {"available": False, "error": str(e)}
        except Exception as e:  # libvirtError while querying
            return {"available": False, "error": str(e)}

    # ------------------------------------------------------------------ helpers
    def _dom(self, uuid: str):
        try:
            return self.conn().lookupByUUIDString(uuid)
        except libvirt.libvirtError:
            raise NotFound(f"Virtual machine {uuid} not found")

    @staticmethod
    def _state(dom) -> str:
        return STATE_NAMES.get(dom.info()[0], "unknown")

    def _xml(self, dom, inactive: bool = False) -> str:
        flags = libvirt.VIR_DOMAIN_XML_SECURE
        if inactive:
            flags |= libvirt.VIR_DOMAIN_XML_INACTIVE
        return dom.XMLDesc(flags)

    def _pool(self, name: str):
        try:
            return self.conn().storagePoolLookupByName(name)
        except libvirt.libvirtError:
            raise NotFound(f"Datastore '{name}' not found")

    def _pool_is_block(self, pool) -> bool:
        return domxml.parse_pool(pool.XMLDesc(0))["type"] in ("logical", "iscsi", "disk", "scsi", "mpath")

    def _vm_summary(self, dom, stats: Optional[dict] = None) -> dict:
        info = domxml.parse_domain(self._xml(dom, inactive=False))
        state = self._state(dom)
        s = {
            "uuid": dom.UUIDString(), "name": dom.name(), "state": state, "id": dom.ID() if dom.isActive() else None,
            "vcpus": info["vcpus"], "memory_mib": info["memory_mib"], "os_type": info["os_type"],
            "os_variant": info["os_variant"], "firmware": info["firmware"], "template": info["template"],
            "autostart": bool((info.get("autostart_cfg") or {}).get("enabled")) or bool(dom.autostart()),
            "persistent": bool(dom.isPersistent()),
            "description": info["description"], "created_by": info["created_by"],
            "disk_count": len(info["disks"]), "nic_count": len(info["nics"]),
            "has_snapshots": dom.snapshotNum(0) > 0 if dom.isPersistent() else False,
        }
        if stats:
            s.update(stats)
        return s

    # ------------------------------------------------------------------ VMs
    def list_vms(self) -> List[dict]:
        c = self.conn()
        stats = self.vm_stats()
        out = []
        for d in c.listAllDomains(0):
            try:
                out.append(self._vm_summary(d, stats.get(d.UUIDString())))
            except libvirt.libvirtError:
                continue  # domain vanished while listing
        out.sort(key=lambda v: v["name"].lower())
        return out

    def vm_stats(self) -> Dict[str, dict]:
        """CPU % (of allocated vCPUs), memory, disk and network counters for running VMs."""
        c = self.conn()
        flags = (libvirt.VIR_DOMAIN_STATS_STATE | libvirt.VIR_DOMAIN_STATS_CPU_TOTAL |
                 libvirt.VIR_DOMAIN_STATS_BALLOON | libvirt.VIR_DOMAIN_STATS_INTERFACE |
                 libvirt.VIR_DOMAIN_STATS_BLOCK | libvirt.VIR_DOMAIN_STATS_VCPU)
        try:
            records = c.getAllDomainStats(flags, libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE)
        except libvirt.libvirtError:
            return {}
        now = time.monotonic()
        out = {}
        for dom, st in records:
            u = dom.UUIDString()
            cpu_time = st.get("cpu.time", 0)
            vcpus = st.get("vcpu.current", 1) or 1
            rx = sum(st.get(f"net.{i}.rx.bytes", 0) for i in range(st.get("net.count", 0)))
            tx = sum(st.get(f"net.{i}.tx.bytes", 0) for i in range(st.get("net.count", 0)))
            rd = sum(st.get(f"block.{i}.rd.bytes", 0) for i in range(st.get("block.count", 0)))
            wr = sum(st.get(f"block.{i}.wr.bytes", 0) for i in range(st.get("block.count", 0)))
            prev = self._stats_prev.get(u)
            cpu_pct = rxs = txs = rds = wrs = 0.0
            if prev:
                dt = max(0.001, now - prev[0])
                cpu_pct = min(100.0, max(0.0, (cpu_time - prev[1]) / (dt * 1e9 * vcpus) * 100))
                rxs, txs = max(0, rx - prev[2]) / dt, max(0, tx - prev[3]) / dt
                rds, wrs = max(0, rd - prev[4]) / dt, max(0, wr - prev[5]) / dt
            self._stats_prev[u] = (now, cpu_time, rx, tx, rd, wr)
            cur = st.get("balloon.current", 0) * 1024
            unused = st.get("balloon.unused")
            avail = st.get("balloon.available")
            mem_used = (avail - unused) * 1024 if unused is not None and avail else None
            out[u] = {"cpu_pct": round(cpu_pct, 1), "mem_assigned": cur, "mem_guest_used": mem_used,
                      "net_rx": round(rxs), "net_tx": round(txs), "disk_read": round(rds), "disk_write": round(wrs)}
        return out

    def vm_brief(self, uuid: str) -> dict:
        dom = self._dom(uuid)
        info = domxml.parse_domain(self._xml(dom, inactive=dom.isPersistent()))
        return {"uuid": uuid, "name": dom.name(), "template": info["template"], "state": self._state(dom)}

    def vm_detail(self, uuid: str) -> dict:
        dom = self._dom(uuid)
        live = domxml.parse_domain(self._xml(dom))
        persistent = domxml.parse_domain(self._xml(dom, inactive=True)) if dom.isPersistent() else live
        summary = self._vm_summary(dom, self.vm_stats().get(uuid))
        c = self.conn()
        for d in live["disks"]:
            d["capacity"] = d["allocation"] = None
            d["datastore"] = None
            if d["path"]:
                try:
                    v = c.storageVolLookupByPath(d["path"])
                    _, cap, alloc = v.info()
                    d.update(capacity=cap, allocation=alloc, datastore=v.storagePoolLookupByVolume().name(),
                             volume=v.name())
                except libvirt.libvirtError:
                    pass
        for cd in live["cdroms"]:
            cd["iso"] = os.path.basename(cd["path"]) if cd["path"] else None
        ips: List[dict] = []
        ip_source = None
        agent_state = live.get("guest_agent")
        if dom.isActive():
            # guest agent first (exact), then libvirt DHCP leases (NAT networks), then the host ARP/neighbour
            # table - the only source on bridged networks when the guest has no agent
            sources = [("agent", libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT),
                       ("lease", libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE),
                       ("arp", getattr(libvirt, "VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP", 2))]
            for label, src in sources:
                if label == "agent" and agent_state != "connected":
                    continue
                try:
                    res = dom.interfaceAddresses(src, 0)
                except libvirt.libvirtError:
                    continue
                for ifname, val in (res or {}).items():
                    if ifname == "lo":
                        continue
                    for a in val.get("addrs") or []:
                        if a.get("addr", "").startswith(("127.", "::1")):
                            continue
                        ips.append({"interface": ifname, "mac": val.get("hwaddr"), "address": a.get("addr"),
                                    "prefix": a.get("prefix"),
                                    "link_local": a.get("addr", "").lower().startswith("fe80")})
                if ips:
                    ip_source = label
                    break
            ips.sort(key=lambda a: (a["link_local"], ":" in a["address"]))
        pending = (persistent["vcpus"] != live["vcpus"] or persistent["memory_mib"] != live["memory_mib"]
                   or persistent["mem_locked"] != live["mem_locked"] or persistent["video"] != live["video"]
                   or persistent["cpu_hotadd"] != live["cpu_hotadd"] or persistent["mem_hotadd"] != live["mem_hotadd"]
                   or persistent["serials"] != live["serials"]) if dom.isActive() else False
        live_graphics = live.get("graphics") or {}
        detail = {
            **summary, "hardware": {**{k: live[k] for k in ("disks", "cdroms", "nics", "firmware", "secure_boot",
                                                            "tpm", "machine", "domain_type", "boot_order",
                                                            "usb_controllers")},
                         "serials": persistent["serials"]},
            "config": {"vcpus": persistent["vcpus"], "memory_mib": persistent["memory_mib"],
                       "boot_order": persistent["boot_order"]},
            "resources": {**{k: persistent[k] for k in ("cpu_shares", "cpu_quota_us", "mem_locked", "cpu_hotadd",
                                                        "vcpus_max", "video", "mem_hotadd", "mem_max_mib")},
                          "mem_dimms": live["mem_dimms"], "mem_slots": domxml.MEM_SLOTS},
            "hostdevs": persistent["hostdevs"],
            "autostart_cfg": self._autostart_cfg(dom, persistent),
            "pending_restart": pending, "guest_ips": ips, "guest_ip_source": ip_source,
            "guest": self._guest_info(dom, agent_state),
            "host": {"hostname": self.capabilities().get("hostname")},
            "console": {"type": live_graphics.get("type"), "available": dom.isActive() and live_graphics.get("port", -1) > 0},
            "snapshots": self.list_snapshots(uuid) if dom.isPersistent() else [],
        }
        return detail

    def _guest_info(self, dom, agent_state: Optional[str]) -> dict:
        """Guest OS identity reported by the QEMU guest agent (cached 30 s; the agent can be slow)."""
        out = {"agent": agent_state if dom.isActive() else ("configured" if agent_state else None),
               "hostname": None, "os_name": None, "os_version": None, "kernel": None, "timezone": None, "users": None}
        if not dom.isActive() or agent_state != "connected":
            return out
        u = dom.UUIDString()
        hit = self._guest_cache.get(u)
        if hit and time.monotonic() - hit[0] < 30:
            return {**out, **hit[1]}
        got: dict = {}
        try:
            flags = 0
            for n in ("VIR_DOMAIN_GUEST_INFO_OS", "VIR_DOMAIN_GUEST_INFO_HOSTNAME", "VIR_DOMAIN_GUEST_INFO_TIMEZONE",
                      "VIR_DOMAIN_GUEST_INFO_USERS"):
                flags |= getattr(libvirt, n, 0)
            info = dom.guestInfo(flags, 0) or {}
            got = {"hostname": info.get("hostname"),
                   "os_name": info.get("os.pretty-name") or info.get("os.name"),
                   "os_version": info.get("os.version"), "kernel": info.get("os.kernel-release"),
                   "timezone": info.get("timezone.name"), "users": info.get("user.count")}
        except (libvirt.libvirtError, AttributeError):
            got = {}
        self._guest_cache[u] = (time.monotonic(), got)
        return {**out, **got}

    # -- creation -------------------------------------------------------
    def _alloc_disk(self, pool_name: str, vol_name: str, size_gib: int, thick: bool, fmt: Optional[str] = None):
        pool = self._pool(pool_name)
        if not pool.isActive():
            raise HypervisorError(f"Datastore '{pool_name}' is not active")
        block = self._pool_is_block(pool)
        fmt = "raw" if block else (fmt or "qcow2")
        name = vol_name if block else vol_name + ('.qcow2' if fmt == 'qcow2' else '.img')
        try:
            pool.storageVolLookupByName(name)
            raise Conflict(f"A disk named {name} already exists in datastore {pool_name}")
        except libvirt.libvirtError:
            pass
        _, cap, alloc, avail = pool.info()
        need = _gib(size_gib) if (thick or block) else 0
        if need > avail:
            raise HypervisorError(f"Not enough free space in datastore {pool_name}")
        flags = libvirt.VIR_STORAGE_VOL_CREATE_PREALLOC_METADATA if (fmt == "qcow2" and not thick) else 0
        vol = pool.createXML(domxml.build_volume(name, _gib(size_gib), "" if block else fmt, thick), flags)
        return vol, block, fmt

    def create_vm(self, spec: dict, username: str, progress: Callable[[int, str], None]) -> dict:
        c = self.conn()
        caps = self.capabilities()
        try:
            c.lookupByName(spec["name"])
            raise Conflict(f"A virtual machine named '{spec['name']}' already exists")
        except libvirt.libvirtError:
            pass
        if spec["firmware"] == "uefi" and not caps["uefi"]:
            raise HypervisorError("UEFI firmware (OVMF) is not available on this host")
        if spec.get("secure_boot") and not caps["secure_boot"]:
            raise HypervisorError("Secure Boot firmware is not available on this host")
        cdrom_path = None
        if spec.get("iso"):
            cdrom_path = self.iso_path(spec["iso"])
        created = []
        disks = []
        try:
            for i, d in enumerate(spec["disks"]):
                progress(10 + 50 * i // max(1, len(spec["disks"])), f"Creating disk {i + 1}")
                if d.get("existing"):
                    v = self._pool(d["datastore"]).storageVolLookupByName(d["existing"])
                    info = domxml.parse_volume(v.XMLDesc(0))
                    if info["format"] not in ("qcow2", "raw", "", None):
                        raise Conflict(f"{d['existing']} is a {info['format']} image; use Import VM to convert it "
                                       f"to qcow2 first")
                    disks.append({"path": v.path(), "block": self._pool_is_block(self._pool(d["datastore"])),
                                  "format": info["format"] or "raw", "bus": d["bus"]})
                    continue
                vol, block, fmt = self._alloc_disk(d["datastore"], f"{spec['name']}_{i}", d["size_gib"],
                                                   d.get("provisioning") == "thick", d.get("format"))
                created.append(vol)
                disks.append({"path": vol.path(), "block": block, "format": fmt, "bus": d["bus"]})
            progress(70, "Defining virtual machine")
            if spec.get("cpu_hotadd"):
                spec = {**spec, "vcpus_max": max(spec["vcpus"], self.max_vcpus())}
            if spec.get("mem_hotadd"):
                spec = {**spec, "mem_max_mib": max(spec["memory_mib"], self.max_memory_mib())}
            root = domxml.build_domain(spec, caps["domain_type"], disks, cdrom_path, caps["tpm"], username)
            dom = c.defineXMLFlags(domxml.tostring(root), libvirt.VIR_DOMAIN_DEFINE_VALIDATE)
        except Exception:
            for v in created:
                try:
                    v.delete(0)
                except libvirt.libvirtError:
                    pass
            raise
        if spec.get("autostart"):
            self.set_autostart(dom.UUIDString(), True)
        if spec.get("power_on"):
            progress(90, "Powering on")
            dom.create()
        return {"uuid": dom.UUIDString(), "name": dom.name(), "message": f"Virtual machine {dom.name()} created"}

    # -- power ----------------------------------------------------------
    def power(self, uuid: str, action: str) -> str:
        dom = self._dom(uuid)
        state = self._state(dom)
        if action == "start":
            if domxml.parse_domain(self._xml(dom, True)).get("template"):
                raise Conflict("Templates cannot be powered on. Deploy a virtual machine from the template instead.")
            if state == "paused":
                dom.resume()
            elif state in ("shutoff", "crashed"):
                dom.create()
            elif state == "suspended":
                dom.pMWakeup(0)
            else:
                raise Conflict(f"Cannot power on a VM that is {state}")
        elif action == "shutdown":
            if state not in ("running", "paused"):
                raise Conflict(f"Cannot shut down a VM that is {state}")
            dom.shutdown()
        elif action == "poweroff":
            if not dom.isActive():
                raise Conflict("VM is already powered off")
            dom.destroy()
        elif action == "reboot":
            if state != "running":
                raise Conflict("VM must be running to restart the guest")
            dom.reboot(0)
        elif action == "reset":
            if not dom.isActive():
                raise Conflict("VM must be running to reset")
            dom.reset(0)
        elif action == "suspend":
            if state != "running":
                raise Conflict("Only a running VM can be paused")
            dom.suspend()
        elif action == "resume":
            if state != "paused":
                raise Conflict("VM is not paused")
            dom.resume()
        else:
            raise HypervisorError("unknown power action")
        return self._state(dom)

    def wait_state(self, uuid: str, states: tuple, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self._state(self._dom(uuid)) in states:
                    return True
            except NotFound:
                return True
            time.sleep(1)
        return False

    AUTOSTART_DEFAULTS = {"enabled": False, "order": 0, "start_delay": -1, "stop_action": "", "stop_delay": -1,
                          "wait_heartbeat": False}

    def _autostart_cfg(self, dom, info: Optional[dict] = None) -> dict:
        info = info or domxml.parse_domain(self._xml(dom, inactive=dom.isPersistent()))
        cfg = dict(self.AUTOSTART_DEFAULTS)
        cfg.update(info.get("autostart_cfg") or {})
        try:
            if dom.autostart():  # set by an older LSX-VE or by virsh
                cfg["enabled"] = True
        except libvirt.libvirtError:
            pass
        return cfg

    def set_autostart(self, uuid: str, enabled: Optional[bool] = None, **opts) -> dict:
        """Autostart is LSX-VE metadata executed in order by lsxve-vm-autostart.service (ESXi autostart
        configuration: order, start delay, stop action, stop delay). libvirt's own unordered autostart is
        switched off so the two never race."""
        dom = self._dom(uuid)
        cfg = self._autostart_cfg(dom)
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        cfg.update({k: val for k, val in opts.items() if k in self.AUTOSTART_DEFAULTS and val is not None})
        if cfg["enabled"] and domxml.parse_domain(self._xml(dom, True)).get("template"):
            raise Conflict("Templates cannot be set to autostart")
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG | (libvirt.VIR_DOMAIN_AFFECT_LIVE if dom.isActive() else 0)
        out = {**cfg, "enabled": 1 if cfg["enabled"] else 0, "wait_heartbeat": 1 if cfg["wait_heartbeat"] else 0}
        dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, domxml.build_autostart(out), "lsxas", domxml.AS_NS,
                        flags)
        try:
            if dom.autostart():
                dom.setAutostart(0)
        except libvirt.libvirtError:
            pass
        return cfg

    def autostart_list(self) -> List[dict]:
        out = []
        for d in self.conn().listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_PERSISTENT):
            try:
                info = domxml.parse_domain(self._xml(d, inactive=True))
                if info["template"]:
                    continue
                out.append({"uuid": d.UUIDString(), "name": d.name(), "state": self._state(d),
                            **self._autostart_cfg(d, info)})
            except libvirt.libvirtError:
                continue
        out.sort(key=lambda x: (not x["enabled"], x["order"] or 10 ** 6, x["name"].lower()))
        return out

    def max_memory_mib(self) -> int:
        """Upper limit for memory hot add: the host's RAM (rounded down to whole GiB)."""
        try:
            mib = int(self.conn().getInfo()[1])
        except (libvirt.libvirtError, IndexError, TypeError, ValueError):
            mib = 0
        return max(1024, mib // 1024 * 1024)

    def max_vcpus(self) -> int:
        caps = self.capabilities()
        n = os.cpu_count() or 1
        return max(1, min(n, caps.get("max_vcpus") or n))

    # -- guest tools ----------------------------------------------------
    def mount_media_path(self, uuid: str, path: Optional[str]) -> dict:
        """Insert an ISO by absolute path (guest tools) into the first CD/DVD drive; adds a drive when the VM
        is powered off and has none."""
        dom = self._dom(uuid)
        running = dom.isActive()
        cfg = domxml.parse_domain(self._xml(dom, inactive=True))
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG | (libvirt.VIR_DOMAIN_AFFECT_LIVE if running else 0)
        if cfg["cdroms"]:
            tgt = cfg["cdroms"][0]["target"]
            dom.updateDeviceFlags(domxml.tostring(domxml.build_cdrom(path, tgt)), flags)
            return {"message": f"Media mounted on {tgt}", "target": tgt}
        if running:
            raise Conflict("The VM has no CD/DVD drive. Power it off; LSX-VE then adds a SATA CD/DVD drive.")
        used = [x["target"] for x in cfg["disks"] + cfg["cdroms"]]
        tgt = domxml.disk_target_names("sata", used, 1)[0]
        dom.attachDeviceFlags(domxml.tostring(domxml.build_cdrom(path, tgt)), libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        return {"message": f"CD/DVD drive {tgt} added with the media", "target": tgt}

    # -- host devices (passthrough) ---------------------------------------
    def list_host_devices(self) -> dict:
        c = self.conn()
        users: Dict[str, List[str]] = {}
        for d in c.listAllDomains(0):
            try:
                for h in domxml.parse_domain(self._xml(d, inactive=d.isPersistent()))["hostdevs"]:
                    users.setdefault(h["id"], []).append(d.name())
            except libvirt.libvirtError:
                continue
        pci, usb = [], []
        flags = libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_PCI_DEV | libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_USB_DEV
        for nd in c.listAllDevices(flags):
            try:
                r = ET.fromstring(nd.XMLDesc(0))
            except libvirt.libvirtError:
                continue
            cap = r.find("capability")
            if cap is None:
                continue
            if cap.get("type") == "pci":
                addr = "%04x:%02x:%02x.%x" % tuple(int(cap.findtext(k) or "0") for k in ("domain", "bus", "slot", "function"))
                grp = cap.find("iommuGroup")
                pci.append({"id": f"pci:{addr}", "address": addr, "vendor": cap.findtext("vendor") or "",
                            "product": cap.findtext("product") or "", "class": cap.findtext("class") or "",
                            "driver": r.findtext("driver/name") or "",
                            "iommu_group": int(grp.get("number")) if grp is not None else None,
                            "used_by": users.get(f"pci:{addr}", [])})
            elif cap.get("type") == "usb_device":
                ven, prod = cap.find("vendor"), cap.find("product")
                if ven is None or prod is None:
                    continue
                vid, pid = ven.get("id", "0x0")[2:].lower().zfill(4), prod.get("id", "0x0")[2:].lower().zfill(4)
                if vid == "1d6b":  # Linux Foundation root hubs
                    continue
                usb.append({"id": f"usb:{vid}:{pid}", "vendor": vid, "product": pid, "vendor_name": ven.text or "",
                            "product_name": prod.text or "", "bus": cap.findtext("bus"), "device": cap.findtext("device"),
                            "used_by": users.get(f"usb:{vid}:{pid}", [])})
        pci.sort(key=lambda x: x["address"])
        usb.sort(key=lambda x: (x["vendor"], x["product"]))
        return {"pci": pci, "usb": usb}

    # -- edit -----------------------------------------------------------
    def update_vm(self, uuid: str, changes: dict) -> dict:
        c = self.conn()
        dom = self._dom(uuid)
        running = dom.isActive()
        notes = []
        root = ET.fromstring(self._xml(dom, inactive=True))
        if "name" in changes and changes["name"] != dom.name():
            if running:
                raise Conflict("Power off the VM before renaming it")
            try:
                c.lookupByName(changes["name"])
                raise Conflict(f"A virtual machine named '{changes['name']}' already exists")
            except libvirt.libvirtError:
                pass
            if dom.snapshotNum(0):
                raise Conflict("VMs with snapshots cannot be renamed")
            dom.rename(changes["name"], 0)
            root = ET.fromstring(self._xml(dom, inactive=True))
        cur = domxml.parse_domain(domxml.tostring(root))
        live_now = domxml.parse_domain(self._xml(dom)) if running else cur
        hot_live = []
        if "vcpus" in changes or "cpu_hotadd" in changes:
            n = changes.get("vcpus", cur["vcpus"])
            hot = changes.get("cpu_hotadd", cur["cpu_hotadd"])
            vmax = max(n, self.max_vcpus()) if hot else n
            domxml.set_vcpus(root, n, hot, vmax)
            if running and "cpu_hotadd" in changes and hot != live_now["cpu_hotadd"]:
                notes.append("CPU hot add setting takes effect at next power on")
            if running and n != live_now["vcpus"]:
                if live_now["cpu_hotadd"] and live_now["vcpus"] < n <= live_now["vcpus_max"]:
                    hot_live.append(n)
                else:
                    notes.append("vCPU change takes effect at next power on" +
                                 ("" if live_now["cpu_hotadd"] else " (enable CPU hot add to add vCPUs live)"))
        if any(k in changes for k in ("cpu_shares", "cpu_quota_us", "mem_lock")):
            domxml.set_tuning(root, changes.get("cpu_shares"), changes.get("cpu_quota_us"), changes.get("mem_lock"))
            if running and "mem_lock" in changes and changes["mem_lock"] != live_now["mem_locked"]:
                notes.append("Memory reservation takes effect at next power on")
        if "video_model" in changes or "video_vram_mib" in changes:
            vid = cur["video"]
            domxml.set_video(root, changes.get("video_model", vid["model"] or "virtio"),
                             changes.get("video_vram_mib", vid["vram_mib"]))
            if running:
                notes.append("Video card change takes effect at next power on")
        for lim in changes.get("disk_limits", []):
            el = next((d for d in root.findall("devices/disk") if d.get("device", "disk") == "disk"
                       and d.find("target") is not None and d.find("target").get("dev") == lim["target"]), None)
            if el is None:
                raise NotFound(f"Disk {lim['target']} not found")
            domxml.set_disk_iotune(el, lim["iops"], lim["bps"])
        hot_mem = 0
        if "memory_mib" in changes or "mem_hotadd" in changes:
            new = changes.get("memory_mib", cur["memory_mib"])
            hot = changes.get("mem_hotadd", cur["mem_hotadd"])
            delta = new - live_now["memory_mib"]
            if (running and delta > 0 and hot and cur["mem_hotadd"] and live_now["mem_hotadd"]
                    and cur["memory_mib"] == live_now["memory_mib"]):
                # live: plug a DIMM (libvirt adds it to the persistent configuration as well)
                if delta % domxml.MEM_HOTADD_STEP:
                    raise Conflict(f"Memory can be hot-added in steps of {domxml.MEM_HOTADD_STEP} MiB")
                if new > live_now["mem_max_mib"]:
                    raise Conflict(f"Hot add limit of this VM is {live_now['mem_max_mib']} MiB; power off to go higher")
                if live_now["mem_dimms"] >= domxml.MEM_SLOTS:
                    raise Conflict("All memory hot add slots are in use; power off the VM to change its memory")
                hot_mem = delta
            else:
                mx = 0
                if hot:
                    mx = cur["mem_max_mib"] if cur["mem_hotadd"] and cur["mem_max_mib"] >= new else                         max(new, self.max_memory_mib())
                domxml.set_memory(root, new, hot, mx)
                if running and new != live_now["memory_mib"]:
                    notes.append("Memory change takes effect at next power on" +
                                 (" (enable memory hot add to add memory live)" if new > live_now["memory_mib"]
                                  and not live_now["mem_hotadd"] else ""))
                if running and hot != live_now["mem_hotadd"]:
                    notes.append("Memory hot add setting takes effect at next power on")
        if "description" in changes:
            el = root.find("description")
            if el is None:
                el = ET.SubElement(root, "description")
            el.text = changes["description"]
        if "boot_order" in changes:
            domxml.apply_boot_order(root, changes["boot_order"])
            if running:
                notes.append("Boot order change takes effect at next power on")
        if "os_type" in changes:
            domxml.set_metadata(root, "os-type", changes["os_type"])
        if "os_variant" in changes:
            domxml.set_metadata(root, "os-variant", changes["os_variant"])
        c.defineXML(domxml.tostring(root))
        dom = self._dom(uuid)

        live_flag = libvirt.VIR_DOMAIN_AFFECT_CONFIG | (libvirt.VIR_DOMAIN_AFFECT_LIVE if running else 0)
        if hot_mem:
            dom.attachDeviceFlags(domxml.tostring(domxml.build_dimm(hot_mem)), live_flag)
            notes.append(f"{hot_mem} MiB of memory hot-added")
        if (changes.get("add_serials") or changes.get("remove_serials")) and running:
            raise Conflict("Power off the VM to add or remove serial ports")
        for port in changes.get("remove_serials", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            sp = next((x for x in cfg["serials"] if x["port"] == port), None)
            if sp is None or sp["type"] != "file":
                raise NotFound(f"Serial port {port + 1} is not a file-backed serial port")
            dom.detachDeviceFlags(domxml.tostring(domxml.build_serial_file(port, sp["path"])),
                                  libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        for sp in changes.get("add_serials", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            free = [n for n in range(1, domxml.SERIAL_MAX) if n not in {x["port"] for x in cfg["serials"]}]
            if not free:
                raise Conflict(f"A VM can have at most {domxml.SERIAL_MAX} serial ports")
            path = os.path.join(self.pool_dir(sp["datastore"]), sp["file"])
            if path in self._paths_in_use() or any(x["path"] == path for x in cfg["serials"]):
                raise Conflict(f"{sp['file']} is already used by a virtual machine")
            dom.attachDeviceFlags(domxml.tostring(domxml.build_serial_file(free[0], path)),
                                  libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            notes.append(f"Serial port {free[0] + 1} writes to [{sp['datastore']}] {sp['file']}")
        for n in hot_live:
            dom.setVcpusFlags(n, libvirt.VIR_DOMAIN_AFFECT_LIVE)
            notes.append(f"{n} vCPUs hot-added")
        if running and ("cpu_shares" in changes or "cpu_quota_us" in changes):
            params = {}
            if "cpu_shares" in changes:
                params["cpu_shares"] = changes["cpu_shares"]
            if "cpu_quota_us" in changes:
                params["global_period"] = domxml.CPU_PERIOD_US
                params["global_quota"] = changes["cpu_quota_us"] if changes["cpu_quota_us"] > 0 else -1
            try:
                dom.setSchedulerParametersFlags(params, libvirt.VIR_DOMAIN_AFFECT_LIVE)
            except libvirt.libvirtError as e:
                notes.append(f"CPU allocation saved; live update failed ({e.get_error_message()}), applies at next power on")
        if running:
            for lim in changes.get("disk_limits", []):
                try:
                    dom.setBlockIoTune(lim["target"], {"total_iops_sec": lim["iops"], "total_bytes_sec": lim["bps"]},
                                       libvirt.VIR_DOMAIN_AFFECT_LIVE)
                except libvirt.libvirtError as e:
                    notes.append(f"I/O limit of {lim['target']} applies at next power on ({e.get_error_message()})")
        for rz in changes.get("resize_disks", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            d = next((x for x in cfg["disks"] if x["target"] == rz["target"]), None)
            if d is None or not d["path"]:
                raise NotFound(f"Disk {rz['target']} not found")
            if self._disk_has_snapshots(dom):
                raise Conflict("Delete the VM's snapshots before extending a disk")
            vol = c.storageVolLookupByPath(d["path"])
            new = rz["size_gib"] * 1024 ** 3
            cap = vol.info()[1]
            if new <= cap:
                raise Conflict(f"Disk {rz['target']} is already {cap / 1024 ** 3:.1f} GiB; disks can only grow")
            if running:
                dom.blockResize(rz["target"], new, libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
            else:
                vol.resize(new, 0)
            notes.append(f"Disk {rz['target']} extended to {rz['size_gib']} GiB - extend the partition and file "
                         f"system inside the guest")
        for hid in changes.get("remove_hostdevs", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            h = next((x for x in cfg["hostdevs"] if x["id"] == hid), None)
            if h is None:
                raise NotFound(f"Device {hid} is not attached")
            if running and h["type"] == "pci":
                raise Conflict("Power off the VM to remove a PCI device")
            dom.detachDeviceFlags(domxml.tostring(domxml.build_hostdev(h)), live_flag)
        for h in changes.get("add_hostdevs", []):
            if running and h["type"] == "pci":
                raise Conflict("Power off the VM to add a PCI device")
            dom.attachDeviceFlags(domxml.tostring(domxml.build_hostdev(h)), live_flag)
        if "cdrom" in changes:
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            if not cfg["cdroms"]:
                raise HypervisorError("VM has no CD/DVD drive")
            tgt = cfg["cdroms"][0]["target"]
            path = self.iso_path(changes["cdrom"]) if changes["cdrom"] else None
            dom.updateDeviceFlags(domxml.tostring(domxml.build_cdrom(path, tgt)), live_flag)
        for t in changes.get("remove_disks", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            match = [d for d in cfg["disks"] if d["target"] == t]
            if not match:
                raise NotFound(f"Disk {t} not found")
            d = match[0]
            el = domxml.build_disk(d["path"], d["path"].startswith("/dev/"), d["format"], d["target"], d["bus"])
            dom.detachDeviceFlags(domxml.tostring(el), live_flag)
            if changes.get("delete_removed_disks"):
                try:
                    c.storageVolLookupByPath(d["path"]).delete(0)
                except libvirt.libvirtError as e:
                    notes.append(f"Disk {t} detached but volume not deleted: {e.get_error_message()}")
        for i, d in enumerate(changes.get("add_disks", [])):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            used = [x["target"] for x in cfg["disks"] + cfg["cdroms"]]
            tgt = domxml.disk_target_names(d["bus"], used, 1)[0]
            if d.get("existing"):
                v = self._pool(d["datastore"]).storageVolLookupByName(d["existing"])
                fmt = domxml.parse_volume(v.XMLDesc(0))["format"] or "raw"
                path, block = v.path(), self._pool_is_block(self._pool(d["datastore"]))
            else:
                vol, block, fmt = self._alloc_disk(d["datastore"], f"{dom.name()}_{int(time.time())}_{i}",
                                                   d["size_gib"], d.get("provisioning") == "thick", d.get("format"))
                path = vol.path()
            if d["bus"] == "scsi" and not any(x["bus"] == "scsi" for x in cfg["disks"]):
                ctl = ET.Element("controller", {"type": "scsi", "model": "virtio-scsi"})
                dom.attachDeviceFlags(domxml.tostring(ctl), live_flag)
            if running and d["bus"] == "sata":
                raise Conflict("SATA disks cannot be hot-added; power off the VM or use VirtIO/SCSI")
            dom.attachDeviceFlags(domxml.tostring(domxml.build_disk(path, block, fmt if fmt in ("qcow2", "raw") else "raw",
                                                                    tgt, d["bus"])), live_flag)
        for mac in changes.get("remove_nics", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            match = [n for n in cfg["nics"] if n["mac"] == mac]
            if not match:
                raise NotFound(f"Network adapter {mac} not found")
            n = match[0]
            dom.detachDeviceFlags(domxml.tostring(domxml.build_nic(n["type"], n["source"], n["model"], mac)), live_flag)
        for n in changes.get("update_nics", []):
            cfg = domxml.parse_domain(self._xml(dom, inactive=True))
            match = [x for x in cfg["nics"] if x["mac"] == n["mac"]]
            if not match:
                raise NotFound(f"Network adapter {n['mac']} not found")
            cur = match[0]
            if n["type"] != cur["type"] or n.get("model", cur["model"]) != cur["model"]:
                if running:
                    raise Conflict("Changing adapter type or model requires the VM to be powered off")
            el = domxml.build_nic(n["type"], n["source"], n.get("model", cur["model"]), n["mac"])
            if "connected" in n:
                ET.SubElement(el, "link", {"state": "up" if n["connected"] else "down"})
            dom.updateDeviceFlags(domxml.tostring(el), live_flag)
        for n in changes.get("add_nics", []):
            dom.attachDeviceFlags(domxml.tostring(domxml.build_nic(n["type"], n["source"], n["model"], n.get("mac"))),
                                  live_flag)
        if "description" in changes and running:
            try:  # notes are metadata: show them on the running VM at once, not only after the next power on
                dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_DESCRIPTION, changes["description"] or None, None, None,
                                libvirt.VIR_DOMAIN_AFFECT_LIVE)
            except libvirt.libvirtError as e:
                LOG.warning("live description update failed: %s", e)
        if "autostart" in changes:
            self.set_autostart(uuid, changes["autostart"])
        return {"message": "; ".join(notes) or "Configuration saved", "notes": notes}

    @staticmethod
    def _disk_has_snapshots(dom) -> bool:
        try:
            return dom.snapshotNum(0) > 0
        except libvirt.libvirtError:
            return False

    # -- delete / clone / template --------------------------------------
    def delete_vm(self, uuid: str, delete_disks: bool) -> dict:
        c = self.conn()
        dom = self._dom(uuid)
        name = dom.name()
        if dom.isActive():
            raise Conflict("Power off the VM before deleting it")
        cfg = domxml.parse_domain(self._xml(dom, inactive=True))
        paths = [d["path"] for d in cfg["disks"] if d["path"] and not d["readonly"]]
        flags = (libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA |
                 libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        for extra in ("VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA", "VIR_DOMAIN_UNDEFINE_TPM"):
            flags |= getattr(libvirt, extra, 0)
        dom.undefineFlags(flags)
        deleted, kept = [], []
        if delete_disks:
            in_use = self._paths_in_use()
            for p in paths:
                if p in in_use:
                    kept.append(p)
                    continue
                try:
                    c.storageVolLookupByPath(p).delete(0)
                    deleted.append(p)
                except libvirt.libvirtError:
                    kept.append(p)
        msg = f"Deleted {name}" + (f"; {len(deleted)} disk(s) removed" if deleted else "")
        if kept:
            msg += f"; {len(kept)} disk(s) kept (shared or not in a datastore)"
        return {"message": msg, "deleted_disks": deleted, "kept_disks": kept}

    def _paths_in_use(self, exclude: Optional[str] = None) -> Dict[str, List[str]]:
        used: Dict[str, List[str]] = {}
        for d in self.conn().listAllDomains(0):
            if exclude and d.UUIDString() == exclude:
                continue
            try:
                cfg = domxml.parse_domain(self._xml(d, inactive=d.isPersistent()))
            except libvirt.libvirtError:
                continue
            for x in cfg["disks"] + cfg["cdroms"]:
                if x["path"]:
                    used.setdefault(x["path"], []).append(d.name())
        from ..ct import lxc  # the LXC container directory may live inside a datastore
        used.setdefault(lxc.lxcpath().rstrip("/"), []).append("LXC containers")
        return used

    def clone_vm(self, uuid: str, new_name: str, as_template: bool, power_on: bool, username: str,
                 progress: Callable[[int, str], None]) -> dict:
        c = self.conn()
        src = self._dom(uuid)
        if src.isActive():
            raise Conflict("Power off the source VM before cloning")
        try:
            c.lookupByName(new_name)
            raise Conflict(f"A virtual machine named '{new_name}' already exists")
        except libvirt.libvirtError:
            pass
        root = ET.fromstring(self._xml(src, inactive=True))
        root.find("name").text = new_name
        u = root.find("uuid")
        if u is not None:
            root.remove(u)
        for iface in root.findall("devices/interface"):
            m = iface.find("mac")
            if m is not None:
                iface.remove(m)
            t = iface.find("target")
            if t is not None:
                iface.remove(t)
        os_el = root.find("os")
        if os_el is not None:
            nv = os_el.find("nvram")
            if nv is not None:
                os_el.remove(nv)
        g = root.find("devices/graphics")
        if g is not None:
            g.set("passwd", domxml.vnc_password())
            g.set("port", "-1")
            g.set("autoport", "yes")
        domxml.set_metadata(root, "template", "true" if as_template else "false")
        md = root.find("metadata")
        if md is not None:  # a clone does not inherit autostart (as on ESXi)
            for el in md.findall(f"{{{domxml.AS_NS}}}autostart"):
                md.remove(el)
        domxml.set_metadata(root, "created-by", username)
        disks = [d for d in root.findall("devices/disk") if d.get("device") == "disk"]
        created = []
        try:
            for i, d in enumerate(disks):
                srcel = d.find("source")
                path = srcel.get("file") or srcel.get("dev")
                try:
                    svol = c.storageVolLookupByPath(path)
                except libvirt.libvirtError:
                    raise HypervisorError(f"Disk {path} is not in a datastore and cannot be cloned")
                pool = svol.storagePoolLookupByVolume()
                block = self._pool_is_block(pool)
                vinfo = domxml.parse_volume(svol.XMLDesc(0))
                fmt = vinfo["format"] or "raw"
                ext = "" if block else (".qcow2" if fmt == "qcow2" else ".img")
                vname = f"{new_name}_{i}{ext}"
                _, cap, _ = svol.info()
                volxml = domxml.build_volume(vname, cap, "" if block else fmt, False)
                holder: dict = {}

                def copy():
                    try:
                        holder["vol"] = pool.createXMLFrom(volxml, svol, 0)
                    except Exception as e:  # noqa
                        holder["err"] = e
                th = threading.Thread(target=copy, daemon=True)
                th.start()
                base = 5 + 85 * i // len(disks)
                span = 85 // max(1, len(disks))
                while th.is_alive():
                    th.join(2)
                    try:
                        _, _, alloc = pool.storageVolLookupByName(vname).info()
                        _, _, salloc = svol.info()
                        frac = min(1.0, alloc / salloc) if salloc else 0
                        progress(base + int(span * frac), f"Copying disk {i + 1} of {len(disks)}")
                    except libvirt.libvirtError:
                        pass
                if "err" in holder:
                    raise holder["err"]
                created.append(holder["vol"])
                if block:
                    srcel.attrib.pop("file", None)
                    srcel.set("dev", holder["vol"].path())
                else:
                    srcel.attrib.pop("dev", None)
                    srcel.set("file", holder["vol"].path())
            progress(92, "Registering clone")
            dom = c.defineXML(domxml.tostring(root))
        except Exception:
            for v in created:
                try:
                    v.delete(0)
                except libvirt.libvirtError:
                    pass
            raise
        if power_on and not as_template:
            dom.create()
        return {"uuid": dom.UUIDString(), "name": new_name,
                "message": f"{'Template' if as_template else 'Clone'} {new_name} created"}

    def set_template(self, uuid: str, is_template: bool) -> None:
        dom = self._dom(uuid)
        if is_template and dom.isActive():
            raise Conflict("Power off the VM before converting it to a template")
        root = ET.fromstring(self._xml(dom, inactive=True))
        domxml.set_metadata(root, "template", "true" if is_template else "false")
        self.conn().defineXML(domxml.tostring(root))
        if is_template:
            self._dom(uuid).setAutostart(0)

    # -- snapshots ------------------------------------------------------
    def list_snapshots(self, uuid: str) -> List[dict]:
        dom = self._dom(uuid)
        out = []
        current = None
        try:
            if dom.hasCurrentSnapshot(0):
                current = dom.snapshotCurrent(0).getName()
        except libvirt.libvirtError:
            pass
        for s in dom.listAllSnapshots(0):
            info = domxml.parse_snapshot(s.getXMLDesc(0))
            info["current"] = info["name"] == current
            out.append(info)
        out.sort(key=lambda s: s["created"])
        return out

    def create_snapshot(self, uuid: str, name: str, description: str) -> dict:
        dom = self._dom(uuid)
        cfg = domxml.parse_domain(self._xml(dom, inactive=True))
        bad = [d["target"] for d in cfg["disks"] if d["format"] != "qcow2"]
        if bad:
            raise Conflict("Snapshots require all disks to be qcow2 (not supported for: %s)" % ", ".join(bad))
        try:
            dom.snapshotLookupByName(name, 0)
            raise Conflict(f"A snapshot named '{name}' already exists")
        except libvirt.libvirtError:
            pass
        dom.snapshotCreateXML(domxml.build_snapshot(name, description), 0)
        return {"message": f"Snapshot {name} created"}

    def revert_snapshot(self, uuid: str, name: str) -> dict:
        dom = self._dom(uuid)
        try:
            snap = dom.snapshotLookupByName(name, 0)
        except libvirt.libvirtError:
            raise NotFound(f"Snapshot {name} not found")
        dom.revertToSnapshot(snap, 0)
        return {"message": f"Reverted to snapshot {name}"}

    def delete_snapshot(self, uuid: str, name: str, children: bool) -> dict:
        dom = self._dom(uuid)
        try:
            snap = dom.snapshotLookupByName(name, 0)
        except libvirt.libvirtError:
            raise NotFound(f"Snapshot {name} not found")
        snap.delete(libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_CHILDREN if children else 0)
        return {"message": f"Snapshot {name} deleted"}

    # -- console --------------------------------------------------------
    def console_endpoint(self, uuid: str) -> dict:
        dom = self._dom(uuid)
        if not dom.isActive():
            raise Conflict("The VM must be powered on to open its console")
        g = domxml.parse_domain(self._xml(dom)).get("graphics")
        if not g or g["type"] != "vnc" or g["port"] <= 0:
            raise HypervisorError("This VM has no VNC console")
        if g["listen"] not in ("127.0.0.1", "localhost", "::1"):
            raise HypervisorError("Refusing console: VNC is not bound to localhost")
        return {"host": "127.0.0.1", "port": g["port"], "password": g.get("passwd") or "", "name": dom.name()}

    def screenshot(self, uuid: str) -> Optional[bytes]:
        dom = self._dom(uuid)
        if not dom.isActive():
            return None
        st = self.conn().newStream(0)
        mime = dom.screenshot(st, 0, 0)
        buf = bytearray()
        while True:
            chunk = st.recv(262144)
            if chunk in (-2, None) or not chunk:
                break
            buf += chunk
            if len(buf) > 32 * 1024 * 1024:
                break
        st.finish()
        return bytes(buf) if mime else None

    # ------------------------------------------------------------------ storage
    def list_pools(self) -> List[dict]:
        out = []
        for p in self.conn().listAllStoragePools(0):
            out.append(self._pool_summary(p))
        out.sort(key=lambda p: (p["name"] != DEFAULT_POOL, p["name"]))
        return out

    def _pool_summary(self, p) -> dict:
        st, cap, alloc, avail = p.info()
        info = domxml.parse_pool(p.XMLDesc(0))
        return {"name": p.name(), "uuid": p.UUIDString(), "state": POOL_STATES.get(st, str(st)),
                "active": bool(p.isActive()), "autostart": bool(p.autostart()), "capacity": cap, "allocation": alloc,
                "available": avail, "volumes": p.numOfVolumes() if p.isActive() else 0,
                "is_iso_library": p.name() == ISO_POOL, **info}

    def create_pool(self, spec: dict) -> dict:
        c = self.conn()
        try:
            c.storagePoolLookupByName(spec["name"])
            raise Conflict(f"A datastore named '{spec['name']}' already exists")
        except libvirt.libvirtError:
            pass
        pool = c.storagePoolDefineXML(domxml.build_pool(spec), 0)
        try:
            if spec["type"] in ("dir", "netfs"):
                pool.build(0)
            pool.create(0)
            pool.setAutostart(1)
        except libvirt.libvirtError:
            try:
                if pool.isActive():
                    pool.destroy()
                pool.undefine()
            except libvirt.libvirtError:
                pass
            raise
        return {"message": f"Datastore {spec['name']} created", **self._pool_summary(pool)}

    def pool_action(self, name: str, action: str) -> dict:
        p = self._pool(name)
        if action == "start":
            p.create(0)
        elif action == "stop":
            if name in (ISO_POOL,):
                raise Conflict("The ISO library cannot be deactivated")
            in_use = self._pool_in_use(p)
            if in_use:
                raise Conflict(f"Datastore is used by: {', '.join(sorted(in_use))}")
            p.destroy()
        elif action == "refresh":
            p.refresh(0)
        elif action == "autostart_on":
            p.setAutostart(1)
        elif action == "autostart_off":
            p.setAutostart(0)
        return self._pool_summary(p)

    def _pool_in_use(self, p) -> set:
        if not p.isActive():
            return set()
        paths = {v.path() for v in p.listAllVolumes(0)}
        users = set()
        for path, vms in self._paths_in_use().items():
            if path in paths:
                users.update(vms)
        return users

    def remove_pool(self, name: str) -> dict:
        if name in (ISO_POOL, DEFAULT_POOL):
            raise Conflict("Built-in datastores cannot be removed")
        p = self._pool(name)
        users = self._pool_in_use(p)
        if users:
            raise Conflict(f"Datastore is used by: {', '.join(sorted(users))}")
        if p.isActive():
            p.destroy()
        p.undefine()
        return {"message": f"Datastore {name} removed (data on the underlying storage was not erased)"}

    def list_volumes(self, pool_name: str) -> List[dict]:
        p = self._pool(pool_name)
        if not p.isActive():
            return []
        p.refresh(0)
        used = self._paths_in_use()
        out = []
        for v in p.listAllVolumes(0):
            t, cap, alloc = v.info()
            info = domxml.parse_volume(v.XMLDesc(0))
            out.append({"name": v.name(), "path": v.path(), "capacity": cap, "allocation": alloc,
                        "format": info["format"], "backing": info["backing"], "used_by": used.get(v.path(), [])})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def create_volume(self, pool_name: str, name: str, size_gib: int, fmt: str, thick: bool) -> dict:
        p = self._pool(pool_name)
        block = self._pool_is_block(p)
        if block:
            fmt = ""
        vol = p.createXML(domxml.build_volume(name, _gib(size_gib), fmt, thick), 0)
        return {"message": f"Volume {name} created", "path": vol.path()}

    def delete_volume(self, pool_name: str, name: str) -> dict:
        p = self._pool(pool_name)
        try:
            v = p.storageVolLookupByName(name)
        except libvirt.libvirtError:
            raise NotFound(f"Volume {name} not found")
        users = self._paths_in_use().get(v.path())
        if users:
            raise Conflict(f"Volume is in use by: {', '.join(users)}")
        v.delete(0)
        return {"message": f"Volume {name} deleted"}

    def resize_volume(self, pool_name: str, name: str, size_gib: int) -> dict:
        p = self._pool(pool_name)
        v = p.storageVolLookupByName(name)
        _, cap, _ = v.info()
        if _gib(size_gib) <= cap:
            raise Conflict("Volumes can only be grown")
        if self._paths_in_use().get(v.path()):
            raise Conflict("Power off / detach VMs using this volume before resizing it")
        v.resize(_gib(size_gib), 0)
        return {"message": f"Volume {name} resized to {size_gib} GiB"}

    # -- ISO library ----------------------------------------------------
    def ensure_iso_pool(self, path: str) -> None:
        c = self.conn()
        try:
            p = c.storagePoolLookupByName(ISO_POOL)
        except libvirt.libvirtError:
            p = c.storagePoolDefineXML(domxml.build_pool({"type": "dir", "name": ISO_POOL, "path": path}), 0)
            p.build(0)
        if not p.isActive():
            p.create(0)
        p.setAutostart(1)

    def ensure_default_pool(self, path: str = "/var/lib/libvirt/images", name: str = DEFAULT_POOL) -> None:
        c = self.conn()
        try:
            p = c.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            p = c.storagePoolDefineXML(domxml.build_pool({"type": "dir", "name": name, "path": path}), 0)
            p.build(0)
        if not p.isActive():
            p.create(0)
        p.setAutostart(1)

    def list_isos(self) -> List[dict]:
        try:
            vols = self.list_volumes(ISO_POOL)
        except NotFound:
            return []
        return [v for v in vols if v["name"].lower().endswith((".iso", ".img"))]

    def iso_path(self, name: str) -> str:
        try:
            return self._pool(ISO_POOL).storageVolLookupByName(name).path()
        except libvirt.libvirtError:
            raise NotFound(f"ISO image {name} not found in the ISO library")

    def begin_iso_upload(self, name: str, size: int):
        p = self._pool(ISO_POOL)
        p.refresh(0)
        try:
            p.storageVolLookupByName(name)
            raise Conflict(f"An image named {name} already exists")
        except libvirt.libvirtError:
            pass
        _, _, _, avail = p.info()
        if size > avail:
            raise HypervisorError("Not enough free space in the ISO library")
        vol = p.createXML(domxml.build_volume(name, size, "raw", False), 0)
        st = self.conn().newStream(0)
        vol.upload(st, 0, size, 0)
        return vol, st

    def pool_dir(self, name: str) -> str:
        """Target directory of a directory/NFS datastore (import and export work with files)."""
        p = self._pool(name)
        info = domxml.parse_pool(p.XMLDesc(0))
        if info["type"] not in ("dir", "netfs") or not p.isActive():
            raise Conflict(f"Datastore {name} must be an active directory or NFS datastore")
        return info["path"]

    def refresh_pool(self, name: str) -> None:
        try:
            self._pool(name).refresh(0)
        except libvirt.libvirtError:
            pass

    def volume_path(self, pool: str, vol: str) -> str:
        p = self._pool(pool)
        p.refresh(0)
        try:
            return p.storageVolLookupByName(vol).path()
        except libvirt.libvirtError:
            raise NotFound(f"File {vol} not found in datastore {pool}")

    def begin_volume_upload(self, pool_name: str, name: str, size: int):
        p = self._pool(pool_name)
        if self._pool_is_block(p):
            raise Conflict("Files can be uploaded to directory and NFS datastores only")
        p.refresh(0)
        try:
            p.storageVolLookupByName(name)
            raise Conflict(f"A file named {name} already exists in {pool_name}")
        except libvirt.libvirtError:
            pass
        if size > p.info()[3]:
            raise HypervisorError(f"Not enough free space in datastore {pool_name}")
        vol = p.createXML(domxml.build_volume(name, size, "raw", False), 0)
        st = self.conn().newStream(0)
        vol.upload(st, 0, size, 0)
        return vol, st

    def begin_volume_download(self, pool_name: str, name: str):
        p = self._pool(pool_name)
        p.refresh(0)
        try:
            vol = p.storageVolLookupByName(name)
        except libvirt.libvirtError:
            raise NotFound(f"File {name} not found in datastore {pool_name}")
        st = self.conn().newStream(0)
        vol.download(st, 0, 0, 0)
        return vol, st

    def delete_iso(self, name: str) -> dict:
        return self.delete_volume(ISO_POOL, name)

    # ------------------------------------------------------------------ networks
    def list_networks(self) -> List[dict]:
        out = []
        used: Dict[str, List[str]] = {}
        for d in self.conn().listAllDomains(0):
            try:
                for n in domxml.parse_domain(self._xml(d, inactive=d.isPersistent()))["nics"]:
                    if n["type"] == "network":
                        used.setdefault(n["source"], []).append(d.name())
            except libvirt.libvirtError:
                continue
        for n in self.conn().listAllNetworks(0):
            info = domxml.parse_network(n.XMLDesc(0))
            try:
                bridge = n.bridgeName()
            except libvirt.libvirtError:
                bridge = info.get("bridge")
            out.append({"name": n.name(), "uuid": n.UUIDString(), "active": bool(n.isActive()),
                        "autostart": bool(n.autostart()), "persistent": bool(n.isPersistent()), **info,
                        "bridge": bridge, "used_by": used.get(n.name(), [])})
        out.sort(key=lambda x: x["name"])
        return out

    def create_network(self, spec: dict) -> dict:
        c = self.conn()
        try:
            c.networkLookupByName(spec["name"])
            raise Conflict(f"A virtual network named '{spec['name']}' already exists")
        except libvirt.libvirtError:
            pass
        net = c.networkDefineXML(domxml.build_network(spec))
        try:
            net.create()
            net.setAutostart(1)
        except libvirt.libvirtError:
            net.undefine()
            raise
        return {"message": f"Virtual network {spec['name']} created"}

    def network_action(self, name: str, action: str) -> dict:
        try:
            n = self.conn().networkLookupByName(name)
        except libvirt.libvirtError:
            raise NotFound(f"Virtual network {name} not found")
        if action == "start":
            n.create()
        elif action == "stop":
            n.destroy()
        elif action == "autostart_on":
            n.setAutostart(1)
        elif action == "autostart_off":
            n.setAutostart(0)
        elif action == "delete":
            users = [x for x in self.list_networks() if x["name"] == name][0]["used_by"]
            if users:
                raise Conflict(f"Network is used by: {', '.join(users)}")
            if n.isActive():
                n.destroy()
            n.undefine()
            return {"message": f"Virtual network {name} deleted"}
        return {"message": f"Virtual network {name}: {action} done"}

    def ensure_default_network(self, bridge: str) -> str:
        """Network "default" = the host bridge shared with the physical LAN (forward mode="bridge").

        A NAT "default" from an older LSX-VE is replaced when no running VM uses it. Returns what was done."""
        c = self.conn()
        want = domxml.build_network({"name": "default", "mode": "bridge", "bridge": bridge})
        try:
            n = c.networkLookupByName("default")
        except libvirt.libvirtError:
            n = None
        if n is not None:
            info = domxml.parse_network(n.XMLDesc(0))
            if info.get("mode") == "bridge" and info.get("bridge") == bridge:
                if not n.isActive():
                    n.create()
                n.setAutostart(1)
                return "ok"
            running = []
            for d in c.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
                try:
                    nics = domxml.parse_domain(self._xml(d))["nics"]
                except libvirt.libvirtError:
                    continue
                if any(x["type"] == "network" and x["source"] == "default" for x in nics):
                    running.append(d.name())
            if running:
                return f"waiting: running VMs still use the old network: {', '.join(running)}"
            if n.isActive():
                n.destroy()
            n.undefine()
        existed = n is not None
        n = c.networkDefineXML(want)
        n.create()
        n.setAutostart(1)
        return "replaced" if existed else "created"

    def counts(self) -> dict:
        running = stopped = paused = templates = 0
        for d in self.conn().listAllDomains(0):
            try:
                st = self._state(d)
                if st == "running":
                    running += 1
                elif st == "paused":
                    paused += 1
                else:
                    if domxml.parse_domain(self._xml(d, True)).get("template"):
                        templates += 1
                    else:
                        stopped += 1
            except libvirt.libvirtError:
                continue
        return {"running": running, "stopped": stopped, "paused": paused, "templates": templates}
