"""Networking endpoints: host networking (NetworkManager via agent, with rollback) and virtual networks."""
from __future__ import annotations

import ipaddress

from ..common import validate as v
from ..host import hardware, vswitch
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, json_response


def _ipcfg(b: dict, prefix: str = "") -> dict:
    """Shallow structural validation; the agent validates every value again."""
    out = {}
    for fam in ("ipv4", "ipv6"):
        if fam in b and b[fam] is not None:
            x = b[fam]
            if not isinstance(x, dict):
                raise v.ValidationError(fam, "must be an object")
            out[fam] = {k: x[k] for k in ("method", "addresses", "gateway", "dns") if k in x}
    return out


def _change(b: dict) -> dict:
    op = v.choice(b.get("op"), "op", ("configure", "create_bridge", "create_vlan", "create_bond", "delete"))
    ch = {"op": op, **_ipcfg(b)}
    if "mtu" in b and b["mtu"] not in (None, ""):
        ch["mtu"] = v.integer(b["mtu"], "mtu", 576, 9216)
    if op in ("configure", "delete"):
        ch["uuid"] = v.uuid(b.get("uuid"))
        if "autoconnect" in b:
            ch["autoconnect"] = v.boolean(b["autoconnect"], "autoconnect")
    else:
        ch["name"] = v.ifname(b.get("name"), "name")
    if op == "create_bridge":
        ch["ports"] = v.list_of(b.get("ports", []), "ports", v.ifname, 8)
        ch["stp"] = v.boolean(b.get("stp", False), "stp")
    if op == "create_vlan":
        ch["parent"] = v.ifname(b.get("parent"), "parent")
        ch["vlan_id"] = v.integer(b.get("vlan_id"), "vlan_id", 1, 4094)
    if op == "create_bond":
        ch["mode"] = v.string(b.get("mode", "active-backup"), "mode", 3, 20)
        ch["members"] = v.list_of(b.get("members", []), "members", v.ifname, 8)
    return ch


def _vnet_spec(b: dict) -> dict:
    mode = v.choice(b.get("mode"), "mode", ("nat", "isolated", "route", "bridge"))
    spec = {"name": v.name(v.require(b, "name")), "mode": mode}
    if mode == "bridge":
        spec["bridge"] = v.ifname(v.require(b, "bridge"), "bridge")
        return spec
    spec["ipv4"] = v.ipv4_interface(v.require(b, "ipv4"), "ipv4")
    net = ipaddress.IPv4Interface(spec["ipv4"]).network
    if b.get("dhcp_start") or b.get("dhcp_end"):
        s = ipaddress.IPv4Address(v.ip_address(b.get("dhcp_start"), "dhcp_start", 4))
        e = ipaddress.IPv4Address(v.ip_address(b.get("dhcp_end"), "dhcp_end", 4))
        if s not in net or e not in net or s > e:
            raise v.ValidationError("dhcp_start", "DHCP range must be inside the network and start <= end")
        spec["dhcp_start"], spec["dhcp_end"] = str(s), str(e)
    return spec


