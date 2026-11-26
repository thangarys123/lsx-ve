"""Host alarms (the ESXi predefined alarm set, evaluated every 30 s).

An alarm is identified by ``definition:object``. When its state changes (triggered, severity changed,
cleared) an event is written and every web client gets an ``alarm`` message. Thresholds are stored in the
database (meta key ``alarm_settings``); alarm messages also go to the journal, so remote syslog carries them.
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from ..common import log
from ..host import vswitch
from . import tls

LOG = log.get("lsxve.alarms")

DEFINITIONS = {
    "host_cpu": "Host CPU usage",
    "host_memory": "Host memory usage",
    "datastore_usage": "Datastore usage on disk",
    "datastore_inactive": "Datastore inaccessible",
    "uplink": "Network uplink connectivity",
    "vm_cpu": "Virtual machine CPU usage",
    "vm_crashed": "Virtual machine error",
    "hypervisor": "Hypervisor service",
    "certificate": "Host certificate expiration",
    "maintenance": "Host in maintenance mode",
}
DEFAULT_SETTINGS = {
    "host_cpu": {"enabled": True, "warning": 80, "critical": 90, "minutes": 5},
    "host_memory": {"enabled": True, "warning": 85, "critical": 95},
    "datastore_usage": {"enabled": True, "warning": 75, "critical": 85},
    "vm_cpu": {"enabled": True, "warning": 75, "critical": 90, "minutes": 5},
    "uplink": {"enabled": True},
    "certificate": {"enabled": True, "warning_days": 30},
}


class AlarmManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.active: Dict[str, dict] = {}
        self.vm_cpu: Dict[str, list] = {}

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        s = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            stored = json.loads(self.ctx.db.get_meta("alarm_settings", "{}") or "{}")
        except ValueError:
            stored = {}
        for k, val in stored.items():
            if k in s and isinstance(val, dict):
                s[k].update({kk: vv for kk, vv in val.items() if kk in s[k] and isinstance(vv, type(s[k][kk]))})
        return s

    def save_settings(self, new: dict) -> dict:
        cur = self.settings()
        for k, val in (new or {}).items():
            if k not in cur or not isinstance(val, dict):
                continue
            for kk, vv in val.items():
                if kk not in cur[k]:
                    continue
                if kk == "enabled":
                    cur[k][kk] = bool(vv)
                elif isinstance(vv, int) and not isinstance(vv, bool) and 1 <= vv <= (365 if kk.endswith("days") else 100
                                                                                       if kk != "minutes" else 60):
                    cur[k][kk] = vv
        for k in ("host_cpu", "host_memory", "datastore_usage", "vm_cpu"):
            if cur[k]["warning"] > cur[k]["critical"]:
                cur[k]["warning"] = cur[k]["critical"]
        self.ctx.db.set_meta("alarm_settings", json.dumps(cur))
        return cur

    # ------------------------------------------------------------------ evaluation
    def list(self) -> List[dict]:
        return sorted(self.active.values(), key=lambda a: (a["severity"] != "critical", -a["since"]))

    @staticmethod
    def _level(value: float, cfg: dict) -> Optional[str]:
        if value >= cfg["critical"]:
            return "critical"
        if value >= cfg["warning"]:
            return "warning"
        return None

    def evaluate(self) -> None:
        st = self.settings()
        found: Dict[str, dict] = {}

        def hit(defn: str, obj: str, severity: str, message: str, target: str = "host"):
            found[f"{defn}:{obj}"] = {"definition": defn, "name": DEFINITIONS[defn], "object": obj,
                                      "severity": severity, "message": message, "target": target}
        now = time.time()
        # host CPU / memory (averages over the window)
        hist = self.ctx.sampler.series(now - 60 * st["host_cpu"]["minutes"])
        if st["host_cpu"]["enabled"] and len(hist) >= 5:
            avg = sum(h["cpu"] for h in hist) / len(hist)
            lvl = self._level(avg, st["host_cpu"])
            if lvl:
                hit("host_cpu", "host", lvl, f"Host CPU usage {avg:.0f}% over {st['host_cpu']['minutes']} min")
        last = hist[-1] if hist else None
        if st["host_memory"]["enabled"] and last:
            lvl = self._level(last["mem"], st["host_memory"])
            if lvl:
                hit("host_memory", "host", lvl, f"Host memory usage {last['mem']:.0f}%")
        # hypervisor + datastores
        try:
            pools = self.ctx.hv.list_pools()
        except Exception as e:  # noqa: BLE001
            hit("hypervisor", "libvirt", "critical", f"The hypervisor service is not reachable: {e}")
            pools = []
        for p in pools:
            if not p.get("active"):
                hit("datastore_inactive", p["name"], "critical", f"Datastore {p['name']} is inaccessible", f"pool:{p['name']}")
                continue
            if st["datastore_usage"]["enabled"] and p.get("capacity"):
                used = 100.0 * (p["capacity"] - p.get("available", 0)) / p["capacity"]
                lvl = self._level(used, st["datastore_usage"])
                if lvl:
                    hit("datastore_usage", p["name"], lvl, f"Datastore {p['name']} is {used:.0f}% full", f"pool:{p['name']}")
        # vSwitch uplinks
        if st["uplink"]["enabled"]:
            try:
                for sw in vswitch.list_vswitches():
                    ups = sw["uplinks"]
                    down = [u["name"] for u in ups if not u["carrier"]]
                    if ups and len(down) == len(ups):
                        hit("uplink", sw["name"], "critical", f"Virtual switch {sw['name']}: all uplinks down "
                                                              f"({', '.join(down)})")
                    elif down:
                        hit("uplink", sw["name"], "warning", f"Virtual switch {sw['name']}: uplink redundancy lost "
                                                             f"({', '.join(down)} down)")
            except Exception:  # noqa: BLE001
                pass
        # VMs
        try:
            vms = self.ctx.hv.list_vms()
        except Exception:  # noqa: BLE001
            vms = []
        stats = self.ctx.last_vm_stats or {}
        for vm in vms:
            if vm["state"] == "crashed":
                hit("vm_crashed", vm["uuid"], "critical", f"Virtual machine {vm['name']} crashed", vm["uuid"])
            s = stats.get(vm["uuid"])
            win = self.vm_cpu.setdefault(vm["uuid"], [])
            if s:
                win.append((now, s["cpu_pct"]))
            win[:] = [x for x in win if now - x[0] <= 60 * st["vm_cpu"]["minutes"]]
            if st["vm_cpu"]["enabled"] and len(win) >= 5 and s:
                avg = sum(x[1] for x in win) / len(win)
                lvl = self._level(avg, st["vm_cpu"])
                if lvl:
                    hit("vm_cpu", vm["uuid"], lvl, f"Virtual machine {vm['name']} CPU usage {avg:.0f}%", vm["uuid"])
        # certificate
        if st["certificate"]["enabled"]:
            ci = tls.info()
            if ci.get("present") and ci.get("not_after"):
                days = (ci["not_after"] - now) / 86400
                if days < 0:
                    hit("certificate", "host", "critical", "The host certificate has expired")
                elif days < st["certificate"]["warning_days"]:
                    hit("certificate", "host", "warning", f"The host certificate expires in {days:.0f} days")
        if self.ctx.maintenance().get("enabled"):
            hit("maintenance", "host", "info", "The host is in maintenance mode: VMs cannot be powered on")
        self._apply(found, now)

    def _apply(self, found: Dict[str, dict], now: float) -> None:
        changed = False
        for key, a in found.items():
            old = self.active.get(key)
            if old and old["severity"] == a["severity"]:
                old["message"] = a["message"]
                continue
            self.active[key] = {**a, "id": key, "since": old["since"] if old else now, "acknowledged": False}
            sev = "error" if a["severity"] == "critical" else ("warning" if a["severity"] == "warning" else "info")
            self.ctx.bus.event(sev, "alarm", f"Alarm '{a['name']}' {a['severity']}: {a['message']}", a["target"], a["object"])
            LOG.warning("alarm %s %s: %s", a["name"], a["severity"], a["message"])
            changed = True
        for key in [k for k in self.active if k not in found]:
            a = self.active.pop(key)
            self.ctx.bus.event("info", "alarm", f"Alarm '{a['name']}' cleared: {a['message']}", a["target"], a["object"])
            changed = True
        if changed:
            self.ctx.bus.broadcast("alarm", {"alarms": self.list()})

    def acknowledge(self, key: str, user: str) -> dict:
        a = self.active.get(key)
        if not a:
            raise KeyError(key)
        a["acknowledged"], a["acknowledged_by"] = True, user
        self.ctx.bus.broadcast("alarm", {"alarms": self.list()})
        return a
