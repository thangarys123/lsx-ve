"""Runtime configuration for all LSX-VE components.

Configuration is read from /etc/lsxve/lsxve.conf (JSON). Every key has a safe
default so a missing or partial file never prevents the services from starting.
Set LSXVE_DEV=1 to run from a source checkout (state kept under ./var).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEV = os.environ.get("LSXVE_DEV") == "1"

if DEV:
    _root = Path(os.environ.get("LSXVE_DEV_ROOT", Path(__file__).resolve().parents[3] / "var"))
    ETC_DIR = _root / "etc"
    STATE_DIR = _root / "lib"
    RUN_DIR = _root / "run"
    LOG_DIR = _root / "log"
else:
    ETC_DIR = Path("/etc/lsxve")
    STATE_DIR = Path("/var/lib/lsxve")
    RUN_DIR = Path("/run/lsxve")
    LOG_DIR = Path("/var/log/lsxve")

CONFIG_FILE = ETC_DIR / "lsxve.conf"
TLS_DIR = ETC_DIR / "tls"
DB_FILE = STATE_DIR / "lsxve.db"
AGENT_SOCKET = RUN_DIR / "agent.sock"
ISO_DIR = (STATE_DIR / "iso") if DEV else Path("/lsxve/iso")
DATASTORE_ROOT = Path("/lsxve/datastores")
GUEST_TOOLS_ISO = Path(os.environ.get("LSXVE_GUEST_TOOLS", "/usr/share/lsxve/guest-tools/lsx-ve-guest-tools.iso"))
SUPPORT_DIR = STATE_DIR / "support"
MAINTENANCE_FILE = STATE_DIR / "maintenance.json"
AUTOSTART_FILE = STATE_DIR / "autostart.json"
LOCKDOWN_FILE = STATE_DIR / "lockdown.json"
WEB_ROOT = Path(os.environ.get("LSXVE_WEB_ROOT", "/usr/share/lsxve/web"))
if DEV and not WEB_ROOT.exists():
    WEB_ROOT = Path(__file__).resolve().parents[3] / "web" / "dist"

DEFAULTS = {
    "listen_address": "0.0.0.0",
    "https_port": 443,
    "http_redirect_port": 80,
    "session_idle_timeout": 1800,       # seconds
    "session_absolute_timeout": 43200,  # seconds
    "login_max_failures": 5,
    "login_lockout_seconds": 300,
    "api_rate_per_minute": 1200,
    "metrics_interval": 3,
    "metrics_history": 1200,            # samples kept in memory (1h @ 3s)
    "libvirt_uri": "qemu:///system",
    "max_upload_bytes": 64 * 1024 ** 3,
    "network_rollback_seconds": 90,
    "management_bridge": "br0",          # bridge VMs/containers share with the management NIC ("" = off)
    "trusted_origins": [],
    "console_blank_minutes": -1,          # DCUI screen + backlight off after N idle minutes; -1 = 10 on laptops, 0 = never
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:  # pragma: no cover - logged by caller
        cfg["_config_error"] = str(exc)
    if DEV:
        cfg["https_port"] = int(os.environ.get("LSXVE_DEV_PORT", "8443"))
        cfg["http_redirect_port"] = 0
        cfg["listen_address"] = os.environ.get("LSXVE_DEV_LISTEN", "127.0.0.1")
    return cfg


def ensure_dirs() -> None:
    for d in (ETC_DIR, TLS_DIR, STATE_DIR, RUN_DIR, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
