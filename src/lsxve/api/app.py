"""lsxve-api: the LSX-VE management API and web console server."""
from __future__ import annotations

import asyncio
import collections
import json
import os
import secrets
import signal
import sys
import time

from .. import PRODUCT, __version__
from ..agent.client import AgentClient
from ..common import config, db as dbm, log, security
from ..host.metrics import HostSampler
from ..hv import driver as hvdrv
from . import alarms, routes_admin, routes_ct, routes_host, routes_network, routes_security, routes_storage, routes_vm, tls
from .auth import AuthService
from .context import Ctx
from .events import EventBus, TaskManager
from .httpd import HTTPServer, Router, redirect_server

LOG = log.setup("lsxve.api")

FIRSTBOOT_ADMIN = config.ETC_DIR / "firstboot-admin.json"

EVENT_SEVERITY = {"crashed": "error", "stopped": "info", "started": "info", "suspended": "info", "resumed": "info",
                  "defined": "info", "undefined": "warning", "shutdown": "info", "pmsuspended": "info"}
EVENT_TEXT = {"started": "powered on", "stopped": "powered off", "shutdown": "shut down", "suspended": "paused",
              "resumed": "resumed", "crashed": "crashed", "defined": "configuration saved", "undefined": "unregistered",
              "pmsuspended": "suspended by guest"}


