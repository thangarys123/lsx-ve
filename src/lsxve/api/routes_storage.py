"""Storage endpoints: datastores (libvirt pools), volumes, physical disks, ISO library."""
from __future__ import annotations

import asyncio
import re
import time

from ..ct import lxc
from ..common import config, validate as v
from ..host import hardware
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, json_response

DATASTORE_UPLOAD_EXT = (".ova", ".ovf", ".vmdk", ".qcow2", ".img", ".raw", ".vhd", ".vhdx", ".vdi", ".iso", ".mf",
                        ".nvram")
ALLOWED_DIR_PREFIXES = ("/lsxve/", "/var/lib/libvirt/images", "/srv/", "/mnt/", "/data/")
RE_VG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,62}$")


def _pool_spec(b: dict) -> dict:
    t = v.choice(b.get("type"), "type", ("dir", "netfs", "logical", "iscsi"))
    spec = {"type": t, "name": v.name(v.require(b, "name"))}
    if t in ("dir", "netfs"):
        path = v.string(b.get("path") or f"{config.DATASTORE_ROOT}/{spec['name']}", "path", 2, 200, v.RE_EXPORT)
        if not any(path == p.rstrip("/") or path.startswith(p) for p in ALLOWED_DIR_PREFIXES) or ".." in path:
            raise v.ValidationError("path", "datastore paths must be under " + ", ".join(ALLOWED_DIR_PREFIXES))
        spec["path"] = path
    if t == "netfs":
        spec["host"] = v.host_or_ip(v.require(b, "host"), "host")
        spec["export"] = v.string(v.require(b, "export"), "export", 1, 250, v.RE_EXPORT)
    if t == "logical":
        spec["vg"] = v.string(v.require(b, "vg"), "vg", 1, 63, RE_VG)
    if t == "iscsi":
        spec["host"] = v.host_or_ip(v.require(b, "host"), "host")
        spec["port"] = v.integer(b.get("port", 3260), "port", 1, 65535)
        spec["iqn"] = v.string(v.require(b, "iqn"), "iqn", 4, 223, v.RE_IQN)
    return spec


