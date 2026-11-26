"""SQLite persistence for users, roles, sessions, tasks, events and audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL,           -- JSON list
    builtin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL REFERENCES roles(name),
    enabled INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    password_changed REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    ip TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    target_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    result TEXT
);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    target_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

PERMISSIONS = {
    "host.view": "View host summary, hardware and monitoring",
    "host.config": "Change host configuration (hostname, DNS, NTP, time, services, SSH, firewall, certificates)",
    "host.power": "Reboot or shut down the host",
    "vm.view": "View virtual machines",
    "vm.power": "Power operations on virtual machines",
    "vm.console": "Open virtual machine consoles",
    "vm.snapshot": "Create, revert and delete snapshots",
    "vm.manage": "Create, edit, clone, delete virtual machines and templates",
    "ct.view": "View LXC containers",
    "ct.power": "Start, stop, restart, freeze and resume containers",
    "ct.console": "Open container consoles",
    "ct.snapshot": "Create, restore and delete container snapshots",
    "ct.manage": "Create, edit, clone and delete containers",
    "storage.view": "View storage",
    "storage.manage": "Manage datastores, volumes and ISO images",
    "storage.format": "Format physical disks (destructive)",
    "network.view": "View networking",
    "network.manage": "Change host and virtual networking",
    "security.manage": "Manage users and roles",
    "logs.view": "View system logs, events and audit log",
}

BUILTIN_ROLES = {
    "Administrator": ("Full control of the host", sorted(PERMISSIONS)),
    "Operator": ("Operate virtual machines; read-only elsewhere",
                 ["host.view", "vm.view", "vm.power", "vm.console", "vm.snapshot", "ct.view", "ct.power",
                  "ct.console", "ct.snapshot", "storage.view", "network.view", "logs.view"]),
    "ReadOnly": ("View everything, change nothing",
                 ["host.view", "vm.view", "ct.view", "storage.view", "network.view", "logs.view"]),
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._seed()

    def _seed(self) -> None:
        with self._lock:
            for name, (desc, perms) in BUILTIN_ROLES.items():
                self._conn.execute(
                    "INSERT INTO roles(name, description, permissions, builtin) VALUES(?,?,?,1) "
                    "ON CONFLICT(name) DO UPDATE SET permissions=excluded.permissions, description=excluded.description",
                    (name, desc, json.dumps(perms)))
            self._conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def transaction(self):
        return _Tx(self)

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    # -- audit -----------------------------------------------------------
    def audit(self, username: str, ip: str, action: str, target: str, outcome: str, details: Optional[dict] = None):
        self.execute("INSERT INTO audit(ts, username, ip, action, target, outcome, details) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), username, ip, action, target, outcome, json.dumps(details or {}, default=str)))

    def prune(self, keep_days: int = 90) -> None:
        cutoff = time.time() - keep_days * 86400
        for table, col in (("events", "ts"), ("audit", "ts"), ("tasks", "created")):
            self.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False


def create_user(db: Database, username: str, password_hash: str, role: str, full_name: str = "",
                must_change: bool = False) -> int:
    now = time.time()
    cur = db.execute(
        "INSERT INTO users(username, full_name, password_hash, role, enabled, must_change_password, created, password_changed)"
        " VALUES(?,?,?,?,1,?,?,?)", (username, full_name, password_hash, role, int(must_change), now, now))
    return int(cur.lastrowid)
