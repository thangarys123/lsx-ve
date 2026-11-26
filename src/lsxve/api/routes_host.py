"""Host summary, monitoring, configuration, security and log endpoints."""
from __future__ import annotations

import json
import os
import re
import shutil
import time

from .. import PRODUCT, __version__
from ..agent.client import AgentError
from ..common import config, proc, validate as v
from ..host import hardware
from . import tls
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, json_response

RE_UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,80}$")


def register(ctx: Ctx) -> None:
    r = ctx.router
    hv = ctx.hv

    def _storage_totals() -> dict:
        try:
            pools = hv.list_pools()
            cap = sum(p["capacity"] for p in pools if p["active"])
            used = sum(p["allocation"] for p in pools if p["active"])
            return {"capacity": cap, "used": used, "free": cap - used, "datastores": len(pools)}
        except Exception:
            try:
                du = shutil.disk_usage("/var/lib/libvirt/images" if os.path.exists("/var/lib/libvirt/images") else "/")
                return {"capacity": du.total, "used": du.used, "free": du.free, "datastores": 0}
            except OSError:
                return {"capacity": 0, "used": 0, "free": 0, "datastores": 0}

    def _summary() -> dict:
        s = hardware.summary()
        hvs = hv.status()
        counts = {"running": 0, "stopped": 0, "paused": 0, "templates": 0}
        if hvs.get("available"):
            try:
                counts = hv.counts()
            except Exception:
                pass
        sensors = hardware.sensors()
        health = "unknown" if not sensors else ("critical" if any(x["status"] == "critical" for x in sensors) else
                                                 "warning" if any(x["status"] == "warning" for x in sensors) else "normal")
        last = ctx.sampler.history[-1] if ctx.sampler.history else None
        return {**s, "product": PRODUCT, "version": __version__, "management": hardware.management_addresses(),
                "hypervisor": hvs, "vms": counts, "storage": _storage_totals(), "health": health,
                "sensors_count": len(sensors), "current": last}

    @handler
    async def public_info(req: Request) -> Response:
        return json_response({"product": PRODUCT, "version": __version__, "hostname": hardware.hostname()})

    @handler
    async def summary(req: Request) -> Response:
        return json_response(await ctx.run(_summary))

    @handler
    async def hw(req: Request) -> Response:
        def collect():
            return {"dmi": hardware.dmi(), "cpu": hardware.cpu_info(), "memory": hardware.memory_info(),
                    "pci": hardware.pci_devices(), "sensors": hardware.sensors(), "nics": hardware.physical_nics(),
                    "firmware": hardware.firmware_mode(), "secure_boot": hardware.secure_boot(),
                    "iommu": hardware.iommu_enabled(), "kvm": hardware.kvm_status()}
        return json_response(await ctx.run(collect))

    @handler
    async def metrics(req: Request) -> Response:
        since = float(req.q("since", "0") or 0)
        return json_response({"interval": ctx.cfg["metrics_interval"], "samples": ctx.sampler.series(since),
                              "vms": ctx.last_vm_stats})

    # -- configuration --------------------------------------------------
    @handler
    async def host_config(req: Request) -> Response:
        def collect():
            out = {"hostname": hardware.hostname(), "agent": True, "errors": {}}
            for key, op in (("time", "host.get_time"), ("dns", "host.get_dns"), ("ssh", "host.get_ssh")):
                try:
                    out[key] = ctx.agent.call(op, timeout=20)
                except AgentError as e:
                    out[key] = None
                    if e.kind == "unavailable":      # agent socket not reachable at all
                        out["agent"] = False
                        out["agent_error"] = e.message
                    else:                            # a single read failed; the agent itself works
                        out["errors"][key] = e.message
                except Exception as e:  # noqa
                    out[key] = None
                    out["errors"][key] = str(e)
            return out
        return json_response(await ctx.run(collect))

    @handler
    async def timezones(req: Request) -> Response:
        return json_response(await ctx.run(ctx.agent.call, "host.list_timezones"))

    def change(op: str, action: str, perm_event: str, build):
        @handler
        async def h(req: Request) -> Response:
            args = build(req.json())
            try:
                res = await ctx.run(ctx.agent.call, op, **args)
            except Exception:
                ctx.audit(req, action, "", "failure", **{k: val for k, val in args.items() if k != "password"})
                raise
            ctx.audit(req, action, "", **{k: val for k, val in args.items() if k != "password"})
            ctx.bus.event("info", "host", perm_event.format(**args) if "{" in perm_event else perm_event, "host",
                          hardware.hostname(), req.username)
            return json_response(res)
        return h

    r.add("GET", "/api/v1/public/info", public_info, auth=False)
    r.add("GET", "/api/v1/host/summary", summary, "host.view")
    r.add("GET", "/api/v1/host/hardware", hw, "host.view")
    r.add("GET", "/api/v1/host/metrics", metrics, "host.view")
    r.add("GET", "/api/v1/host/config", host_config, "host.view")
    r.add("GET", "/api/v1/host/timezones", timezones, "host.view")
    r.add("PUT", "/api/v1/host/hostname", change("host.set_hostname", "host.hostname", "Host name changed to {hostname}",
                                                 lambda b: {"hostname": v.hostname(v.require(b, "hostname"))}),
          "host.config")
    r.add("PUT", "/api/v1/host/dns", change("host.set_dns", "host.dns", "DNS configuration changed",
                                            lambda b: {"servers": v.list_of(b.get("servers", []), "servers",
                                                                            lambda x, f: v.ip_address(x, f), 4),
                                                       "search": v.list_of(b.get("search", []), "search", v.hostname, 6)}),
          "host.config")
    r.add("PUT", "/api/v1/host/ntp", change("host.set_ntp", "host.ntp", "NTP configuration changed",
                                            lambda b: {"servers": v.list_of(b.get("servers", []), "servers",
                                                                            v.host_or_ip, 8),
                                                       "enabled": v.boolean(b.get("enabled", True), "enabled")}),
          "host.config")
    r.add("PUT", "/api/v1/host/timezone", change("host.set_timezone", "host.timezone", "Time zone set to {tz}",
                                                 lambda b: {"tz": v.string(v.require(b, "timezone"), "timezone", 1, 64,
                                                                           v.RE_TZ)}), "host.config")
    r.add("PUT", "/api/v1/host/ssh", change("host.set_ssh", "host.ssh", "SSH configuration changed",
                                            lambda b: {"enabled": v.boolean(v.require(b, "enabled"), "enabled"),
                                                       "permit_root_login": v.choice(
                                                           b.get("permit_root_login", "prohibit-password"),
                                                           "permit_root_login", ("yes", "no", "prohibit-password"))}),
          "host.config")
    r.add("POST", "/api/v1/host/firewall", change("firewall.change", "host.firewall", "Firewall rule {op}: {value}",
                                                  lambda b: {"op": v.choice(b.get("op"), "op", ("add", "remove")),
                                                             "kind": v.choice(b.get("kind"), "kind", ("service", "port")),
                                                             "value": v.string(b.get("value"), "value", 1, 64)}),
          "host.config")
    r.add("PUT", "/api/v1/host/root-password", change("host.set_root_password", "host.root_password",
                                                      "Host root password changed",
                                                      lambda b: {"password": v.password(v.require(b, "password"))}),
          "security.manage")

    @handler
    async def firewall(req: Request) -> Response:
        return json_response(await ctx.run(ctx.agent.call, "firewall.status"))

    @handler
    async def services(req: Request) -> Response:
        return json_response(await ctx.run(ctx.agent.call, "host.list_services"))

    @handler
    async def service_action(req: Request) -> Response:
        name = v.string(req.params["name"], "name", 1, 40, RE_UNIT)
        action = v.choice(req.params["action"], "action", ("start", "stop", "restart", "enable", "disable"))
        res = await ctx.run(ctx.agent.call, "host.service_action", name=name, action=action)
        ctx.audit(req, "host.service", name, action=action)
        ctx.bus.event("info", "host", f"Service {name}: {action}", "host", hardware.hostname(), req.username)
        return json_response(res)

    @handler
    async def power(req: Request) -> Response:
        b = req.json()
        action = v.choice(b.get("action"), "action", ("reboot", "poweroff"))
        if b.get("confirm") != hardware.hostname():
            raise v.ValidationError("confirm", "type the host name to confirm")
        reason = v.text(b.get("reason", ""), "reason", 200)
        ctx.audit(req, f"host.{action}", hardware.hostname(), reason=reason)
        ctx.bus.event("warning", "host", f"Host {'reboot' if action == 'reboot' else 'shutdown'} requested"
                      + (f": {reason}" if reason else ""), "host", hardware.hostname(), req.username)
        return json_response(await ctx.run(ctx.agent.call, "host.power", action=action))

    # -- certificates ---------------------------------------------------
    @handler
    async def cert_info(req: Request) -> Response:
        return json_response(await ctx.run(tls.info))

    @handler
    async def cert_install(req: Request) -> Response:
        b = req.json()
        cert = v.text(v.require(b, "certificate"), "certificate", 64 * 1024)
        key = v.text(v.require(b, "private_key"), "private_key", 32 * 1024)
        if ctx.ssl_ctx is None:
            raise HTTPError(409, "TLS is not enabled on this listener")
        try:
            res = await ctx.run(tls.install, cert, key, ctx.ssl_ctx)
        except ValueError as e:
            ctx.audit(req, "host.certificate.install", "", "failure", error=str(e))
            raise HTTPError(422, str(e), "invalid_certificate")
        ctx.audit(req, "host.certificate.install", res.get("subject", ""))
        ctx.bus.event("info", "host", "Custom TLS certificate installed", "host", hardware.hostname(), req.username)
        return json_response(res)

    @handler
    async def cert_regen(req: Request) -> Response:
        if ctx.ssl_ctx is None:
            raise HTTPError(409, "TLS is not enabled on this listener")
        mg = hardware.management_addresses()
        ips = [a.split("/")[0] for a in mg["ipv4"] + mg["ipv6"]]
        await ctx.run(tls.generate_self_signed, hardware.hostname(), ips)
        ctx.ssl_ctx.load_cert_chain(str(tls.CERT), str(tls.KEY))
        ctx.audit(req, "host.certificate.regenerate", hardware.hostname())
        ctx.bus.event("info", "host", "Self-signed TLS certificate regenerated", "host", hardware.hostname(), req.username)
        return json_response(await ctx.run(tls.info))

    # -- logs -----------------------------------------------------------
    @handler
    async def logs(req: Request) -> Response:
        args = ["/usr/bin/journalctl", "--no-pager", "-o", "json", "-q"]
        lines = v.integer(req.q("lines", "200"), "lines", 1, 2000)
        args += ["-n", str(lines)]
        unit = req.q("unit")
        if unit:
            args += ["-u", v.string(unit, "unit", 1, 80, RE_UNIT)]
        prio = req.q("priority")
        if prio:
            args += ["-p", v.choice(prio, "priority", ("emerg", "alert", "crit", "err", "warning", "notice", "info",
                                                      "debug"))]
        grep = req.q("grep")
        if grep:
            args += ["--case-sensitive=false", "-g", v.string(grep, "grep", 1, 100)]
        boot = req.q("boot")
        if boot == "current":
            args += ["-b"]
        if not proc.which("/usr/bin/journalctl"):
            raise HTTPError(503, "journalctl is not available on this system")

        def run():
            cp = proc.run(args, timeout=30, check=False)
            out = []
            for line in cp.stdout.splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                msg = e.get("MESSAGE")
                if isinstance(msg, list):
                    msg = bytes(msg).decode("utf-8", "replace")
                out.append({"ts": int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6, "priority": int(e.get("PRIORITY", 6)),
                            "unit": e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER", ""),
                            "ident": e.get("SYSLOG_IDENTIFIER", ""), "pid": e.get("_PID"), "message": msg})
            return out
        return json_response(await ctx.run(run))

    r.add("GET", "/api/v1/host/firewall", firewall, "host.view")
    r.add("GET", "/api/v1/host/services", services, "host.view")
    r.add("POST", "/api/v1/host/services/{name}/{action}", service_action, "host.config")
    r.add("POST", "/api/v1/host/power", power, "host.power")
    r.add("GET", "/api/v1/host/certificate", cert_info, "host.view")
    r.add("POST", "/api/v1/host/certificate", cert_install, "host.config", max_body=128 * 1024)
    r.add("POST", "/api/v1/host/certificate/regenerate", cert_regen, "host.config")
    r.add("GET", "/api/v1/host/logs", logs, "logs.view")
