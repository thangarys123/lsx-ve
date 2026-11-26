"""LXC container endpoints.

The API process is unprivileged: every container operation goes through
lsxve-agent (ct.* operations), which validates its arguments again. Long
operations (create, delete, clone, snapshots, power) run as tasks.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time

from ..common import security, validate as v
from ..ct import lxc
from .context import Ctx, handler
from .httpd import Request, Response, WebSocket, json_response

POWER_LABELS = {"start": "Start", "shutdown": "Shut down", "stop": "Stop", "reboot": "Restart",
                "freeze": "Freeze", "unfreeze": "Resume"}


def _name(req: Request) -> str:
    return lxc.ct_name(req.params["name"])


def register(ctx: Ctx) -> None:
    r = ctx.router
    agent = ctx.agent

    def changed(name: str, what: str) -> None:
        ctx.bus.broadcast("ct", {"name": name, "change": what})

    def task(kind: str, desc: str, name: str, req: Request, fn, timeout: float = 600) -> Response:
        def run(tc):
            try:
                return fn(timeout)
            finally:
                changed(name, kind)
        t = ctx.tasks.submit(kind, desc, run, req.username, f"ct:{name}", name, "ct")
        return json_response(t, 202)

    @handler
    async def status(req: Request) -> Response:
        st = await ctx.run(agent.call, "ct.status", timeout=20)
        nets = {}
        try:
            for n in await ctx.run(ctx.hv.list_networks):
                if n.get("bridge"):
                    nets[n["bridge"]] = n["name"]
        except Exception:  # noqa: BLE001 - libvirt down: bridges are still listed
            pass
        st["bridges"] = [{"name": b, "network": nets.get(b, "")} for b in st.get("bridges", [])]
        return json_response(st)

    @handler
    async def images(req: Request) -> Response:
        return json_response(await ctx.run(agent.call, "ct.images", timeout=40, refresh=req.q("refresh") == "1"))

    @handler
    async def list_cts(req: Request) -> Response:
        return json_response(await ctx.run(agent.call, "ct.list", timeout=60))

    @handler
    async def get_ct(req: Request) -> Response:
        return json_response(await ctx.run(agent.call, "ct.get", timeout=30, name=_name(req)))

    @handler
    async def create_ct(req: Request) -> Response:
        b = req.json()
        spec = dict(b) if isinstance(b, dict) else {}
        spec["created_by"] = req.username
        s = lxc.validate_spec(spec)  # fail fast with 422; the agent validates again
        ctx.audit(req, "ct.create", s["name"], "submitted", source=s["source"], memory_mib=s["memory_mib"],
                  cpus=s["cpus"], unprivileged=s["unprivileged"])
        return task("ct.create", f"Create container {s['name']}", s["name"], req,
                    lambda t: agent.call("ct.create", timeout=2000, spec=spec))

    @handler
    async def update_ct(req: Request) -> Response:
        name = _name(req)
        b = req.json()
        if not isinstance(b, dict) or not b:
            raise v.ValidationError("body", "no changes supplied")
        allowed = {"memory_mib", "cpus", "autostart", "bridge", "ipv4", "gateway", "description", "root_password"}
        extra = set(b) - allowed
        if extra:
            raise v.ValidationError(sorted(extra)[0], "cannot be changed")
        res = await ctx.run(agent.call, "ct.update", timeout=120, name=name, changes=b)
        ctx.audit(req, "ct.update", name, changes=sorted(b))
        ctx.bus.event("info", "ct", f"Container {name} reconfigured", f"ct:{name}", name, req.username)
        changed(name, "update")
        return json_response(res)

    @handler
    async def delete_ct(req: Request) -> Response:
        name = _name(req)
        ctx.audit(req, "ct.delete", name, "submitted")
        return task("ct.delete", f"Delete container {name}", name, req,
                    lambda t: agent.call("ct.delete", timeout=1900, name=name))

    @handler
    async def power(req: Request) -> Response:
        name = _name(req)
        action = v.choice(req.params["action"], "action", POWER_LABELS)
        if action in ("start", "reboot", "unfreeze"):
            ctx.require_not_maintenance("Starting containers")
        ctx.audit(req, f"ct.power.{action}", name)

        def run(t):
            res = agent.call("ct.action", timeout=180, name=name, action=action)
            ctx.bus.event("info", "ct", f"Container {name}: {POWER_LABELS[action].lower()} ({res.get('state')})",
                          f"ct:{name}", name, req.username)
            return res
        return task(f"ct.power.{action}", f"{POWER_LABELS[action]} container {name}", name, req, run)

    @handler
    async def clone(req: Request) -> Response:
        name = _name(req)
        new = lxc.ct_name(v.require(req.json(), "name"), "name")
        ctx.audit(req, "ct.clone", name, "submitted", new_name=new)
        return task("ct.clone", f"Clone container {name} → {new}", name, req,
                    lambda t: agent.call("ct.clone", timeout=3700, name=name, new_name=new))

    @handler
    async def snapshots(req: Request) -> Response:
        return json_response(await ctx.run(agent.call, "ct.snapshots", timeout=30, name=_name(req)))

    @handler
    async def snapshot_create(req: Request) -> Response:
        name = _name(req)
        b = req.json()
        comment = v.text(b.get("comment", "") if isinstance(b, dict) else "", "comment", 500)
        ctx.audit(req, "ct.snapshot.create", name, "submitted")
        return task("ct.snapshot.create", f"Snapshot container {name}", name, req,
                    lambda t: agent.call("ct.snapshot_create", timeout=3700, name=name, comment=comment))

    @handler
    async def snapshot_restore(req: Request) -> Response:
        name = _name(req)
        snap = v.string(req.params["snap"], "snapshot", 5, 8, lxc.RE_SNAP)
        ctx.audit(req, "ct.snapshot.restore", name, "submitted", snapshot=snap)
        return task("ct.snapshot.restore", f"Restore container {name} to {snap}", name, req,
                    lambda t: agent.call("ct.snapshot_restore", timeout=3700, name=name, snapshot=snap))

    @handler
    async def snapshot_delete(req: Request) -> Response:
        name = _name(req)
        snap = v.string(req.params["snap"], "snapshot", 5, 8, lxc.RE_SNAP)
        ctx.audit(req, "ct.snapshot.delete", name, "submitted", snapshot=snap)
        return task("ct.snapshot.delete", f"Delete snapshot {snap} of {name}", name, req,
                    lambda t: agent.call("ct.snapshot_delete", timeout=1900, name=name, snapshot=snap))

    # -- console: xterm.js <-> WebSocket <-> pty (lxc-console, started by the agent) ------------
    @handler
    async def console_ticket(req: Request) -> Response:
        name = _name(req)
        info = await ctx.run(agent.call, "ct.get", timeout=30, name=name)
        if info["state"] != "running":
            raise v.ValidationError("state", f"Container {name} is not running")
        now = time.time()
        for k in [k for k, t in ctx.console_tickets.items() if t["expires"] < now]:
            ctx.console_tickets.pop(k, None)
        ticket = security.new_token(24)
        ctx.console_tickets[ticket] = {"ct": name, "user_id": req.user["id"], "expires": now + 30,
                                       "session": req.session["token_hash"]}
        ctx.audit(req, "ct.console.open", name)
        return json_response({"ticket": ticket, "name": name, "path": f"/api/v1/ct-console?ticket={ticket}"})

    async def console_ws(req: Request, ws: WebSocket) -> None:
        t = ctx.console_tickets.pop(req.q("ticket", ""), None)
        if not t or "ct" not in t or t["expires"] < time.time() or t["user_id"] != req.user["id"] or \
                t["session"] != req.session["token_hash"] or "ct.console" not in req.permissions:
            await ws.close(4401, "invalid or expired console ticket")
            return
        try:
            cols = v.integer(req.q("cols", "80"), "cols", 10, 1000)
            rows = v.integer(req.q("rows", "24"), "rows", 5, 500)
            _info, fd = await ctx.run(agent.call_fd, "ct.console", name=t["ct"], cols=cols, rows=rows)
        except Exception as e:  # noqa: BLE001
            await ws.close(4500, f"console unavailable: {getattr(e, 'message', e)}"[:120])
            return
        import fcntl  # POSIX only; imported here so the API still loads in a non-Linux dev checkout
        import termios
        os.set_blocking(fd, False)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def readable():
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(fd)
            q.put_nowait(data)
        loop.add_reader(fd, readable)

        async def pty_to_ws():
            while True:
                data = await q.get()
                if not data:
                    break
                await ws.send_bytes(data)

        async def ws_to_pty():
            while True:
                opcode, data = await ws.receive()
                if opcode == 0x1:  # text frame: control message
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "resize":
                        try:
                            c = v.integer(msg.get("cols"), "cols", 10, 1000)
                            rw = v.integer(msg.get("rows"), "rows", 5, 500)
                        except v.ValidationError:
                            continue
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rw, c, 0, 0))
                    continue
                view = memoryview(data)
                while view:
                    try:
                        n = os.write(fd, view)
                        view = view[n:]
                    except BlockingIOError:
                        await asyncio.sleep(0.01)

        tasks = [asyncio.ensure_future(pty_to_ws()), asyncio.ensure_future(ws_to_pty())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for tk in tasks:
                tk.cancel()
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            os.close(fd)  # lxc-console sees the hang-up and exits
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    r.add("GET", "/api/v1/containers/status", status, "ct.view")
    r.add("GET", "/api/v1/containers/images", images, "ct.manage")
    r.add("GET", "/api/v1/containers", list_cts, "ct.view")
    r.add("POST", "/api/v1/containers", create_ct, "ct.manage")
    r.add("GET", "/api/v1/containers/{name}", get_ct, "ct.view")
    r.add("PATCH", "/api/v1/containers/{name}", update_ct, "ct.manage")
    r.add("DELETE", "/api/v1/containers/{name}", delete_ct, "ct.manage")
    r.add("POST", "/api/v1/containers/{name}/power/{action}", power, "ct.power")
    r.add("POST", "/api/v1/containers/{name}/clone", clone, "ct.manage")
    r.add("GET", "/api/v1/containers/{name}/snapshots", snapshots, "ct.view")
    r.add("POST", "/api/v1/containers/{name}/snapshots", snapshot_create, "ct.snapshot")
    r.add("POST", "/api/v1/containers/{name}/snapshots/{snap}/restore", snapshot_restore, "ct.snapshot")
    r.add("DELETE", "/api/v1/containers/{name}/snapshots/{snap}", snapshot_delete, "ct.snapshot")
    r.add("POST", "/api/v1/containers/{name}/console-ticket", console_ticket, "ct.console")
    r.add("GET", "/api/v1/ct-console", console_ws, "ct.console", websocket=True)