def bootstrap_admin(db: dbm.Database) -> None:
    """Import the administrator account created by the installer (one-shot)."""
    if FIRSTBOOT_ADMIN.exists():
        try:
            data = json.loads(FIRSTBOOT_ADMIN.read_text())
            username = data["username"]
            h = data["password_hash"]
            if not (h.startswith("scrypt$") or h.startswith(("$6$", "$5$"))):
                raise ValueError("unsupported password hash format")
            if not db.one("SELECT id FROM users WHERE username=?", (username,)):
                dbm.create_user(db, username, h, "Administrator", data.get("full_name", "Administrator"))
                db.audit("installer", "local", "security.user.create", username, "success", {"role": "Administrator"})
                LOG.info("imported installer administrator account %s", username)
            FIRSTBOOT_ADMIN.unlink()
        except (OSError, ValueError, KeyError) as e:
            LOG.error("cannot import installer administrator: %s", e)
    if not db.one("SELECT id FROM users LIMIT 1"):
        pw = os.environ.get("LSXVE_DEV_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        dbm.create_user(db, "admin", security.hash_password(pw), "Administrator", "Administrator", must_change=True)
        msg = "No accounts existed: created 'admin' (password must be changed at first login)."
        if config.DEV:
            LOG.warning("%s password=%s", msg, pw)
        else:
            (config.ETC_DIR / "initial-admin-password").write_text(pw + "\n")
            os.chmod(config.ETC_DIR / "initial-admin-password", 0o600)
            LOG.warning("%s Password stored in %s", msg, config.ETC_DIR / "initial-admin-password")


def build_ctx(cfg: dict) -> Ctx:
    db = dbm.Database(config.DB_FILE)
    bus = EventBus(db)
    tasks = TaskManager(db, bus)
    auth = AuthService(db, cfg)

    def on_dom(ev: dict) -> None:
        bus.broadcast("vm", ev)
        name = ev["event"]
        if name in ("defined", "agent"):
            return  # reconfigurations are logged by their tasks; guest agent changes only refresh clients
        bus.event(EVENT_SEVERITY.get(name, "info"), "vm", f"Virtual machine {ev['name']} {EVENT_TEXT.get(name, name)}",
                  ev["uuid"], ev["name"])

    hv = hvdrv.Hypervisor(cfg["libvirt_uri"], on_dom)
    return Ctx(cfg=cfg, db=db, bus=bus, tasks=tasks, auth=auth, hv=hv, agent=AgentClient(),
               sampler=HostSampler(cfg["metrics_history"]), router=Router())


async def metrics_loop(ctx: Ctx) -> None:
    interval = max(1, ctx.cfg["metrics_interval"])
    while True:
        await asyncio.sleep(interval)
        try:
            s = ctx.sampler.sample()
            vms = {}
            if hvdrv.HAVE_LIBVIRT:
                try:
                    vms = await ctx.run(ctx.hv.vm_stats)
                except Exception:
                    vms = {}
            ctx.last_vm_stats = vms
            now = time.time()
            for u, st in vms.items():
                h = ctx.vm_history.get(u)
                if h is None:
                    h = ctx.vm_history[u] = collections.deque(maxlen=ctx.cfg["metrics_history"])
                h.append((round(now, 1), st))
            for u in [u for u, h in ctx.vm_history.items() if u not in vms and h and now - h[-1][0] > 3600]:
                ctx.vm_history.pop(u, None)
            ctx.bus.broadcast("metrics", {"host": s, "vms": vms})
        except Exception:  # pragma: no cover
            LOG.exception("metrics sampling failed")


async def alarm_loop(ctx: Ctx) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await ctx.run(ctx.alarms.evaluate)
        except Exception:  # pragma: no cover
            LOG.exception("alarm evaluation failed")
        await asyncio.sleep(30)


async def housekeeping(ctx: Ctx) -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(60)
        try:
            ctx.auth.purge_expired()
            now = time.time()
            for k in [k for k, t in ctx.console_tickets.items() if t["expires"] < now]:
                ctx.console_tickets.pop(k, None)
            if now - last_prune > 86400:
                ctx.db.prune()
                last_prune = now
        except Exception:  # pragma: no cover
            LOG.exception("housekeeping failed")


async def default_network_init(ctx: Ctx) -> None:
    """Point libvirt network "default" at the management bridge once lsxve-agent has created it.

    There is no NAT network: VMs on "default" are on the physical LAN and use its DHCP server."""
    bridge = ctx.cfg.get("management_bridge") or ""
    if not bridge:
        return
    warned = False
    for _ in range(24 * 60):  # retry every minute for a day (e.g. a running VM still on the old NAT network)
        if os.path.isdir(f"/sys/class/net/{bridge}/bridge"):
            try:
                res = await ctx.run(ctx.hv.ensure_default_network, bridge)
                if res in ("created", "replaced"):
                    ctx.bus.event("info", "network", f'Virtual network "default" is bridged to {bridge} '
                                                     "(VMs get addresses from the physical network)")
                if not res.startswith("waiting"):
                    return
                if not warned:
                    LOG.warning("default network: %s", res)
                    warned = True
            except Exception as e:  # noqa: BLE001
                if not warned:
                    LOG.warning("default network unavailable: %s", e)
                    warned = True
        await asyncio.sleep(60 if warned else 10)


async def hypervisor_init(ctx: Ctx) -> None:
    """Make sure the built-in datastores and default virtual network exist."""
    if not hvdrv.HAVE_LIBVIRT:
        LOG.warning("libvirt bindings missing - hypervisor features disabled")
        return
    announced = False
    for attempt in range(120):
        try:
            await ctx.run(ctx.hv.conn)
            ds1 = config.DATASTORE_ROOT / "datastore1"
            if ds1.is_dir() and (os.path.ismount(ds1) or (ds1 / ".lsxve-datastore").is_file()):
                # local datastore created by the LSX-VE installer (own partition on older installs,
                # a directory on the root partition with the fixed disk layout)
                await ctx.run(ctx.hv.ensure_default_pool, str(ds1), "datastore1")
            else:
                await ctx.run(ctx.hv.ensure_default_pool)
            await ctx.run(ctx.hv.ensure_iso_pool, str(config.ISO_DIR))
            asyncio.ensure_future(default_network_init(ctx))
            caps = await ctx.run(ctx.hv.capabilities)
            ctx.bus.event("info", "host", f"Hypervisor ready (libvirt {caps['libvirt_version']}, QEMU "
                                          f"{caps['qemu_version']}, {'KVM accelerated' if caps['accelerated'] else 'software emulation - KVM unavailable'})")
            return
        except Exception as e:
            if not announced:
                LOG.warning("hypervisor not ready yet: %s", e)
                announced = True
            await asyncio.sleep(5)
    ctx.bus.event("error", "host", "Hypervisor did not become available")


async def amain() -> None:
    cfg = config.load()
    config.ensure_dirs()
    if cfg.get("_config_error"):
        LOG.error("configuration error, using defaults: %s", cfg["_config_error"])
    ctx = build_ctx(cfg)
    bootstrap_admin(ctx.db)
    ctx.alarms = alarms.AlarmManager(ctx)
    loop = asyncio.get_running_loop()
    ctx.bus.attach(loop)

    use_tls = not (config.DEV and os.environ.get("LSXVE_DEV_TLS") != "1")
    if use_tls:
        await ctx.run(tls.ensure)
        ctx.ssl_ctx = tls.make_context()
    routes_security.register(ctx, secure_cookie=use_tls)
    routes_host.register(ctx)
    routes_admin.register(ctx)
    routes_vm.register(ctx)
    routes_ct.register(ctx)
    routes_storage.register(ctx)
    routes_network.register(ctx)

    server = HTTPServer(ctx.router, config.WEB_ROOT, ctx.auth.middleware)
    server.hsts = use_tls
    srv = await asyncio.start_server(server.handle_connection, cfg["listen_address"], cfg["https_port"],
                                     ssl=ctx.ssl_ctx, limit=64 * 1024, backlog=256)
    servers = [srv]
    if use_tls and cfg["http_redirect_port"]:
        try:
            servers.append(await asyncio.start_server(
                lambda r, w: redirect_server(r, w, cfg["https_port"]), cfg["listen_address"], cfg["http_redirect_port"]))
        except OSError as e:
            LOG.warning("HTTP redirect listener disabled: %s", e)
    scheme = "https" if use_tls else "http"
    LOG.info("%s %s management API listening on %s://%s:%s", PRODUCT, __version__, scheme, cfg["listen_address"],
             cfg["https_port"])
    ctx.bus.event("info", "host", f"{PRODUCT} management services started")
    bg = [asyncio.ensure_future(metrics_loop(ctx)), asyncio.ensure_future(housekeeping(ctx)),
          asyncio.ensure_future(hypervisor_init(ctx)), asyncio.ensure_future(alarm_loop(ctx))]

    stop = asyncio.Event()
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        for t in bg:
            t.cancel()
        for s in servers:
            s.close()
        LOG.info("shutting down")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
