"""Host administration: maintenance mode, VM autostart configuration, alarms, remote syslog, SNMP, power
policy, lockdown mode, account lockout policy and support bundles (ESXi Host > Manage / Monitor)."""
from __future__ import annotations

import json
import os
import re
import time

from ..common import config, validate as v
from ..host import hardware
from ..hv import autostart as hvauto
from ..hv import domxml
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, json_response

RE_BUNDLE = re.compile(r"^lsxve-support-[A-Za-z0-9._-]{1,120}\.tar\.gz$")


def _write_json(path, data: dict, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def register(ctx: Ctx) -> None:
    r = ctx.router
    hv = ctx.hv
    host = hardware.hostname

    def event(req: Request, msg: str, sev: str = "info") -> None:
        ctx.bus.event(sev, "host", msg, "host", host(), req.username)

    # -- maintenance mode -------------------------------------------------
    @handler
    async def maint_get(req: Request) -> Response:
        m = ctx.maintenance()
        running = []
        try:
            running = [{"uuid": x["uuid"], "name": x["name"]} for x in await ctx.run(hv.list_vms)
                       if x["state"] in ("running", "paused")]
        except Exception:  # noqa: BLE001
            pass
        return json_response({**m, "enabled": bool(m.get("enabled")), "running_vms": running})

    @handler
    async def maint_put(req: Request) -> Response:
        b = req.json()
        enable = v.boolean(v.require(b, "enabled"), "enabled")
        evacuate = v.boolean(b.get("shutdown_vms", False), "shutdown_vms")
        user = req.username
        if not enable:
            _write_json(config.MAINTENANCE_FILE, {"enabled": False, "changed": int(time.time()), "by": user})
            ctx.audit(req, "host.maintenance.exit", host())
            event(req, "Host exited maintenance mode")
            ctx.bus.broadcast("maintenance", {"enabled": False})
            return json_response({"enabled": False})
        vms = await ctx.run(hv.list_vms)
        running = [x for x in vms if x["state"] in ("running", "paused")]
        if running and not evacuate:
            raise HTTPError(409, f"{len(running)} virtual machine(s) are powered on. Power them off first or choose "
                                 "to shut them down.", "vms_running")
        ctx.audit(req, "host.maintenance.enter", host(), shutdown_vms=evacuate)

        def run(tc):
            # block new power-ons first, then shut the guests down (no vMotion on a standalone host)
            _write_json(config.MAINTENANCE_FILE, {"enabled": True, "changed": int(time.time()), "by": user,
                                                  "entering": bool(running)})
            for i, vm in enumerate(running):
                tc.progress(int(90 * i / max(1, len(running))), f"Shutting down {vm['name']}")
                try:
                    if vm["state"] == "paused":
                        hv.power(vm["uuid"], "poweroff")
                        continue
                    hv.power(vm["uuid"], "shutdown")
                    if not hv.wait_state(vm["uuid"], ("shutoff",), 180):
                        hv.power(vm["uuid"], "poweroff")
                except Exception as e:  # noqa: BLE001
                    ctx.bus.event("warning", "vm", f"Maintenance mode: shutting down {vm['name']} failed: {e}",
                                  vm["uuid"], vm["name"], user)
            _write_json(config.MAINTENANCE_FILE, {"enabled": True, "changed": int(time.time()), "by": user})
            ctx.bus.event("warning", "host", "Host entered maintenance mode", "host", host(), user)
            ctx.bus.broadcast("maintenance", {"enabled": True})
            return {"message": "The host is in maintenance mode"}
        t = ctx.tasks.submit("host.maintenance", "Enter maintenance mode", run, user, "host", host(), "host")
        return json_response(t, 202)

    # -- VM autostart configuration --------------------------------------
    @handler
    async def auto_get(req: Request) -> Response:
        vms = []
        try:
            vms = await ctx.run(hv.autostart_list)
        except Exception:  # noqa: BLE001
            pass
        return json_response({"settings": hvauto.settings(), "vms": vms, "stop_actions": list(domxml.STOP_ACTIONS)})

    @handler
    async def auto_put(req: Request) -> Response:
        b = req.json()
        s = hvauto.settings()
        if "enabled" in b:
            s["enabled"] = v.boolean(b["enabled"], "enabled")
        for k in ("start_delay", "stop_delay"):
            if k in b:
                s[k] = v.integer(b[k], k, 0, 3600)
        if "stop_action" in b:
            s["stop_action"] = v.choice(b["stop_action"], "stop_action", domxml.STOP_ACTIONS)
        if "wait_heartbeat" in b:
            s["wait_heartbeat"] = v.boolean(b["wait_heartbeat"], "wait_heartbeat")
        _write_json(config.AUTOSTART_FILE, s, 0o644)
        # per-VM order in one call: [{"uuid", "enabled", "order"}]
        for i, item in enumerate(v.list_of(b.get("vms", []), "vms", lambda x, f: x, 512)):
            if not isinstance(item, dict):
                raise v.ValidationError(f"vms[{i}]", "must be an object")
            uuid = v.uuid(item.get("uuid"))
            opts = {}
            if "order" in item:
                opts["order"] = v.integer(item["order"] or 0, f"vms[{i}].order", 0, 999)
            en = v.boolean(item["enabled"], f"vms[{i}].enabled") if "enabled" in item else None
            await ctx.run(hv.set_autostart, uuid, en, **opts)
        ctx.audit(req, "host.autostart", host(), **s)
        event(req, "Virtual machine autostart configuration changed")
        return await auto_get(req)

    # -- alarms ------------------------------------------------------------
    @handler
    async def alarms_get(req: Request) -> Response:
        return json_response({"alarms": ctx.alarms.list(), "settings": ctx.alarms.settings()})

    @handler
    async def alarm_ack(req: Request) -> Response:
        key = v.string(req.params["id"], "id", 3, 200)
        try:
            a = ctx.alarms.acknowledge(key, req.username)
        except KeyError:
            raise HTTPError(404, "alarm not found or already cleared", "not_found")
        ctx.audit(req, "alarm.acknowledge", key)
        return json_response(a)

    @handler
    async def alarm_settings(req: Request) -> Response:
        s = ctx.alarms.save_settings(req.json())
        ctx.audit(req, "alarm.settings", host())
        await ctx.run(ctx.alarms.evaluate)
        return json_response({"alarms": ctx.alarms.list(), "settings": s})

    # -- agent-backed host settings ----------------------------------------
    def agent_get(op: str):
        @handler
        async def h(req: Request) -> Response:
            return json_response(await ctx.run(ctx.agent.call, op, timeout=60))
        return h

    def agent_set(op: str, action: str, message: str, build, timeout: int = 180):
        @handler
        async def h(req: Request) -> Response:
            args = build(req.json())
            safe = {k: ("***" if k == "community" else val) for k, val in args.items()}
            try:
                res = await ctx.run(ctx.agent.call, op, timeout=timeout, **args)
            except Exception:
                ctx.audit(req, action, host(), "failure", **safe)
                raise
            ctx.audit(req, action, host(), **safe)
            event(req, message.format(**args), "warning" if action == "host.lockdown" else "info")
            return json_response(res)
        return h

    def syslog_args(b):
        t = b.get("targets", [])
        if not isinstance(t, list):
            raise v.ValidationError("targets", "must be a list")
        return {"targets": t}

    def snmp_args(b):
        return {"enabled": v.boolean(v.require(b, "enabled"), "enabled"), "community": b.get("community") or None,
                "sources": b.get("sources") or [], "location": b.get("location") or "",
                "contact": b.get("contact") or ""}

    # -- account lockout (local web UI accounts) ---------------------------
    @handler
    async def policy_get(req: Request) -> Response:
        return json_response({k: ctx.cfg[k] for k in ("login_max_failures", "login_lockout_seconds",
                                                      "session_idle_minutes") if k in ctx.cfg})

    @handler
    async def policy_put(req: Request) -> Response:
        b = req.json()
        new = {}
        if "login_max_failures" in b:
            new["login_max_failures"] = v.integer(b["login_max_failures"], "login_max_failures", 0, 100)
        if "login_lockout_seconds" in b:
            new["login_lockout_seconds"] = v.integer(b["login_lockout_seconds"], "login_lockout_seconds", 0, 86400)
        if not new:
            raise v.ValidationError("body", "nothing to change")

        def save():
            try:
                with open(config.CONFIG_FILE, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except FileNotFoundError:
                data = {}
            data.update(new)
            _write_json(config.CONFIG_FILE, data, 0o640)
        await ctx.run(save)
        ctx.cfg.update(new)       # AuthService shares this dict: applies without a restart
        ctx.audit(req, "security.policy", host(), **new)
        event(req, "Account lockout policy changed")
        return await policy_get(req)

    # -- support bundle -------------------------------------------------------
    def bundles():
        d = config.SUPPORT_DIR
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            if RE_BUNDLE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size, "created": st.st_mtime})
        return sorted(out, key=lambda x: -x["created"])

    @handler
    async def bundle_list(req: Request) -> Response:
        return json_response(await ctx.run(bundles))

    @handler
    async def bundle_create(req: Request) -> Response:
        ctx.audit(req, "host.support_bundle", host())

        def run(tc):
            tc.progress(10, "Collecting logs and configuration")
            res = ctx.agent.call("host.support_bundle", timeout=900)
            return {"message": f"Support bundle {res['name']} created", **res}
        t = ctx.tasks.submit("host.support_bundle", "Generate support bundle", run, req.username, "host:bundle",
                             host(), "host")
        return json_response(t, 202)

    @handler
    async def bundle_download(req: Request) -> Response:
        name = v.string(req.params["name"], "name", 1, 160, RE_BUNDLE)
        p = config.SUPPORT_DIR / name
        if not p.is_file():
            raise HTTPError(404, "support bundle not found", "not_found")
        ctx.audit(req, "host.support_bundle.download", name)
        return Response(200, b"", {"Content-Type": "application/gzip",
                                   "Content-Disposition": f'attachment; filename="{name}"',
                                   "Cache-Control": "no-store"}, file_path=p)

    r.add("GET", "/api/v1/host/maintenance", maint_get, "host.view")
    r.add("PUT", "/api/v1/host/maintenance", maint_put, "host.config")
    r.add("GET", "/api/v1/host/autostart", auto_get, "host.view")
    r.add("PUT", "/api/v1/host/autostart", auto_put, "host.config")
    r.add("GET", "/api/v1/alarms", alarms_get, "host.view")
    r.add("POST", "/api/v1/alarms/{id}/ack", alarm_ack, "host.config")
    r.add("PUT", "/api/v1/alarms/settings", alarm_settings, "host.config")
    r.add("GET", "/api/v1/host/syslog", agent_get("host.get_syslog"), "host.view")
    r.add("PUT", "/api/v1/host/syslog", agent_set("host.set_syslog", "host.syslog", "Remote syslog configuration changed",
                                                  syslog_args), "host.config")
    r.add("GET", "/api/v1/host/snmp", agent_get("host.get_snmp"), "host.view")
    r.add("PUT", "/api/v1/host/snmp", agent_set("host.set_snmp", "host.snmp", "SNMP agent configuration changed",
                                                snmp_args), "host.config")
    r.add("GET", "/api/v1/host/power-policy", agent_get("host.get_power_policy"), "host.view")
    r.add("PUT", "/api/v1/host/power-policy", agent_set("host.set_power_policy", "host.power_policy",
                                                        "Power policy set to {policy}",
                                                        lambda b: {"policy": v.string(v.require(b, "policy"), "policy",
                                                                                      1, 40)}), "host.config")
    r.add("GET", "/api/v1/host/lockdown", agent_get("host.get_lockdown"), "host.view")
    r.add("PUT", "/api/v1/host/lockdown", agent_set("host.set_lockdown", "host.lockdown", "Lockdown mode set to {mode}",
                                                    lambda b: {"mode": v.choice(v.require(b, "mode"), "mode",
                                                                                ("disabled", "normal", "strict"))}),
          "security.manage")
    r.add("GET", "/api/v1/host/security-policy", policy_get, "host.view")
    r.add("PUT", "/api/v1/host/security-policy", policy_put, "security.manage")
    r.add("GET", "/api/v1/host/support-info", agent_get("host.support_info"), "host.view")
    r.add("GET", "/api/v1/host/support-bundles", bundle_list, "host.config")
    r.add("POST", "/api/v1/host/support-bundles", bundle_create, "host.config")
    r.add("GET", "/api/v1/host/support-bundles/{name}", bundle_download, "host.config")