def register(ctx: Ctx) -> None:
    r = ctx.router
    hv = ctx.hv

    @handler
    async def pools(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_pools))

    @handler
    async def pool_create(req: Request) -> Response:
        spec = _pool_spec(req.json())
        ctx.audit(req, "storage.datastore.create", spec["name"], "submitted", type=spec["type"])
        t = ctx.tasks.submit("storage.datastore.create", f"Create datastore {spec['name']} ({spec['type']})",
                             lambda tc: hv.create_pool(spec), req.username, f"pool:{spec['name']}", spec["name"],
                             "storage")
        return json_response(t, 202)

    @handler
    async def pool_action(req: Request) -> Response:
        name = v.name(req.params["name"])
        action = v.choice(req.params["action"], "action", ("start", "stop", "refresh", "autostart_on", "autostart_off"))
        res = await ctx.run(hv.pool_action, name, action)
        ctx.audit(req, f"storage.datastore.{action}", name)
        ctx.bus.event("info", "storage", f"Datastore {name}: {action.replace('_', ' ')}", f"pool:{name}", name,
                      req.username)
        return json_response(res)

    @handler
    async def pool_delete(req: Request) -> Response:
        name = v.name(req.params["name"])
        res = await ctx.run(hv.remove_pool, name)
        ctx.audit(req, "storage.datastore.remove", name)
        ctx.bus.event("warning", "storage", f"Datastore {name} removed", f"pool:{name}", name, req.username)
        return json_response(res)

    @handler
    async def volumes(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_volumes, v.name(req.params["name"])))

    @handler
    async def volume_create(req: Request) -> Response:
        pool = v.name(req.params["name"])
        b = req.json()
        name = v.string(v.require(b, "name"), "name", 1, 120, v.RE_FILENAME)
        size = v.integer(v.require(b, "size_gib"), "size_gib", 1, 65536)
        fmt = v.choice(b.get("format", "qcow2"), "format", ("qcow2", "raw"))
        if fmt == "qcow2" and not name.endswith(".qcow2"):
            name += ".qcow2"
        thick = v.choice(b.get("provisioning", "thin"), "provisioning", ("thin", "thick")) == "thick"
        ctx.audit(req, "storage.volume.create", f"{pool}/{name}", "submitted", size_gib=size)
        t = ctx.tasks.submit("storage.volume.create", f"Create virtual disk {name} ({size} GiB) in {pool}",
                             lambda tc: hv.create_volume(pool, name, size, fmt, thick), req.username,
                             f"vol:{pool}/{name}", name, "storage")
        return json_response(t, 202)

    @handler
    async def volume_delete(req: Request) -> Response:
        pool = v.name(req.params["name"])
        vol = v.string(req.params["vol"], "volume", 1, 200, v.RE_FILENAME)
        res = await ctx.run(hv.delete_volume, pool, vol)
        ctx.audit(req, "storage.volume.delete", f"{pool}/{vol}")
        ctx.bus.event("warning", "storage", f"Virtual disk {vol} deleted from {pool}", f"vol:{pool}/{vol}", vol,
                      req.username)
        return json_response(res)

    @handler
    async def volume_resize(req: Request) -> Response:
        pool = v.name(req.params["name"])
        vol = v.string(req.params["vol"], "volume", 1, 200, v.RE_FILENAME)
        size = v.integer(v.require(req.json(), "size_gib"), "size_gib", 1, 65536)
        ctx.audit(req, "storage.volume.resize", f"{pool}/{vol}", "submitted", size_gib=size)
        t = ctx.tasks.submit("storage.volume.resize", f"Grow virtual disk {vol} to {size} GiB",
                             lambda tc: hv.resize_volume(pool, vol, size), req.username, f"vol:{pool}/{vol}", vol,
                             "storage")
        return json_response(t, 202)

    # -- physical disks -------------------------------------------------
    @handler
    async def disks(req: Request) -> Response:
        devs = await ctx.run(hardware.block_devices)
        disks_ = [d for d in devs if d.get("type") == "disk" and not d.get("name", "").startswith(("zram", "loop"))]
        for d in disks_:
            try:
                d["check"] = await ctx.run(ctx.agent.call, "storage.disk_check", dev=d["name"])
            except Exception as e:
                d["check"] = {"in_use": True, "reasons": [f"cannot verify disk usage: {e}"]}
        return json_response(disks_)

    @handler
    async def disk_smart(req: Request) -> Response:
        dev = v.string(req.params["dev"], "device", 3, 16, v.RE_DEVNAME)
        return json_response(await ctx.run(ctx.agent.call, "storage.smart", dev=dev))

    @handler
    async def disk_format(req: Request) -> Response:
        dev = v.string(req.params["dev"], "device", 3, 16, v.RE_DEVNAME)
        b = req.json()
        kind = v.choice(b.get("kind"), "kind", ("xfs", "ext4", "lvm"))
        name = v.name(v.require(b, "name"))
        confirm = v.string(v.require(b, "confirm"), "confirm", 1, 40)
        if confirm != f"/dev/{dev}":
            raise v.ValidationError("confirm", f"type /dev/{dev} to confirm that all data on the disk will be erased")
        check = await ctx.run(ctx.agent.call, "storage.disk_check", dev=dev)
        if check["in_use"]:
            raise HTTPError(409, "Disk is in use: " + "; ".join(check["reasons"]), "disk_in_use")
        ctx.audit(req, "storage.disk.format", f"/dev/{dev}", "submitted", kind=kind, name=name,
                  serial=check.get("serial"))
        ctx.bus.event("warning", "storage", f"Erasing /dev/{dev} to create datastore {name}", f"disk:{dev}", dev,
                      req.username)

        def run(tc):
            tc.progress(10, f"Erasing and formatting /dev/{dev}")
            res = ctx.agent.call("storage.create_datastore", timeout=3600, dev=dev, kind=kind, name=name,
                                 confirm=confirm)
            tc.progress(80, "Registering datastore")
            if kind == "lvm":
                hv.create_pool({"type": "logical", "name": name, "vg": res["vg"]})
            else:
                hv.create_pool({"type": "dir", "name": name, "path": res["path"]})
            return {"message": f"Datastore {name} created on /dev/{dev}", **res}
        t = ctx.tasks.submit("storage.disk.format", f"Create datastore {name} on /dev/{dev} ({kind})", run,
                             req.username, f"disk:{dev}", dev, "storage")
        return json_response(t, 202)

    @handler
    async def filesystems(req: Request) -> Response:
        def collect():
            out = {"mounts": [], "lvm": {"vgs": [], "lvs": []}}
            try:
                out["mounts"] = ctx.agent.call("storage.mounts")
                out["lvm"] = ctx.agent.call("storage.lvm")
            except Exception as e:
                out["error"] = str(e)
            return out
        return json_response(await ctx.run(collect))

    # -- ISO library ----------------------------------------------------
    @handler
    async def isos(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_isos))

    @handler
    async def iso_delete(req: Request) -> Response:
        name = v.string(req.params["name"], "name", 1, 200, v.RE_FILENAME)
        res = await ctx.run(hv.delete_iso, name)
        ctx.audit(req, "storage.iso.delete", name)
        ctx.bus.event("info", "storage", f"ISO image {name} deleted", f"iso:{name}", name, req.username)
        return json_response(res)

    async def iso_upload(req: Request) -> Response:
        """Streaming upload: PUT /api/v1/storage/isos/{name} with the raw file as body."""
        try:
            name = v.string(req.params["name"], "name", 5, 200, v.RE_FILENAME)
            if not name.lower().endswith((".iso", ".img") + lxc.TARBALL_EXT):
                raise v.ValidationError("name", "only .iso / .img images or container rootfs archives "
                                                "(.tar, .tar.gz, .tar.xz, .tar.zst, .tar.bz2) can be uploaded")
            size = req.content_length
            if size <= 0:
                raise HTTPError(411, "Content-Length required")
            vol, stream = await ctx.run(hv.begin_iso_upload, name, size)
        except Exception as exc:
            from .context import map_error
            raise map_error(exc)
        ctx.audit(req, "storage.iso.upload", name, "started", size=size)
        task_info = {"done": 0}
        holder = {}

        def watch(tc):  # a task that mirrors upload progress in "Recent tasks"
            holder["tc"] = tc
            while "finished" not in holder:
                time.sleep(1)
                tc.progress(int(task_info["done"] * 100 / size), f"{task_info['done'] // 1048576} MiB received")
            if holder["finished"] is not True:
                raise RuntimeError(holder["finished"])
            return {"message": f"Uploaded {name} ({size // 1048576} MiB)"}
        task = ctx.tasks.submit("storage.iso.upload", f"Upload ISO image {name}", watch, req.username, f"iso:{name}",
                                name, "storage")
        try:
            remaining = size
            while remaining:
                chunk = await asyncio.wait_for(req.reader.read(min(1048576, remaining)), 60)
                if not chunk:
                    raise ConnectionError("client disconnected")
                await ctx.run(stream.send, chunk)
                remaining -= len(chunk)
                task_info["done"] = size - remaining
            req.content_length = 0
            await ctx.run(stream.finish)
            holder["finished"] = True
        except Exception as e:
            holder["finished"] = f"upload aborted: {e}"
            try:
                await ctx.run(stream.abort)
            except Exception:
                pass
            try:
                await ctx.run(vol.delete, 0)
            except Exception:
                pass
            ctx.audit(req, "storage.iso.upload", name, "failure", error=str(e))
            raise HTTPError(400, f"Upload failed: {e}", "upload_failed")
        ctx.audit(req, "storage.iso.upload", name, "success", size=size)
        return json_response({"name": name, "size": size, "task": task["id"]}, 201)

    # -- datastore browser: upload / download files (OVA, OVF, VMDK, disk images) ------------------------
    async def file_upload(req: Request) -> Response:
        """PUT /api/v1/storage/datastores/{name}/files/{file} with the raw file as body (streamed via libvirt)."""
        try:
            pool = v.name(req.params["name"])
            name = v.string(req.params["file"], "file", 3, 200, v.RE_FILENAME)
            if not name.lower().endswith(DATASTORE_UPLOAD_EXT):
                raise v.ValidationError("file", "allowed: " + ", ".join(DATASTORE_UPLOAD_EXT))
            size = req.content_length
            if size <= 0:
                raise HTTPError(411, "Content-Length required")
            vol, stream = await ctx.run(hv.begin_volume_upload, pool, name, size)
        except Exception as exc:
            from .context import map_error
            raise map_error(exc)
        ctx.audit(req, "storage.file.upload", f"{pool}/{name}", "started", size=size)
        progress = {"done": 0}
        holder = {}

        def watch(tc):
            while "finished" not in holder:
                time.sleep(1)
                tc.progress(int(progress["done"] * 100 / size), f"{progress['done'] // 1048576} MiB received")
            if holder["finished"] is not True:
                raise RuntimeError(holder["finished"])
            return {"message": f"Uploaded {name} to {pool} ({size // 1048576} MiB)"}
        task = ctx.tasks.submit("storage.file.upload", f"Upload {name} to datastore {pool}", watch, req.username,
                                f"vol:{pool}/{name}", name, "storage")
        try:
            remaining = size
            while remaining:
                chunk = await asyncio.wait_for(req.reader.read(min(1048576, remaining)), 60)
                if not chunk:
                    raise ConnectionError("client disconnected")
                await ctx.run(stream.send, chunk)
                remaining -= len(chunk)
                progress["done"] = size - remaining
            req.content_length = 0
            await ctx.run(stream.finish)
            holder["finished"] = True
        except Exception as e:
            holder["finished"] = f"upload aborted: {e}"
            for fn in (stream.abort, lambda: vol.delete(0)):
                try:
                    await ctx.run(fn)
                except Exception:
                    pass
            ctx.audit(req, "storage.file.upload", f"{pool}/{name}", "failure", error=str(e))
            raise HTTPError(400, f"Upload failed: {e}", "upload_failed")
        await ctx.run(hv.refresh_pool, pool)
        ctx.audit(req, "storage.file.upload", f"{pool}/{name}", "success", size=size)
        return json_response({"name": name, "size": size, "task": task["id"]}, 201)

    @handler
    async def file_download(req: Request) -> Response:
        pool = v.name(req.params["name"])
        name = v.string(req.params["file"], "file", 1, 200, v.RE_FILENAME)
        vol, st = await ctx.run(hv.begin_volume_download, pool, name)
        ctx.audit(req, "storage.file.download", f"{pool}/{name}")

        async def chunks():
            ok = False
            try:
                while True:
                    data = await ctx.run(st.recv, 1048576)
                    if not data or isinstance(data, int):  # b"" = end of file (blocking stream)
                        break
                    yield data
                ok = True
            finally:
                try:
                    await ctx.run(st.finish if ok else st.abort)
                except Exception:
                    pass
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", name)
        return Response(200, b"", {"Content-Type": "application/octet-stream", "Cache-Control": "no-store",
                                   "Content-Disposition": f'attachment; filename="{safe}"'}, chunks=chunks())

    r.add("PUT", "/api/v1/storage/datastores/{name}/files/{file}", file_upload, "storage.manage", stream=True,
          max_body=ctx.cfg["max_upload_bytes"])
    r.add("GET", "/api/v1/storage/datastores/{name}/files/{file}", file_download, "storage.view")
    r.add("GET", "/api/v1/storage/datastores", pools, "storage.view")
    r.add("POST", "/api/v1/storage/datastores", pool_create, "storage.manage")
    r.add("POST", "/api/v1/storage/datastores/{name}/actions/{action}", pool_action, "storage.manage")
    r.add("DELETE", "/api/v1/storage/datastores/{name}", pool_delete, "storage.manage")
    r.add("GET", "/api/v1/storage/datastores/{name}/volumes", volumes, "storage.view")
    r.add("POST", "/api/v1/storage/datastores/{name}/volumes", volume_create, "storage.manage")
    r.add("DELETE", "/api/v1/storage/datastores/{name}/volumes/{vol}", volume_delete, "storage.manage")
    r.add("POST", "/api/v1/storage/datastores/{name}/volumes/{vol}/resize", volume_resize, "storage.manage")
    r.add("GET", "/api/v1/storage/disks", disks, "storage.view")
    r.add("GET", "/api/v1/storage/disks/{dev}/smart", disk_smart, "storage.view")
    r.add("POST", "/api/v1/storage/disks/{dev}/format", disk_format, "storage.format")
    r.add("GET", "/api/v1/storage/filesystems", filesystems, "storage.view")
    r.add("GET", "/api/v1/storage/isos", isos, "storage.view")
    r.add("PUT", "/api/v1/storage/isos/{name}", iso_upload, "storage.manage", stream=True,
          max_body=ctx.cfg["max_upload_bytes"])
    r.add("DELETE", "/api/v1/storage/isos/{name}", iso_delete, "storage.manage")
