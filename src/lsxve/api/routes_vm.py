"""Virtual machine, snapshot, template and console endpoints."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time

from ..common import config, security, validate as v
from ..host import hardware
from ..hv import domxml, oscatalog, ovf
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, WebSocket, json_response

BOOT_DEVICES = ("cdrom", "disk", "network")


def _nic(x, field):
    if not isinstance(x, dict):
        raise v.ValidationError(field, "must be an object")
    kind = v.choice(x.get("type", "network"), f"{field}.type", ("network", "bridge"))
    src = v.name(x.get("source"), f"{field}.source") if kind == "network" else v.ifname(x.get("source"), f"{field}.source")
    out = {"type": kind, "source": src, "model": v.choice(x.get("model", "virtio"), f"{field}.model", domxml.NIC_MODELS)}
    if x.get("mac"):
        out["mac"] = v.mac(x["mac"], f"{field}.mac")
    return out


def _disk(x, field):
    if not isinstance(x, dict):
        raise v.ValidationError(field, "must be an object")
    out = {"datastore": v.name(x.get("datastore", "default"), f"{field}.datastore"),
           "bus": v.choice(x.get("bus", "virtio"), f"{field}.bus", domxml.DISK_BUSES)}
    if x.get("existing"):
        out["existing"] = v.string(x["existing"], f"{field}.existing", 1, 200, v.RE_FILENAME)
        return out
    out["size_gib"] = v.integer(x.get("size_gib"), f"{field}.size_gib", 1, 65536)
    out["provisioning"] = v.choice(x.get("provisioning", "thin"), f"{field}.provisioning", ("thin", "thick"))
    out["format"] = v.choice(x.get("format", "qcow2"), f"{field}.format", ("qcow2", "raw"))
    return out


def _boot_order(x, field="boot_order"):
    lst = v.list_of(x, field, lambda e, f: v.choice(e, f, BOOT_DEVICES), 3)
    if len(set(lst)) != len(lst) or not lst:
        raise v.ValidationError(field, "must list each boot device once")
    return lst


RE_PCI = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
RE_USBID = re.compile(r"^[0-9a-f]{4}$")
IMPORT_EXT = (".ova", ".ovf", ".vmdk", ".qcow2", ".img", ".raw", ".vhd", ".vhdx", ".vdi")


def _hostdev(x, field):
    if not isinstance(x, dict):
        raise v.ValidationError(field, "must be an object")
    t = v.choice(x.get("type"), f"{field}.type", ("pci", "usb"))
    if t == "pci":
        return {"type": "pci", "address": v.string(str(x.get("address", "")).lower(), f"{field}.address", 12, 12, RE_PCI)}
    return {"type": "usb", "vendor": v.string(str(x.get("vendor", "")).lower(), f"{field}.vendor", 4, 4, RE_USBID),
            "product": v.string(str(x.get("product", "")).lower(), f"{field}.product", 4, 4, RE_USBID)}


def _check_hostdevs(devs):
    for h in devs:
        if h["type"] == "pci":
            why = hardware.pci_passthrough_blocker(h["address"])
            if why:
                raise v.ValidationError("add_hostdevs", f"PCI device {h['address']} cannot be passed through: {why}")


def cpu_quota_from_mhz(limit_mhz: int) -> int:
    """CPU limit in MHz (whole VM) -> CFS quota in microseconds per 100 ms period (0 = unlimited)."""
    if not limit_mhz:
        return 0
    mhz = hardware.cpu_info()["mhz"] or 2000
    return max(1000, int(limit_mhz / mhz * domxml.CPU_PERIOD_US))


def validate_spec(body: dict) -> dict:
    cpu = hardware.cpu_info()
    mem = hardware.memory_info()
    max_mem = max(1024, mem["total"] // (1024 * 1024)) if mem["total"] else 1024 * 1024
    spec = {
        "name": v.name(v.require(body, "name")),
        "description": v.text(body.get("description", ""), "description", 1000),
        "os_type": v.choice(body.get("os_type", "linux"), "os_type", domxml.OS_TYPES),
        "os_variant": v.string(body.get("os_variant", ""), "os_variant", 0, 64),
        "vcpus": v.integer(v.require(body, "vcpus"), "vcpus", 1, max(1, cpu["threads"])),
        "memory_mib": v.integer(v.require(body, "memory_mib"), "memory_mib", 128, max_mem),
        "firmware": v.choice(body.get("firmware", "bios"), "firmware", ("bios", "uefi")),
        "secure_boot": v.boolean(body.get("secure_boot", False), "secure_boot"),
        "tpm": v.boolean(body.get("tpm", False), "tpm"),
        "disks": v.list_of(body.get("disks", []), "disks", _disk, 8),
        "nics": v.list_of(body.get("nics", []), "nics", _nic, 8),
        "boot_order": _boot_order(body.get("boot_order", ["cdrom", "disk", "network"])),
        "autostart": v.boolean(body.get("autostart", False), "autostart"),
        "power_on": v.boolean(body.get("power_on", False), "power_on"),
        "cpu_hotadd": v.boolean(body.get("cpu_hotadd", False), "cpu_hotadd"),
        "mem_hotadd": v.boolean(body.get("mem_hotadd", False), "mem_hotadd"),
        "hostdevs": v.list_of(body.get("hostdevs", []), "hostdevs", _hostdev, 8),
    }
    if body.get("iso"):
        spec["iso"] = v.string(body["iso"], "iso", 1, 200, v.RE_FILENAME)
    if spec["secure_boot"] and spec["firmware"] != "uefi":
        raise v.ValidationError("secure_boot", "Secure Boot requires UEFI firmware")
    return spec


def register(ctx: Ctx) -> None:
    r = ctx.router
    hv = ctx.hv

    @handler
    async def hypervisor_info(req: Request) -> Response:
        st = await ctx.run(hv.status)
        cpu = hardware.cpu_info()
        mem = hardware.memory_info()
        return json_response({
            **st, "kvm": hardware.kvm_status(), "cpu_virtualization": cpu["virtualization"],
            "host_threads": cpu["threads"], "host_memory_mib": mem["total"] // (1024 * 1024),
            "os_types": {k: val["label"] for k, val in domxml.OS_TYPES.items()},
            "os_defaults": {k: {"disk_bus": val["disk_bus"], "nic_model": val["nic"]} for k, val in domxml.OS_TYPES.items()},
            "os_catalog": oscatalog.CATALOG,
            "disk_buses": domxml.DISK_BUSES, "nic_models": domxml.NIC_MODELS, "boot_devices": BOOT_DEVICES,
            "video_models": domxml.VIDEO_MODELS, "stop_actions": domxml.STOP_ACTIONS,
            "guest_tools": os.path.isfile(str(config.GUEST_TOOLS_ISO)), "host_mhz": cpu["mhz"],
            "maintenance": bool(ctx.maintenance().get("enabled")),
        })

    @handler
    async def list_vms(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_vms))

    @handler
    async def get_vm(req: Request) -> Response:
        d = await ctx.run(hv.vm_detail, v.uuid(req.params["uuid"]))
        cpu = hardware.cpu_info()
        d.setdefault("host", {}).update(cpu_model=cpu["model"], cpu_mhz=cpu["mhz"], threads=cpu["threads"])
        return json_response(d)

    @handler
    async def create_vm(req: Request) -> Response:
        spec = validate_spec(req.json())
        if spec["power_on"]:
            ctx.require_not_maintenance()
        await ctx.run(_check_hostdevs, spec["hostdevs"])
        user = req.username
        ctx.audit(req, "vm.create", spec["name"], "submitted", vcpus=spec["vcpus"], memory_mib=spec["memory_mib"])
        t = ctx.tasks.submit("vm.create", f"Create virtual machine {spec['name']}",
                             lambda tc: hv.create_vm(spec, user, tc.progress), user, f"vm:{spec['name']}",
                             spec["name"], "vm")
        return json_response(t, 202)

    @handler
    async def update_vm(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        b = req.json()
        ch: dict = {}
        if "name" in b:
            ch["name"] = v.name(b["name"])
        if "vcpus" in b:
            ch["vcpus"] = v.integer(b["vcpus"], "vcpus", 1, max(1, hardware.cpu_info()["threads"]))
        if "memory_mib" in b:
            ch["memory_mib"] = v.integer(b["memory_mib"], "memory_mib", 128,
                                         max(1024, hardware.memory_info()["total"] // 1048576))
        if "description" in b:
            ch["description"] = v.text(b["description"], "description", 1000)
        if "os_type" in b:
            ch["os_type"] = v.choice(b["os_type"], "os_type", domxml.OS_TYPES)
        if "os_variant" in b:
            ch["os_variant"] = v.string(b["os_variant"], "os_variant", 0, 64)
        if "boot_order" in b:
            ch["boot_order"] = _boot_order(b["boot_order"])
        if "autostart" in b:
            ch["autostart"] = v.boolean(b["autostart"], "autostart")
        if "cdrom" in b:
            ch["cdrom"] = v.string(b["cdrom"], "cdrom", 1, 200, v.RE_FILENAME) if b["cdrom"] else None
        if "add_disks" in b:
            ch["add_disks"] = v.list_of(b["add_disks"], "add_disks", _disk, 8)
        if "remove_disks" in b:
            ch["remove_disks"] = v.list_of(b["remove_disks"], "remove_disks",
                                           lambda x, f: v.string(x, f, 3, 5, v.re.compile(r"^(vd|sd)[a-z]{1,2}$")), 8)
            ch["delete_removed_disks"] = v.boolean(b.get("delete_removed_disks", False), "delete_removed_disks")
        if "add_nics" in b:
            ch["add_nics"] = v.list_of(b["add_nics"], "add_nics", _nic, 8)
        if "remove_nics" in b:
            ch["remove_nics"] = v.list_of(b["remove_nics"], "remove_nics", v.mac, 8)
        if "cpu_hotadd" in b:
            ch["cpu_hotadd"] = v.boolean(b["cpu_hotadd"], "cpu_hotadd")
        if "mem_hotadd" in b:
            ch["mem_hotadd"] = v.boolean(b["mem_hotadd"], "mem_hotadd")
        if "add_serials" in b:
            def ser(x, f):
                if not isinstance(x, dict):
                    raise v.ValidationError(f, "must be an object")
                fn = v.string(x.get("file"), f + ".file", 1, 120, v.RE_FILENAME)
                if not re.search(r"\.(log|txt|out)$", fn, re.I):
                    raise v.ValidationError(f + ".file", "serial output file must end in .log, .txt or .out")
                return {"datastore": v.name(x.get("datastore"), f + ".datastore"), "file": fn}
            ch["add_serials"] = v.list_of(b["add_serials"], "add_serials", ser, 3)
        if "remove_serials" in b:
            ch["remove_serials"] = v.list_of(b["remove_serials"], "remove_serials",
                                             lambda x, f: v.integer(x, f, 1, domxml.SERIAL_MAX - 1), 3)
        if "cpu_shares" in b:
            ch["cpu_shares"] = v.integer(b["cpu_shares"], "cpu_shares", 2, 262144)
        if "cpu_limit_mhz" in b:
            ch["cpu_quota_us"] = cpu_quota_from_mhz(v.integer(b["cpu_limit_mhz"] or 0, "cpu_limit_mhz", 0, 10 ** 7))
        if "mem_lock" in b:
            ch["mem_lock"] = v.boolean(b["mem_lock"], "mem_lock")
        if "video_model" in b:
            ch["video_model"] = v.choice(b["video_model"], "video_model", domxml.VIDEO_MODELS)
        if "video_vram_mib" in b:
            ch["video_vram_mib"] = v.integer(b["video_vram_mib"], "video_vram_mib", 4, 256) if b["video_vram_mib"] else None
        if "disk_limits" in b:
            def lim(x, f):
                if not isinstance(x, dict):
                    raise v.ValidationError(f, "must be an object")
                return {"target": v.string(x.get("target"), f + ".target", 3, 5, re.compile(r"^(vd|sd)[a-z]{1,2}$")),
                        "iops": v.integer(x.get("iops", 0) or 0, f + ".iops", 0, 10 ** 7),
                        "bps": v.integer(x.get("mbps", 0) or 0, f + ".mbps", 0, 10 ** 6) * 1024 * 1024}
            ch["disk_limits"] = v.list_of(b["disk_limits"], "disk_limits", lim, 16)
        if "resize_disks" in b:
            def rz(x, f):
                if not isinstance(x, dict):
                    raise v.ValidationError(f, "must be an object")
                return {"target": v.string(x.get("target"), f + ".target", 3, 5, re.compile(r"^(vd|sd)[a-z]{1,2}$")),
                        "size_gib": v.integer(x.get("size_gib"), f + ".size_gib", 1, 65536)}
            ch["resize_disks"] = v.list_of(b["resize_disks"], "resize_disks", rz, 16)
        if "add_hostdevs" in b:
            ch["add_hostdevs"] = v.list_of(b["add_hostdevs"], "add_hostdevs", _hostdev, 8)
            await ctx.run(_check_hostdevs, ch["add_hostdevs"])
        if "remove_hostdevs" in b:
            ch["remove_hostdevs"] = v.list_of(b["remove_hostdevs"], "remove_hostdevs",
                                              lambda x, f: v.string(x, f, 8, 20, re.compile(r"^(pci|usb):[0-9a-f:.]+$")), 8)
        if "update_nics" in b:
            def upd(x, f):
                n = _nic(x, f)
                n["mac"] = v.mac(x.get("mac"), f + ".mac")
                if "connected" in x:
                    n["connected"] = v.boolean(x["connected"], f + ".connected")
                return n
            ch["update_nics"] = v.list_of(b["update_nics"], "update_nics", upd, 8)
        if not ch:
            raise v.ValidationError("body", "no changes supplied")
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.update", detail["name"], "submitted", changes=sorted(ch))
        t = ctx.tasks.submit("vm.reconfigure", f"Reconfigure virtual machine {detail['name']}",
                             lambda tc: hv.update_vm(uuid, ch), req.username, f"vm:{uuid}", detail["name"], "vm")
        return json_response(t, 202)

    @handler
    async def delete_vm(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        delete_disks = req.q("delete_disks") == "1"
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.delete", detail["name"], "submitted", delete_disks=delete_disks)
        t = ctx.tasks.submit("vm.delete", f"Delete virtual machine {detail['name']}",
                             lambda tc: hv.delete_vm(uuid, delete_disks), req.username, f"vm:{uuid}", detail["name"], "vm")
        return json_response(t, 202)

    POWER_LABELS = {"start": "Power on", "shutdown": "Shut down guest", "poweroff": "Power off", "reboot": "Restart guest",
                    "reset": "Reset", "suspend": "Pause", "resume": "Resume"}

    @handler
    async def power(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        action = v.choice(req.params["action"], "action", POWER_LABELS)
        if action in ("start", "resume"):
            ctx.require_not_maintenance()
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, f"vm.power.{action}", detail["name"])

        def run(tc):
            state = hv.power(uuid, action)
            if action == "shutdown":
                tc.progress(30, "Waiting for guest OS to shut down")
                if not hv.wait_state(uuid, ("shutoff",), 180):
                    return {"message": "Shutdown requested; the guest has not powered off yet (is ACPI / guest agent "
                                       "supported?)", "state": "running"}
                state = "shutoff"
            return {"message": f"{POWER_LABELS[action]}: VM is now {state}", "state": state}
        t = ctx.tasks.submit(f"vm.power.{action}", f"{POWER_LABELS[action]} {detail['name']}", run, req.username,
                             f"vm:{uuid}", detail["name"], "vm")
        return json_response(t, 202)

    @handler
    async def autostart(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        b = req.json()
        opts = {}
        enabled = v.boolean(b["enabled"], "enabled") if "enabled" in b else None
        if "order" in b:
            opts["order"] = v.integer(b["order"] or 0, "order", 0, 999)
        for k in ("start_delay", "stop_delay"):
            if k in b:
                opts[k] = -1 if b[k] in (None, "", -1) else v.integer(b[k], k, 0, 3600)
        if "stop_action" in b:
            opts["stop_action"] = "" if not b["stop_action"] else v.choice(b["stop_action"], "stop_action",
                                                                           domxml.STOP_ACTIONS)
        if "wait_heartbeat" in b:
            opts["wait_heartbeat"] = v.boolean(b["wait_heartbeat"], "wait_heartbeat")
        cfg = await ctx.run(hv.set_autostart, uuid, enabled, **opts)
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.autostart", detail["name"], **cfg)
        ctx.bus.event("info", "vm", f"Autostart settings of {detail['name']} changed (autostart "
                                    f"{'enabled' if cfg['enabled'] else 'disabled'})", uuid, detail["name"], req.username)
        ctx.bus.broadcast("vm", {"uuid": uuid, "name": detail["name"], "event": "autostart", "state": detail["state"]})
        return json_response({"autostart": cfg["enabled"], **cfg})

    # -- guest tools, host devices, performance history ------------------------------------------------
    @handler
    async def guest_tools(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        mount = req.method == "POST"
        path = str(config.GUEST_TOOLS_ISO)
        if mount and not os.path.isfile(path):
            raise HTTPError(503, "LSX-VE Guest Tools are not installed on this host "
                                 f"({path} is missing)", "guest_tools_missing")
        detail = await ctx.run(hv.vm_brief, uuid)
        res = await ctx.run(hv.mount_media_path, uuid, path if mount else None)
        ctx.audit(req, "vm.guest_tools." + ("mount" if mount else "unmount"), detail["name"])
        ctx.bus.event("info", "vm", f"LSX-VE Guest Tools {'mounted in' if mount else 'unmounted from'} {detail['name']}",
                      uuid, detail["name"], req.username)
        ctx.bus.broadcast("vm", {"uuid": uuid, "name": detail["name"], "event": "media", "state": detail["state"]})
        return json_response(res)

    @handler
    async def host_devices(req: Request) -> Response:
        def collect():
            out = hv.list_host_devices()
            for d in out["pci"]:
                d["blocked"] = hardware.pci_passthrough_blocker(d["address"])
            out["iommu"] = hardware.iommu_enabled()
            return out
        return json_response(await ctx.run(collect))

    @handler
    async def vm_metrics(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        h = ctx.vm_history.get(uuid) or []
        return json_response([{"t": t, **st} for t, st in list(h)])

    # -- import / export (OVF / OVA / VMDK) --------------------------------------------------------------
    def _src_path(ds: str, name: str) -> str:
        return hv.volume_path(ds, name)

    @handler
    async def import_inspect(req: Request) -> Response:
        b = req.json()
        ds = v.name(v.require(b, "datastore"), "datastore")
        name = v.string(v.require(b, "file"), "file", 3, 200, v.RE_FILENAME)
        if not name.lower().endswith(IMPORT_EXT):
            raise v.ValidationError("file", "choose an .ova, .ovf or disk image (" + ", ".join(IMPORT_EXT) + ")")
        path = await ctx.run(_src_path, ds, name)
        out = {"datastore": ds, "file": name, "kind": "ovf" if name.lower().endswith((".ova", ".ovf")) else "disk"}
        if out["kind"] == "ovf":
            res = await ctx.run(ctx.agent.call, "vmio.read_ovf", timeout=120, path=path)
            try:
                info = ovf.parse(res["ovf"])
            except (ValueError, Exception) as e:  # noqa: BLE001 - ElementTree.ParseError and friends
                raise v.ValidationError("file", f"cannot read the OVF descriptor: {e}")
            if name.lower().endswith(".ova"):
                have = {m["name"] for m in res["members"]}
                missing = [d["file"] for d in info["disks"] if d["file"] not in have]
            else:
                vols = {x["name"] for x in await ctx.run(hv.list_volumes, ds)}
                missing = [d["file"] for d in info["disks"] if d["file"] not in vols]
            out.update(info, missing=missing)
        else:
            img = await ctx.run(ctx.agent.call, "vmio.image_info", timeout=60, path=path)
            base = re.sub(r"[^A-Za-z0-9_-]", "-", name.rsplit(".", 1)[0])[:60].strip("-") or "imported-vm"
            out.update(name=base, vcpus=2, memory_mib=2048, firmware="bios", os_family="linux", os_label="",
                       disks=[{"file": name, "capacity": img.get("virtual_size") or 0, "format": img.get("format")}],
                       nics=[{"model": "virtio"}], missing=[])
        return json_response(out)

    @handler
    async def import_vm(req: Request) -> Response:
        b = req.json()
        ds = v.name(v.require(b, "datastore"), "datastore")
        src = v.string(v.require(b, "file"), "file", 3, 200, v.RE_FILENAME)
        target = v.name(b.get("target_datastore") or ds, "target_datastore")
        spec = validate_spec({**b, "disks": [], "nics": [], "boot_order": ["disk", "cdrom", "network"]})
        network = v.name(v.require(b, "network"), "network")
        nic_model = v.choice(b.get("nic_model", "virtio"), "nic_model", domxml.NIC_MODELS)
        bus = v.choice(b.get("disk_bus", "sata"), "disk_bus", domxml.DISK_BUSES)
        nic_count = v.integer(b.get("nic_count", 1), "nic_count", 0, 8)
        if spec["power_on"]:
            ctx.require_not_maintenance()
        src_path = await ctx.run(_src_path, ds, src)
        target_dir = await ctx.run(hv.pool_dir, target)
        user = req.username
        ctx.audit(req, "vm.import", spec["name"], "submitted", source=f"{ds}/{src}", target=target)

        def run(tc):
            tag = secrets.token_hex(4)
            temp: list = []
            created: list = []
            try:
                if src.lower().endswith((".ova", ".ovf")):
                    tc.progress(3, "Reading the OVF descriptor")
                    info = ovf.parse(ctx.agent.call("vmio.read_ovf", timeout=120, path=src_path)["ovf"])
                    if src.lower().endswith(".ova"):
                        tc.progress(5, "Unpacking the OVA")
                        files = ctx.agent.call("vmio.extract_ova", timeout=6 * 3600, path=src_path,
                                               dest_dir=target_dir, tag=tag)["files"]
                        temp += list(files.values())
                        disk_paths = [files[d["file"]] for d in info["disks"]]
                    else:
                        disk_paths = [hv.volume_path(ds, d["file"]) for d in info["disks"]]
                else:
                    disk_paths = [src_path]
                n = len(disk_paths)
                for i, dp in enumerate(disk_paths):
                    tc.progress(20 + 60 * i // max(1, n), f"Converting disk {i + 1} of {n} to qcow2")
                    dst = f"{target_dir.rstrip('/')}/{spec['name']}_{i}.qcow2"
                    ctx.agent.call("vmio.convert", timeout=6 * 3600, src=dp, dst=dst, dst_format="qcow2")
                    created.append(dst)
                hv.refresh_pool(target)
                tc.progress(85, "Registering the virtual machine")
                vm_spec = {**spec, "disks": [{"datastore": target, "bus": bus, "existing": os.path.basename(c)}
                                             for c in created],
                           "nics": [{"type": "network", "source": network, "model": nic_model}] * nic_count}
                res = hv.create_vm(vm_spec, user, lambda p_, m: None)
            except Exception:
                hv.refresh_pool(target)
                for c in created:  # failed import: remove the converted disks again
                    try:
                        hv.delete_volume(target, os.path.basename(c))
                    except Exception:  # noqa: BLE001
                        pass
                raise
            finally:
                if temp:
                    try:
                        ctx.agent.call("vmio.cleanup", timeout=300, paths=temp)
                    except Exception:  # noqa: BLE001
                        pass
            ctx.bus.event("info", "vm", f"Virtual machine {spec['name']} imported from {src}", res["uuid"],
                          spec["name"], user)
            return {**res, "message": f"Imported {src} as {spec['name']} ({len(created)} disk(s))"}
        t = ctx.tasks.submit("vm.import", f"Import {src} as virtual machine {spec['name']}", run, user,
                             f"vm:{spec['name']}", spec["name"], "vm")
        return json_response(t, 202)

    @handler
    async def export_vm(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        b = req.json()
        ds = v.name(v.require(b, "datastore"), "datastore")
        vm = await ctx.run(hv.vm_detail, uuid)
        if vm["state"] != "shutoff":
            raise HTTPError(409, "Power off the virtual machine before exporting it")
        out_dir = await ctx.run(hv.pool_dir, ds)
        ova = f"{vm['name']}.ova"
        vols = {x["name"] for x in await ctx.run(hv.list_volumes, ds)}
        if ova in vols:
            raise HTTPError(409, f"{ova} already exists in {ds}; delete it first")
        ctx.audit(req, "vm.export", vm["name"], "submitted", datastore=ds)

        def run(tc):
            tag = secrets.token_hex(4)
            temp, files = [], []
            try:
                disks = [d for d in vm["hardware"]["disks"] if d.get("path")]
                for i, d in enumerate(disks):
                    tc.progress(5 + 70 * i // max(1, len(disks)), f"Converting disk {i + 1} of {len(disks)} to VMDK")
                    dst = f"{out_dir.rstrip('/')}/export-{tag}-{vm['name']}-disk{i + 1}.vmdk"
                    r = ctx.agent.call("vmio.convert", timeout=6 * 3600, src=d["path"], dst=dst, dst_format="vmdk",
                                       stream_optimized=True)
                    temp.append(dst)
                    files.append({"file": f"{vm['name']}-disk{i + 1}.vmdk", "size": r["size"],
                                  "capacity": d.get("capacity") or r.get("virtual_size") or 0})
                tc.progress(80, "Writing the OVA")
                text = ovf.build(vm, files)
                res = ctx.agent.call("vmio.pack_ova", timeout=6 * 3600, ovf_text=text, ovf_name=f"{vm['name']}.ovf",
                                     disks=temp, out=f"{out_dir.rstrip('/')}/{ova}")
            finally:
                if temp:
                    try:
                        ctx.agent.call("vmio.cleanup", timeout=300, paths=temp)
                    except Exception:  # noqa: BLE001
                        pass
            hv.refresh_pool(ds)
            ctx.bus.event("info", "vm", f"{vm['name']} exported to {ds}/{ova}", uuid, vm["name"], req.username)
            return {"message": f"Exported to [{ds}] {ova} ({res['size'] // 1048576} MiB) - download it from the "
                               f"datastore browser", "datastore": ds, "file": ova}
        t = ctx.tasks.submit("vm.export", f"Export {vm['name']} to OVA", run, req.username, f"vm:{uuid}", vm["name"],
                             "vm")
        return json_response(t, 202)

    @handler
    async def clone(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        b = req.json()
        name = v.name(v.require(b, "name"))
        as_template = v.boolean(b.get("template", False), "template")
        power_on = v.boolean(b.get("power_on", False), "power_on")
        if power_on:
            ctx.require_not_maintenance()
        detail = await ctx.run(hv.vm_brief, uuid)
        label = "Deploy from template" if detail["template"] and not as_template else (
            "Clone to template" if as_template else "Clone")
        ctx.audit(req, "vm.clone", detail["name"], "submitted", new_name=name, template=as_template)
        t = ctx.tasks.submit("vm.clone", f"{label} {detail['name']} → {name}",
                             lambda tc: hv.clone_vm(uuid, name, as_template, power_on, req.username, tc.progress),
                             req.username, f"vm:{uuid}", detail["name"], "vm")
        return json_response(t, 202)

    @handler
    async def template(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        is_t = v.boolean(req.json().get("template"), "template")
        await ctx.run(hv.set_template, uuid, is_t)
        detail = await ctx.run(hv.vm_detail, uuid)
        ctx.audit(req, "vm.template", detail["name"], template=is_t)
        ctx.bus.event("info", "vm", f"{detail['name']} {'converted to template' if is_t else 'converted to VM'}",
                      uuid, detail["name"], req.username)
        return json_response(detail)

    @handler
    async def snapshots(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_snapshots, v.uuid(req.params["uuid"])))

    @handler
    async def snapshot_create(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        b = req.json()
        name = v.name(v.require(b, "name"), "name")
        desc = v.text(b.get("description", ""), "description", 500)
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.snapshot.create", detail["name"], "submitted", snapshot=name)
        t = ctx.tasks.submit("vm.snapshot.create", f"Create snapshot {name} of {detail['name']}",
                             lambda tc: hv.create_snapshot(uuid, name, desc), req.username, f"vm:{uuid}",
                             detail["name"], "vm")
        return json_response(t, 202)

    @handler
    async def snapshot_revert(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        name = v.name(req.params["name"])
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.snapshot.revert", detail["name"], "submitted", snapshot=name)
        t = ctx.tasks.submit("vm.snapshot.revert", f"Revert {detail['name']} to snapshot {name}",
                             lambda tc: hv.revert_snapshot(uuid, name), req.username, f"vm:{uuid}", detail["name"], "vm")
        return json_response(t, 202)

    @handler
    async def snapshot_delete(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        name = v.name(req.params["name"])
        children = req.q("children") == "1"
        detail = await ctx.run(hv.vm_brief, uuid)
        ctx.audit(req, "vm.snapshot.delete", detail["name"], "submitted", snapshot=name)
        t = ctx.tasks.submit("vm.snapshot.delete", f"Delete snapshot {name} of {detail['name']}",
                             lambda tc: hv.delete_snapshot(uuid, name, children), req.username, f"vm:{uuid}",
                             detail["name"], "vm")
        return json_response(t, 202)

    # -- console --------------------------------------------------------
    @handler
    async def console_ticket(req: Request) -> Response:
        uuid = v.uuid(req.params["uuid"])
        ep = await ctx.run(hv.console_endpoint, uuid)
        now = time.time()
        for k in [k for k, t in ctx.console_tickets.items() if t["expires"] < now]:
            ctx.console_tickets.pop(k, None)
        ticket = security.new_token(24)
        ctx.console_tickets[ticket] = {"uuid": uuid, "user_id": req.user["id"], "expires": now + 30,
                                       "session": req.session["token_hash"]}
        ctx.audit(req, "vm.console.open", ep["name"])
        return json_response({"ticket": ticket, "password": ep["password"], "name": ep["name"],
                              "path": f"/api/v1/console?ticket={ticket}"})

    async def console_ws(req: Request, ws: WebSocket) -> None:
        ticket = req.q("ticket", "")
        t = ctx.console_tickets.pop(ticket, None)
        if not t or t["expires"] < time.time() or t["user_id"] != req.user["id"] or \
                t["session"] != req.session["token_hash"] or "vm.console" not in req.permissions:
            await ws.close(4401, "invalid or expired console ticket")
            return
        try:
            ep = await ctx.run(hv.console_endpoint, t["uuid"])
            reader, writer = await asyncio.open_connection(ep["host"], ep["port"])
        except Exception:
            await ws.close(4500, "console unavailable")
            return

        async def tcp_to_ws():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)
            finally:
                await ws.close()

        async def ws_to_tcp():
            try:
                while True:
                    _, data = await ws.receive()
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        tasks = [asyncio.ensure_future(tcp_to_ws()), asyncio.ensure_future(ws_to_tcp())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for tk in tasks:
                tk.cancel()
            writer.close()

    @handler
    async def screenshot(req: Request) -> Response:
        data = await ctx.run(hv.screenshot, v.uuid(req.params["uuid"]))
        if not data:
            raise HTTPError(404, "No screenshot available (VM is powered off)")
        ctype = "image/png" if data[:4] == b"\x89PNG" else "image/x-portable-pixmap"
        return Response(200, data, {"Content-Type": ctype, "Cache-Control": "no-store"})

    r.add("GET", "/api/v1/hypervisor", hypervisor_info, "vm.view")
    r.add("GET", "/api/v1/host-devices", host_devices, "vm.view")
    r.add("POST", "/api/v1/vms/import/inspect", import_inspect, "vm.manage")
    r.add("POST", "/api/v1/vms/import", import_vm, "vm.manage")
    r.add("POST", "/api/v1/vms/{uuid}/export", export_vm, "vm.manage")
    r.add("POST", "/api/v1/vms/{uuid}/guest-tools", guest_tools, "vm.manage")
    r.add("DELETE", "/api/v1/vms/{uuid}/guest-tools", guest_tools, "vm.manage")
    r.add("GET", "/api/v1/vms/{uuid}/metrics", vm_metrics, "vm.view")
    r.add("GET", "/api/v1/vms", list_vms, "vm.view")
    r.add("POST", "/api/v1/vms", create_vm, "vm.manage")
    r.add("GET", "/api/v1/vms/{uuid}", get_vm, "vm.view")
    r.add("PATCH", "/api/v1/vms/{uuid}", update_vm, "vm.manage")
    r.add("DELETE", "/api/v1/vms/{uuid}", delete_vm, "vm.manage")
    r.add("POST", "/api/v1/vms/{uuid}/power/{action}", power, "vm.power")
    r.add("PUT", "/api/v1/vms/{uuid}/autostart", autostart, "vm.manage")
    r.add("POST", "/api/v1/vms/{uuid}/clone", clone, "vm.manage")
    r.add("PUT", "/api/v1/vms/{uuid}/template", template, "vm.manage")
    r.add("GET", "/api/v1/vms/{uuid}/snapshots", snapshots, "vm.view")
    r.add("POST", "/api/v1/vms/{uuid}/snapshots", snapshot_create, "vm.snapshot")
    r.add("POST", "/api/v1/vms/{uuid}/snapshots/{name}/revert", snapshot_revert, "vm.snapshot")
    r.add("DELETE", "/api/v1/vms/{uuid}/snapshots/{name}", snapshot_delete, "vm.snapshot")
    r.add("POST", "/api/v1/vms/{uuid}/console-ticket", console_ticket, "vm.console")
    r.add("GET", "/api/v1/vms/{uuid}/screenshot", screenshot, "vm.console")
    r.add("GET", "/api/v1/console", console_ws, "vm.console", websocket=True)
