"""Event bus, persistent events and the task manager.

* Events are persisted (``events`` table) and pushed to WebSocket subscribers.
* Tasks are long-running operations executed in a worker thread pool. They report
  progress, are persisted (``tasks`` table) and are pushed live to clients.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, Optional, Set

from ..common import log
from ..common.db import Database

LOG = log.get("lsxve.tasks")


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.subscribers: Set[asyncio.Queue] = set()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _deliver(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # slow consumer - drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def broadcast(self, msg_type: str, data: Any) -> None:
        """Thread-safe push of a transient message to all WS clients."""
        msg = {"type": msg_type, "data": data}
        if self.loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._deliver(msg)
        else:
            self.loop.call_soon_threadsafe(self._deliver, msg)

    def event(self, severity: str, category: str, message: str, target: str = "", target_name: str = "",
              username: str = "") -> None:
        ts = time.time()
        cur = self.db.execute(
            "INSERT INTO events(ts, severity, category, message, target, target_name, username) VALUES(?,?,?,?,?,?,?)",
            (ts, severity, category, message, target, target_name, username))
        self.broadcast("event", {"id": cur.lastrowid, "ts": ts, "severity": severity, "category": category,
                                 "message": message, "target": target, "target_name": target_name,
                                 "username": username})


class TaskContext:
    """Handed to task functions so they can report progress."""

    def __init__(self, mgr: "TaskManager", task_id: str):
        self.mgr = mgr
        self.id = task_id

    def progress(self, pct: int, message: str = "") -> None:
        self.mgr._update(self.id, progress=max(0, min(100, int(pct))), message=message)


class TaskManager:
    def __init__(self, db: Database, bus: EventBus, workers: int = 8):
        self.db = db
        self.bus = bus
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task")
        self._lock = threading.Lock()
        self._busy_targets: Dict[str, str] = {}
        # tasks left "running" by a previous process are failed
        db.execute("UPDATE tasks SET state='error', message='Interrupted by service restart', finished=? "
                   "WHERE state IN ('queued','running')", (time.time(),))

    def _row(self, task_id: str) -> Optional[dict]:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if r and r.get("result"):
            try:
                r["result"] = json.loads(r["result"])
            except ValueError:
                pass
        return r

    def _update(self, task_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        row = self._row(task_id)
        if row:
            self.bus.broadcast("task", row)

    def submit(self, kind: str, description: str, fn: Callable[[TaskContext], Any], username: str,
               target: str = "", target_name: str = "", category: str = "host",
               exclusive: bool = True) -> dict:
        """Queue ``fn`` for execution. If ``exclusive``, only one task per target may run."""
        task_id = uuid.uuid4().hex[:16]
        with self._lock:
            if exclusive and target and target in self._busy_targets:
                from .httpd import HTTPError
                raise HTTPError(409, f"Another task is already running on {target_name or target}", "busy")
            if exclusive and target:
                self._busy_targets[target] = task_id
        self.db.execute(
            "INSERT INTO tasks(id, kind, description, target, target_name, username, state, progress, created)"
            " VALUES(?,?,?,?,?,?,'queued',0,?)", (task_id, kind, description, target, target_name, username, time.time()))
        self.bus.broadcast("task", self._row(task_id))

        def runner():
            self._update(task_id, state="running", started=time.time())
            ctx = TaskContext(self, task_id)
            try:
                result = fn(ctx)
                self._update(task_id, state="success", progress=100, finished=time.time(),
                             message=(result.get("message", "") if isinstance(result, dict) else "") or "Completed",
                             result=json.dumps(result, default=str) if result is not None else None)
                self.bus.event("info", category, f"{description}: completed", target, target_name, username)
            except Exception as exc:  # report every failure to the user
                msg = _friendly_error(exc)
                LOG.warning("task %s (%s) failed: %s\n%s", task_id, kind, msg, traceback.format_exc())
                self._update(task_id, state="error", finished=time.time(), message=msg)
                self.bus.event("error", category, f"{description}: failed - {msg}", target, target_name, username)
            finally:
                with self._lock:
                    if self._busy_targets.get(target) == task_id:
                        del self._busy_targets[target]

        self.pool.submit(runner)
        return self._row(task_id)

    def list(self, limit: int = 100, state: Optional[str] = None) -> list:
        if state == "active":
            rows = self.db.all("SELECT * FROM tasks WHERE state IN ('queued','running') ORDER BY created DESC LIMIT ?",
                               (limit,))
        elif state:
            rows = self.db.all("SELECT * FROM tasks WHERE state=? ORDER BY created DESC LIMIT ?", (state, limit))
        else:
            rows = self.db.all("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))
        for r in rows:
            if r.get("result"):
                try:
                    r["result"] = json.loads(r["result"])
                except ValueError:
                    pass
        return rows

    def get(self, task_id: str) -> Optional[dict]:
        return self._row(task_id)


def _friendly_error(exc: Exception) -> str:
    try:
        import libvirt  # type: ignore
        if isinstance(exc, libvirt.libvirtError):
            return exc.get_error_message() or str(exc)
    except ImportError:
        pass
    return str(exc) or exc.__class__.__name__
