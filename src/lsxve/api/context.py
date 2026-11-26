"""Shared application context and handler helpers."""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..agent.client import AgentClient, AgentError
from ..common.db import Database
from ..common.validate import ValidationError
from ..host.metrics import HostSampler
from ..hv import driver as hvdrv
from .auth import AuthService
from .events import EventBus, TaskManager
from .httpd import HTTPError, Request, Router


@dataclass
class Ctx:
    cfg: dict
    db: Database
    bus: EventBus
    tasks: TaskManager
    auth: AuthService
    hv: hvdrv.Hypervisor
    agent: AgentClient
    sampler: HostSampler
    router: Router
    ssl_ctx: Optional[ssl.SSLContext] = None
    io: concurrent.futures.ThreadPoolExecutor = field(
        default_factory=lambda: concurrent.futures.ThreadPoolExecutor(16, thread_name_prefix="io"))
    console_tickets: Dict[str, dict] = field(default_factory=dict)
    last_vm_stats: Dict[str, dict] = field(default_factory=dict)
    vm_history: Dict[str, Any] = field(default_factory=dict)     # uuid -> deque of (ts, stats) samples
    alarms: Any = None                                           # api.alarms.AlarmManager

    def maintenance(self) -> dict:
        try:
            import json
            from ..common import config
            return json.loads(config.MAINTENANCE_FILE.read_text())
        except (OSError, ValueError):
            return {"enabled": False}

    def require_not_maintenance(self, what: str = "Powering on virtual machines") -> None:
        if self.maintenance().get("enabled"):
            raise HTTPError(409, f"{what} is not allowed while the host is in maintenance mode", "maintenance")

    async def run(self, fn: Callable, *args, **kwargs) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.io, functools.partial(fn, *args, **kwargs))

    def audit(self, req: Request, action: str, target: str = "", outcome: str = "success", **details) -> None:
        self.db.audit(req.username, req.client_ip, action, target, outcome, details)


def map_error(exc: Exception) -> HTTPError:
    if isinstance(exc, HTTPError):
        return exc
    if isinstance(exc, ValidationError):
        return HTTPError(422, exc.message, "validation", {"field": exc.field})
    if isinstance(exc, hvdrv.NotFound):
        return HTTPError(404, str(exc), "not_found")
    if isinstance(exc, hvdrv.Conflict):
        return HTTPError(409, str(exc), "conflict")
    if isinstance(exc, hvdrv.HypervisorError):
        return HTTPError(503, str(exc), "hypervisor")
    if isinstance(exc, AgentError):
        if exc.kind == "validation":
            return HTTPError(422, exc.message, "validation", {"field": exc.field})
        if exc.kind in ("unavailable", "timeout"):
            return HTTPError(503, exc.message, "agent_unavailable")
        if exc.kind == "not_found":
            return HTTPError(404, exc.message, "not_found")
        if exc.kind == "conflict":
            return HTTPError(409, exc.message, "conflict")
        return HTTPError(500, exc.message, "host_operation_failed")
    if hvdrv.HAVE_LIBVIRT and isinstance(exc, hvdrv.libvirt.libvirtError):
        return HTTPError(500, exc.get_error_message() or str(exc), "libvirt")
    return HTTPError(500, str(exc) or exc.__class__.__name__, "internal")


def handler(fn):
    """Decorator converting domain exceptions into HTTP errors."""
    @functools.wraps(fn)
    async def wrapped(req: Request, *a):
        try:
            return await fn(req, *a)
        except HTTPError:
            raise
        except Exception as exc:  # noqa
            raise map_error(exc)
    return wrapped
