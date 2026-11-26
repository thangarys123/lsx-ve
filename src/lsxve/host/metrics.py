"""Periodic host utilisation sampling with an in-memory history ring buffer."""
from __future__ import annotations

import collections
import os
import time
from typing import Deque, Dict, List, Optional, Tuple


def _read(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _cpu_times() -> Optional[Tuple[int, int]]:
    line = _read("/proc/stat").split("\n", 1)[0]
    if not line.startswith("cpu "):
        return None
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals[:8]), idle


def _net_bytes() -> Dict[str, Tuple[int, int]]:
    out = {}
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("vnet", "virbr", "macvtap")):
            continue
        # only count physical NICs so bridged traffic is not double counted
        if not os.path.exists(f"/sys/class/net/{name}/device"):
            continue
        f = rest.split()
        out[name] = (int(f[0]), int(f[8]))
    return out


def _disk_bytes() -> Tuple[int, int]:
    rd = wr = 0
    for line in _read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        name = f[2]
        if not os.path.exists(f"/sys/block/{name}/device"):
            continue  # whole physical disks only
        rd += int(f[5]) * 512
        wr += int(f[9]) * 512
    return rd, wr


def _meminfo() -> Tuple[int, int]:
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        p = line.split()
        if len(p) >= 2:
            m[p[0].rstrip(":")] = int(p[1]) * 1024
    total = m.get("MemTotal", 0)
    return total, total - m.get("MemAvailable", m.get("MemFree", 0))


class HostSampler:
    def __init__(self, history: int = 1200):
        self.history: Deque[dict] = collections.deque(maxlen=history)
        self._prev_cpu = _cpu_times()
        self._prev_net = _net_bytes()
        self._prev_disk = _disk_bytes()
        self._prev_t = time.monotonic()
        self.available = self._prev_cpu is not None

    def sample(self) -> Optional[dict]:
        if not self.available:
            return None
        now = time.monotonic()
        dt = max(0.001, now - self._prev_t)
        cpu = _cpu_times()
        net = _net_bytes()
        disk = _disk_bytes()
        cpu_pct = 0.0
        if cpu and self._prev_cpu:
            dtot = cpu[0] - self._prev_cpu[0]
            didle = cpu[1] - self._prev_cpu[1]
            cpu_pct = 100.0 * (1 - didle / dtot) if dtot > 0 else 0.0
        rx = tx = 0.0
        per_nic = {}
        for k, (r, t) in net.items():
            pr, pt = self._prev_net.get(k, (r, t))
            nr, nt = max(0, r - pr) / dt, max(0, t - pt) / dt
            per_nic[k] = {"rx": round(nr), "tx": round(nt)}
            rx += nr
            tx += nt
        drd = max(0, disk[0] - self._prev_disk[0]) / dt
        dwr = max(0, disk[1] - self._prev_disk[1]) / dt
        total, used = _meminfo()
        load = _read("/proc/loadavg").split()
        s = {
            "ts": time.time(), "cpu": round(cpu_pct, 1), "mem_used": used, "mem_total": total,
            "mem": round(100.0 * used / total, 1) if total else 0, "net_rx": round(rx), "net_tx": round(tx),
            "nics": per_nic, "disk_read": round(drd), "disk_write": round(dwr),
            "load": [float(x) for x in load[:3]] if len(load) >= 3 else [],
        }
        self._prev_cpu, self._prev_net, self._prev_disk, self._prev_t = cpu, net, disk, now
        self.history.append(s)
        return s

    def series(self, since: float = 0) -> List[dict]:
        return [s for s in self.history if s["ts"] >= since]