def register(ctx: Ctx) -> None:
    r = ctx.router
    hv = ctx.hv

    @handler
    async def host_net(req: Request) -> Response:
        def collect():
            out = {"nics": hardware.physical_nics(), "management": hardware.management_addresses()}
            try:
                out.update(ctx.agent.call("network.state"))
                out["pending"] = ctx.agent.call("network.pending")
                out["vm_bridge"] = ctx.agent.call("network.bridge_status")
                out["agent"] = True
            except Exception as e:
                out["agent"] = False
                out["agent_error"] = str(e)
            return out
        return json_response(await ctx.run(collect))

    @handler
    async def apply(req: Request) -> Response:
        ch = _change(req.json())
        secs = ctx.cfg["network_rollback_seconds"]
        try:
            res = await ctx.run(ctx.agent.call, "network.apply", timeout=180, change=ch, rollback_seconds=secs)
        except Exception as e:
            ctx.audit(req, "network.change", ch.get("name") or ch.get("uuid", ""), "failure", op=ch["op"], error=str(e))
            ctx.bus.event("error", "network", f"Network change failed and was rolled back: {e}", "host",
                          hardware.hostname(), req.username)
            raise
        ctx.audit(req, "network.change", ch.get("name") or ch.get("uuid", ""), "pending", op=ch["op"],
                  checkpoint=res["checkpoint"])
        ctx.bus.event("warning", "network", f"{res['summary']} - awaiting confirmation (automatic rollback in {secs}s)",
                      "host", hardware.hostname(), req.username)
        return json_response(res)

    @handler
    async def pending(req: Request) -> Response:
        return json_response(await ctx.run(ctx.agent.call, "network.pending"))

    @handler
    async def confirm(req: Request) -> Response:
        cp = v.string(v.require(req.json(), "checkpoint"), "checkpoint", 5, 120)
        res = await ctx.run(ctx.agent.call, "network.confirm", checkpoint=cp)
        ctx.audit(req, "network.change.confirm", cp)
        ctx.bus.event("info", "network", f"{res['summary']} - confirmed", "host", hardware.hostname(), req.username)
        return json_response(res)

    @handler
    async def rollback(req: Request) -> Response:
        cp = v.string(v.require(req.json(), "checkpoint"), "checkpoint", 5, 120)
        res = await ctx.run(ctx.agent.call, "network.rollback", checkpoint=cp)
        ctx.audit(req, "network.change.rollback", cp)
        ctx.bus.event("warning", "network", f"{res['summary']} - rolled back", "host", hardware.hostname(), req.username)
        return json_response(res)

    @handler
    async def vnets(req: Request) -> Response:
        return json_response(await ctx.run(hv.list_networks))

    @handler
    async def vnet_create(req: Request) -> Response:
        spec = _vnet_spec(req.json())
        res = await ctx.run(hv.create_network, spec)
        ctx.audit(req, "network.virtual.create", spec["name"], mode=spec["mode"])
        ctx.bus.event("info", "network", f"Virtual network {spec['name']} ({spec['mode']}) created",
                      f"net:{spec['name']}", spec["name"], req.username)
        return json_response(res, 201)

    @handler
    async def vnet_action(req: Request) -> Response:
        name = v.name(req.params["name"])
        action = v.choice(req.params["action"], "action", ("start", "stop", "autostart_on", "autostart_off", "delete"))
        if name == "default" and action == "delete":
            raise HTTPError(409, "The default network cannot be deleted")
        res = await ctx.run(hv.network_action, name, action)
        ctx.audit(req, f"network.virtual.{action}", name)
        ctx.bus.event("info", "network", f"Virtual network {name}: {action.replace('_', ' ')}", f"net:{name}", name,
                      req.username)
        return json_response(res)

    # -- standard virtual switches and port groups (ESXi model) ------------------------------------------
    @handler
    async def vswitches(req: Request) -> Response:
        def collect():
            sw = vswitch.list_vswitches()
            nets = hv.list_networks()
            for s_ in sw:
                s_["portgroups"] = [n["name"] for n in nets
                                    if (n.get("vswitch") or (n["bridge"] if n["mode"] == "bridge" else None)) == s_["name"]]
            return sw
        return json_response(await ctx.run(collect))

    def _portgroups():
        out = []
        for n in hv.list_networks():
            pg = dict(n)
            if n["mode"] == "bridge":
                pg["vswitch"] = n.get("vswitch") or n["bridge"]
                pg["vlan"] = n.get("vlan") or 0
                pg["kind"] = "portgroup"
            else:
                pg["kind"] = "virtual"  # NAT / routed / isolated libvirt network
            out.append(pg)
        return out

    @handler
    async def portgroups(req: Request) -> Response:
        return json_response(await ctx.run(_portgroups))

    @handler
    async def portgroup_create(req: Request) -> Response:
        b = req.json()
        name = v.name(v.require(b, "name"))
        sw = v.ifname(v.require(b, "vswitch"), "vswitch")
        vid = v.integer(b.get("vlan_id", 0), "vlan_id", 0, 4094)
        if not any(x["name"] == sw for x in await ctx.run(vswitch.list_vswitches)):
            raise v.ValidationError("vswitch", f"{sw} is not a virtual switch")
        ctx.audit(req, "network.portgroup.create", name, "submitted", vswitch=sw, vlan=vid)

        def run(tc):
            bridge = sw
            if vid:
                tc.progress(20, f"Creating VLAN {vid} on {sw}")
                bridge = ctx.agent.call("network.vswitch_backing_create", timeout=180, vswitch=sw, vlan_id=vid)["bridge"]
            tc.progress(70, "Creating port group")
            try:
                hv.create_network({"name": name, "mode": "bridge", "bridge": bridge, "vswitch": sw, "vlan": vid})
            except Exception:
                if vid and not _backing_used(sw, vid, exclude=name):
                    ctx.agent.call("network.vswitch_backing_remove", timeout=120, vswitch=sw, vlan_id=vid)
                raise
            ctx.bus.event("info", "network", f"Port group {name} created on {sw}" + (f" (VLAN {vid})" if vid else ""),
                          f"net:{name}", name, req.username)
            return {"message": f"Port group {name} created", "bridge": bridge}
        t = ctx.tasks.submit("network.portgroup.create", f"Add port group {name}", run, req.username, f"net:{name}",
                             name, "network")
        return json_response(t, 202)

    def _backing_used(sw: str, vid: int, exclude: str) -> bool:
        return any(p["name"] != exclude and p.get("vswitch") == sw and p.get("vlan") == vid for p in _portgroups())

    @handler
    async def portgroup_delete(req: Request) -> Response:
        name = v.name(req.params["name"])
        if name == "default":
            raise HTTPError(409, "The default port group cannot be removed")
        pg = next((p for p in await ctx.run(_portgroups) if p["name"] == name), None)
        if pg is None:
            raise HTTPError(404, f"Port group {name} not found")
        if pg["used_by"]:
            raise HTTPError(409, f"Port group {name} is used by: {', '.join(pg['used_by'])}")
        ctx.audit(req, "network.portgroup.delete", name, "submitted")

        def run(tc):
            hv.network_action(name, "delete")
            if pg["kind"] == "portgroup" and pg.get("vlan") and not _backing_used(pg["vswitch"], pg["vlan"], name):
                tc.progress(60, f"Removing VLAN {pg['vlan']} from {pg['vswitch']}")
                ctx.agent.call("network.vswitch_backing_remove", timeout=120, vswitch=pg["vswitch"], vlan_id=pg["vlan"])
            ctx.bus.event("info", "network", f"Port group {name} removed", f"net:{name}", name, req.username)
            return {"message": f"Port group {name} removed"}
        t = ctx.tasks.submit("network.portgroup.delete", f"Remove port group {name}", run, req.username,
                             f"net:{name}", name, "network")
        return json_response(t, 202)

    r.add("GET", "/api/v1/network/vswitches", vswitches, "network.view")
    r.add("GET", "/api/v1/network/portgroups", portgroups, "network.view")
    r.add("POST", "/api/v1/network/portgroups", portgroup_create, "network.manage")
    r.add("DELETE", "/api/v1/network/portgroups/{name}", portgroup_delete, "network.manage")
    r.add("GET", "/api/v1/network/host", host_net, "network.view")
    r.add("POST", "/api/v1/network/host/changes", apply, "network.manage")
    r.add("GET", "/api/v1/network/host/pending", pending, "network.view")
    r.add("POST", "/api/v1/network/host/pending/confirm", confirm, "network.manage")
    r.add("POST", "/api/v1/network/host/pending/rollback", rollback, "network.manage")
    @handler
    async def bridge_retry(req: Request) -> Response:
        ctx.audit(req, "network.bridge.retry", "management bridge", "submitted")
        t = ctx.tasks.submit("network.bridge", "Move the management interface onto the VM bridge",
                             lambda tc: ctx.agent.call("network.bridge_retry", timeout=420), req.username,
                             "network:bridge", "management bridge", "network")
        return json_response(t, 202)

    r.add("POST", "/api/v1/network/host/vm-bridge/retry", bridge_retry, "network.manage")
    r.add("GET", "/api/v1/network/virtual", vnets, "network.view")
    r.add("POST", "/api/v1/network/virtual", vnet_create, "network.manage")
    r.add("POST", "/api/v1/network/virtual/{name}/{action}", vnet_action, "network.manage")
