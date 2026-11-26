"""Privileged host operations. Executed by lsxve-agent (root) and the local console.

Each public function validates its own arguments (defence in depth - the API
validates too) and only runs allow-listed binaries through ``proc.run``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..common import proc, validate as v

NMCLI = "/usr/bin/nmcli"
SYSTEMCTL = "/usr/bin/systemctl"
FWCMD = "/usr/bin/firewall-cmd"

DNS_CONF = Path("/etc/NetworkManager/conf.d/90-lsxve-dns.conf")
CHRONY_CONF = Path("/etc/chrony.conf")
SSHD_DROPIN = Path("/etc/ssh/sshd_config.d/40-lsxve.conf")
DATASTORE_ROOT = Path("/lsxve/datastores")

# service -> allowed actions
SERVICES: Dict[str, tuple] = {
    "sshd": ("start", "stop", "restart", "enable", "disable"),
    "chronyd": ("start", "stop", "restart", "enable", "disable"),
    "firewalld": ("restart",),
    "NetworkManager": ("restart",),
    "virtqemud": ("restart",),
    "virtnetworkd": ("restart",),
    "virtstoraged": ("restart",),
    "virtnodedevd": ("restart",),
    "virtlogd": ("restart",),
    "libvirt-guests": ("enable", "disable"),
    "smartd": ("start", "stop", "restart", "enable", "disable"),
    "iscsid": ("start", "stop", "restart", "enable", "disable"),
    "lsxve-api": ("restart",),
    "lsxve-dcui": ("restart",),
    "lsxve-agent": ("restart",),
}
PROTECTED_FW_SERVICES = {"lsxve"}


def _later(fn, delay: float = 2.0) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


# ---------------------------------------------------------------- host identity
def set_hostname(hostname: str) -> dict:
    hostname = v.hostname(hostname)
    proc.run(["/usr/bin/hostnamectl", "set-hostname", hostname])
    return {"hostname": hostname}


def _try_run(argv: List[str], timeout: float = 5) -> str:
    """Run a read-only status command; never raise (timeouts and missing tools give "")."""
    try:
        return proc.run(argv, check=False, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


CHRONY_SOCK = "/run/chrony/chronyd.sock"


def get_time() -> dict:
    info = dict(line.split("=", 1) for line in _try_run(["/usr/bin/timedatectl", "show"]).splitlines() if "=" in line)
    servers = []
    try:
        for line in CHRONY_CONF.read_text().splitlines():
            m = re.match(r"^\s*(server|pool)\s+(\S+)", line)
            if m:
                servers.append(m.group(2))
    except OSError:
        pass
    tracking, sources = "", []
    # Talk to chronyd over its Unix socket only (-h path): no slow UDP fallback when chronyd is down.
    if proc.which("/usr/bin/chronyc") and os.path.exists(CHRONY_SOCK):
        tracking = _try_run(["/usr/bin/chronyc", "-h", CHRONY_SOCK, "-n", "-c", "tracking"], 3).strip()
        for line in _try_run(["/usr/bin/chronyc", "-h", CHRONY_SOCK, "-n", "-c", "sources"], 3).splitlines():
            f = line.split(",")
            if len(f) >= 10:
                sources.append({"mode": f[0], "state": f[1], "address": f[2], "stratum": f[3],
                                "reach": f[5], "offset": f[8]})
    ref = tracking.split(",")
    tz = info.get("Timezone")
    if not tz:
        try:
            tz = os.path.realpath("/etc/localtime").split("/zoneinfo/", 1)[1]
        except (IndexError, OSError):
            tz = None
    return {"timezone": tz, "ntp_enabled": info.get("NTP") == "yes",
            "ntp_synchronized": info.get("NTPSynchronized") == "yes", "local_rtc": info.get("LocalRTC") == "yes",
            "time": time.time(), "ntp_servers": servers, "ntp_sources": sources,
            "reference": ref[1] if len(ref) > 1 else None,
            "chronyd": os.path.exists(CHRONY_SOCK)}


def set_timezone(tz: str) -> dict:
    tz = v.string(tz, "timezone", 1, 64, v.RE_TZ)
    if not Path("/usr/share/zoneinfo", tz).is_file():
        raise v.ValidationError("timezone", "unknown time zone")
    proc.run(["/usr/bin/timedatectl", "set-timezone", tz])
    return {"timezone": tz}


def list_timezones() -> List[str]:
    return proc.run(["/usr/bin/timedatectl", "list-timezones"]).stdout.split()


def set_ntp(servers: List[str], enabled: bool) -> dict:
    servers = v.list_of(servers, "servers", v.host_or_ip, 8)
    enabled = v.boolean(enabled, "enabled")
    lines = CHRONY_CONF.read_text().splitlines() if CHRONY_CONF.exists() else []
    out = [l for l in lines if not re.match(r"^\s*(server|pool)\s", l) and "LSX-VE managed" not in l]
    managed = ["# --- NTP sources (LSX-VE managed) ---"] + [f"server {s} iburst" for s in servers]
    tmp = CHRONY_CONF.with_suffix(".lsxve.tmp")
    tmp.write_text("\n".join(managed + out) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, CHRONY_CONF)
    proc.run(["/usr/bin/timedatectl", "set-ntp", "true" if enabled else "false"])
    if enabled:
        proc.run([SYSTEMCTL, "restart", "chronyd"])
    return get_time()


def get_dns() -> dict:
    servers, search = [], []
    if DNS_CONF.exists():
        for line in DNS_CONF.read_text().splitlines():
            if line.startswith("servers="):
                servers = [s for s in line.split("=", 1)[1].split(",") if s]
            if line.startswith("searches="):
                search = [s for s in line.split("=", 1)[1].split(",") if s]
    resolv = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            if line.startswith("nameserver"):
                resolv.append(line.split()[1])
    except OSError:
        pass
    return {"mode": "static" if DNS_CONF.exists() else "automatic", "servers": servers, "search": search,
            "effective": resolv}


def set_dns(servers: List[str], search: List[str]) -> dict:
    servers = v.list_of(servers, "servers", lambda x, f: v.ip_address(x, f), 4)
    search = v.list_of(search, "search", v.hostname, 6)
    if not servers:
        if DNS_CONF.exists():
            DNS_CONF.unlink()
    else:
        body = "# Managed by LSX-VE\n[global-dns]\n"
        if search:
            body += "searches=" + ",".join(search) + "\n"
        body += "\n[global-dns-domain-*]\nservers=" + ",".join(servers) + "\n"
        DNS_CONF.write_text(body)
        os.chmod(DNS_CONF, 0o644)
    proc.run([SYSTEMCTL, "reload", "NetworkManager"])
    time.sleep(1)
    return get_dns()


# ---------------------------------------------------------------- services / ssh / power
def list_services() -> List[dict]:
    units = [f"{s}.service" for s in SERVICES]
    cp = proc.run([SYSTEMCTL, "show", "--no-pager", "-p", "Id,Description,ActiveState,SubState,UnitFileState,LoadState",
                   *units], check=False)
    out = []
    for block in cp.stdout.strip().split("\n\n"):
        d = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        name = d.get("Id", "").replace(".service", "")
        if not name:
            continue
        out.append({"name": name, "description": d.get("Description", ""), "active": d.get("ActiveState"),
                    "sub": d.get("SubState"), "enabled": d.get("UnitFileState"),
                    "installed": d.get("LoadState") == "loaded", "actions": list(SERVICES.get(name, ()))})
    return out


def service_action(name: str, action: str) -> dict:
    if name not in SERVICES or action not in SERVICES[name]:
        raise v.ValidationError("action", f"'{action}' is not permitted for service '{name}'")
    unit = f"{name}.service"
    if name in ("lsxve-api", "lsxve-agent"):
        _later(lambda: proc.run([SYSTEMCTL, action, unit], check=False))
        return {"name": name, "scheduled": True}
    proc.run([SYSTEMCTL, action, unit], timeout=60)
    return [s for s in list_services() if s["name"] == name][0]


def get_ssh() -> dict:
    root_login = "prohibit-password"
    if SSHD_DROPIN.exists():
        m = re.search(r"^PermitRootLogin\s+(\S+)", SSHD_DROPIN.read_text(), re.M)
        if m:
            root_login = m.group(1)
    st = [s for s in list_services() if s["name"] == "sshd"]
    return {"service": st[0] if st else None, "permit_root_login": root_login}


def set_ssh(enabled: bool, permit_root_login: str) -> dict:
    enabled = v.boolean(enabled, "enabled")
    prl = v.choice(permit_root_login, "permit_root_login", ("yes", "no", "prohibit-password"))
    from .hostcfg import lockdown_mode
    if enabled and lockdown_mode() != "disabled":
        raise v.ValidationError("enabled", f"SSH cannot be enabled while lockdown mode is {lockdown_mode()}; "
                                           "disable lockdown mode in the web console first")
    SSHD_DROPIN.write_text(f"# Managed by LSX-VE\nPermitRootLogin {prl}\n")
    os.chmod(SSHD_DROPIN, 0o600)
    if enabled:
        proc.run([SYSTEMCTL, "enable", "--now", "sshd.service"])
        proc.run([SYSTEMCTL, "reload", "sshd.service"], check=False)
    else:
        proc.run([SYSTEMCTL, "disable", "--now", "sshd.service"])
    return get_ssh()


def power(action: str) -> dict:
    action = v.choice(action, "action", ("reboot", "poweroff"))
    _later(lambda: proc.run([SYSTEMCTL, action], check=False), 3)
    return {"scheduled": action}


def set_root_password(password: str) -> dict:
    password = v.password(password)
    if ":" in password or "\n" in password:
        raise v.ValidationError("password", "must not contain ':' or newlines")
    proc.run(["/usr/sbin/chpasswd"], input_text=f"root:{password}\n")
    return {"ok": True}


# ---------------------------------------------------------------- firewall
def firewall_status() -> dict:
    if not proc.which(FWCMD):
        return {"available": False}
    state = proc.run([FWCMD, "--state"], check=False).stdout.strip()
    if state != "running":
        return {"available": True, "running": False}
    zone = proc.run([FWCMD, "--get-default-zone"]).stdout.strip()
    services = proc.run([FWCMD, f"--zone={zone}", "--list-services"]).stdout.split()
    ports = proc.run([FWCMD, f"--zone={zone}", "--list-ports"]).stdout.split()
    available = proc.run([FWCMD, "--get-services"]).stdout.split()
    return {"available": True, "running": True, "zone": zone, "services": services, "ports": ports,
            "available_services": available, "protected": sorted(PROTECTED_FW_SERVICES)}


def firewall_change(op: str, kind: str, value: str) -> dict:
    op = v.choice(op, "op", ("add", "remove"))
    kind = v.choice(kind, "kind", ("service", "port"))
    if kind == "service":
        value = v.string(value, "value", 1, 64, re.compile(r"^[a-z0-9][a-z0-9_-]*$"))
        if value not in proc.run([FWCMD, "--get-services"]).stdout.split():
            raise v.ValidationError("value", "unknown firewall service")
        if op == "remove" and value in PROTECTED_FW_SERVICES:
            raise v.ValidationError("value", "the LSX-VE management service cannot be removed")
    else:
        value = v.string(value, "value", 3, 20, re.compile(r"^\d{1,5}(-\d{1,5})?/(tcp|udp|sctp)$"))
        for p in re.findall(r"\d+", value.split("/")[0]):
            if not 1 <= int(p) <= 65535:
                raise v.ValidationError("value", "port out of range")
    zone = proc.run([FWCMD, "--get-default-zone"]).stdout.strip()
    flag = f"--{op}-{kind}={value}"
    proc.run([FWCMD, f"--zone={zone}", flag])
    proc.run([FWCMD, "--permanent", f"--zone={zone}", flag])
    return firewall_status()


# ---------------------------------------------------------------- networking (NetworkManager)
def _nm_unescape(s: str) -> str:
    return s.replace("\\:", ":").replace("\\\\", "\\")


def _nm_split(line: str) -> List[str]:
    return [_nm_unescape(x) for x in re.split(r"(?<!\\):", line)]


def network_state() -> dict:
    devices = []
    cp = proc.run([NMCLI, "-t", "-e", "yes", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], check=False)
    for line in cp.stdout.splitlines():
        f = _nm_split(line)
        if len(f) >= 4 and f[0] != "lo":
            devices.append({"device": f[0], "type": f[1], "state": f[2], "connection": f[3] or None})
    conns = []
    cp = proc.run([NMCLI, "-t", "-e", "yes", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"], check=False)
    for line in cp.stdout.splitlines():
        f = _nm_split(line)
        if len(f) < 5 or f[2] == "loopback":
            continue
        conns.append(_connection_detail(f[1], f[0], f[2], f[3], f[4] == "yes"))
    addrs = []
    try:
        addrs = json.loads(proc.run(["/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else "/usr/bin/ip", "-j", "addr"],
                                    check=False).stdout or "[]")
    except ValueError:
        pass
    routes = []
    try:
        routes = json.loads(proc.run(["/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else "/usr/bin/ip", "-j", "route"],
                                     check=False).stdout or "[]")
    except ValueError:
        pass
    live = {a["ifname"]: [f"{x['local']}/{x['prefixlen']}" for x in a.get("addr_info", []) if x.get("scope") != "host"]
            for a in addrs}
    return {"devices": devices, "connections": conns, "addresses": live,
            "default_gateway": next((r.get("gateway") for r in routes if r.get("dst") == "default"), None),
            "dns": get_dns()}


_FIELDS = ["connection.id", "connection.uuid", "connection.type", "connection.interface-name", "connection.autoconnect",
           "connection.master", "connection.slave-type", "ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns",
           "ipv6.method", "ipv6.addresses", "ipv6.gateway", "802-3-ethernet.mtu", "vlan.parent", "vlan.id",
           "bond.options", "bridge.stp"]


def _connection_detail(uuid: str, name: str, ctype: str, device: str, active: bool) -> dict:
    cp = proc.run([NMCLI, "-t", "-e", "yes", "connection", "show", uuid], check=False)
    d = {}
    for line in cp.stdout.splitlines():
        if ":" in line:
            k, val = re.split(r"(?<!\\):", line, 1)
            if k in _FIELDS or k.startswith(("vlan.", "bond.", "bridge.", "ipv4.", "ipv6.")):
                d[k] = _nm_unescape(val)
    def lst(key):
        return [x.strip() for x in d.get(key, "").split(",") if x.strip() and x.strip() != "--"]
    mtu = d.get("802-3-ethernet.mtu", "auto")
    return {
        "uuid": uuid, "name": name, "type": ctype, "device": device or None, "active": active,
        "interface": d.get("connection.interface-name") or None, "autoconnect": d.get("connection.autoconnect") == "yes",
        "master": d.get("connection.master") or None if d.get("connection.master") not in ("", "--") else None,
        "slave_type": d.get("connection.slave-type") if d.get("connection.slave-type") not in ("", "--") else None,
        "ipv4": {"method": d.get("ipv4.method"), "addresses": lst("ipv4.addresses"),
                 "gateway": d.get("ipv4.gateway") if d.get("ipv4.gateway") not in ("", "--") else None,
                 "dns": lst("ipv4.dns")},
        "ipv6": {"method": d.get("ipv6.method"), "addresses": lst("ipv6.addresses"),
                 "gateway": d.get("ipv6.gateway") if d.get("ipv6.gateway") not in ("", "--") else None},
        "mtu": None if mtu in ("auto", "", "--", "0") else int(mtu) if mtu.isdigit() else None,
        "vlan": {"parent": d.get("vlan.parent"), "id": d.get("vlan.id")} if ctype == "vlan" else None,
        "bond_options": d.get("bond.options") if ctype == "bond" else None,
    }


def _ip_args(cfg: dict) -> List[str]:
    """Translate a validated IP configuration into nmcli property arguments."""
    args: List[str] = []
    ip4 = cfg.get("ipv4")
    if ip4:
        m = v.choice(ip4.get("method"), "ipv4.method", ("auto", "manual", "disabled"))
        if m == "manual":
            addrs = v.list_of(ip4.get("addresses", []), "ipv4.addresses", v.ipv4_interface, 8)
            if not addrs:
                raise v.ValidationError("ipv4.addresses", "at least one address is required for static IPv4")
            gw = ip4.get("gateway")
            args += ["ipv4.method", "manual", "ipv4.addresses", ",".join(addrs),
                     "ipv4.gateway", v.ip_address(gw, "ipv4.gateway", 4) if gw else ""]
        else:
            args += ["ipv4.method", m, "ipv4.addresses", "", "ipv4.gateway", ""]
        if "dns" in ip4:
            dns = v.list_of(ip4.get("dns", []), "ipv4.dns", lambda x, f: v.ip_address(x, f, 4), 4)
            args += ["ipv4.dns", " ".join(dns)]
    ip6 = cfg.get("ipv6")
    if ip6:
        m = v.choice(ip6.get("method"), "ipv6.method", ("auto", "dhcp", "manual", "ignore", "disabled"))
        if m == "manual":
            addrs = v.list_of(ip6.get("addresses", []), "ipv6.addresses", v.ipv6_interface, 8)
            if not addrs:
                raise v.ValidationError("ipv6.addresses", "at least one address is required for static IPv6")
            gw = ip6.get("gateway")
            args += ["ipv6.method", "manual", "ipv6.addresses", ",".join(addrs),
                     "ipv6.gateway", v.ip_address(gw, "ipv6.gateway", 6) if gw else ""]
        else:
            args += ["ipv6.method", m, "ipv6.addresses", "", "ipv6.gateway", ""]
    return args


def _mtu_args(ctype: str, mtu) -> List[str]:
    if mtu is None:
        return []
    mtu = v.integer(mtu, "mtu", 576, 9216)
    prop = {"802-3-ethernet": "802-3-ethernet.mtu", "ethernet": "802-3-ethernet.mtu", "bridge": "ethernet.mtu",
            "bond": "ethernet.mtu", "vlan": "ethernet.mtu"}.get(ctype, "ethernet.mtu")
    return [prop, str(mtu)]


def _conn_type(uuid: str) -> str:
    return proc.run([NMCLI, "-g", "connection.type", "connection", "show", uuid]).stdout.strip()


class Checkpoint:
    """NetworkManager checkpoint (automatic rollback unless confirmed)."""

    FLAGS = 0x1 | 0x2 | 0x4  # DESTROY_ALL | DELETE_NEW_CONNECTIONS | DISCONNECT_NEW_DEVICES

    @staticmethod
    def _nm():
        import dbus  # python3-dbus
        bus = dbus.SystemBus()
        obj = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        return dbus, dbus.Interface(obj, "org.freedesktop.NetworkManager")

    @classmethod
    def create(cls, timeout: int) -> str:
        dbus, nm = cls._nm()
        return str(nm.CheckpointCreate(dbus.Array([], signature="o"), dbus.UInt32(timeout), dbus.UInt32(cls.FLAGS)))

    @classmethod
    def commit(cls, path: str) -> None:
        _, nm = cls._nm()
        nm.CheckpointDestroy(path)

    @classmethod
    def rollback(cls, path: str) -> dict:
        _, nm = cls._nm()
        res = nm.CheckpointRollback(path)
        return {str(k): int(val) for k, val in dict(res).items()}

    @classmethod
    def exists(cls, path: str) -> bool:
        import dbus
        bus = dbus.SystemBus()
        obj = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return path in [str(p) for p in props.Get("org.freedesktop.NetworkManager", "Checkpoints")]


def _apply_change(change: dict) -> str:
    """Apply one validated network change. Returns a human readable summary."""
    op = v.choice(change.get("op"), "op", ("configure", "create_bridge", "create_vlan", "create_bond", "delete"))
    if op == "configure":
        uuid = v.uuid(change.get("uuid"))
        ctype = _conn_type(uuid)
        args = _ip_args(change) + _mtu_args(ctype, change.get("mtu"))
        if "autoconnect" in change:
            args += ["connection.autoconnect", "yes" if v.boolean(change["autoconnect"], "autoconnect") else "no"]
        if not args:
            raise v.ValidationError("change", "nothing to change")
        proc.run([NMCLI, "connection", "modify", uuid, *args])
        proc.run([NMCLI, "connection", "up", uuid], timeout=60)
        return f"Updated connection {uuid}"
    if op == "delete":
        uuid = v.uuid(change.get("uuid"))
        proc.run([NMCLI, "connection", "delete", uuid], timeout=60)
        return f"Deleted connection {uuid}"
    name = v.ifname(change.get("name"), "name")
    if op == "create_bridge":
        ports = v.list_of(change.get("ports", []), "ports", v.ifname, 8)
        stp = "yes" if change.get("stp") else "no"
        proc.run([NMCLI, "connection", "add", "type", "bridge", "ifname", name, "con-name", name,
                  "bridge.stp", stp, *_ip_args(change), *_mtu_args("bridge", change.get("mtu"))])
        for p in ports:
            _release_device(p)
            proc.run([NMCLI, "connection", "add", "type", "ethernet", "ifname", p, "con-name", f"{name}-port-{p}",
                      "master", name, "slave-type", "bridge"])
        proc.run([NMCLI, "connection", "up", name], timeout=60)
        for p in ports:
            proc.run([NMCLI, "connection", "up", f"{name}-port-{p}"], timeout=60, check=False)
        return f"Created bridge {name}" + (f" with ports {', '.join(ports)}" if ports else "")
    if op == "create_vlan":
        parent = v.ifname(change.get("parent"), "parent")
        vid = v.integer(change.get("vlan_id"), "vlan_id", 1, 4094)
        proc.run([NMCLI, "connection", "add", "type", "vlan", "ifname", name, "con-name", name, "dev", parent,
                  "id", str(vid), *_ip_args(change), *_mtu_args("vlan", change.get("mtu"))])
        proc.run([NMCLI, "connection", "up", name], timeout=60)
        return f"Created VLAN {name} (id {vid} on {parent})"
    if op == "create_bond":
        mode = v.choice(change.get("mode"), "mode", ("active-backup", "balance-rr", "balance-xor", "802.3ad",
                                                     "balance-tlb", "balance-alb", "broadcast"))
        members = v.list_of(change.get("members", []), "members", v.ifname, 8)
        if not members:
            raise v.ValidationError("members", "at least one member interface is required")
        proc.run([NMCLI, "connection", "add", "type", "bond", "ifname", name, "con-name", name,
                  "bond.options", f"mode={mode},miimon=100", *_ip_args(change), *_mtu_args("bond", change.get("mtu"))])
        for m in members:
            _release_device(m)
            proc.run([NMCLI, "connection", "add", "type", "ethernet", "ifname", m, "con-name", f"{name}-member-{m}",
                      "master", name, "slave-type", "bond"])
        proc.run([NMCLI, "connection", "up", name], timeout=60)
        for m in members:
            proc.run([NMCLI, "connection", "up", f"{name}-member-{m}"], timeout=60, check=False)
        return f"Created bond {name} ({mode}) with {', '.join(members)}"
    raise v.ValidationError("op", "unsupported")


def _release_device(dev: str) -> None:
    """Disable autoconnect on profiles bound to ``dev`` so it can be enslaved."""
    cp = proc.run([NMCLI, "-t", "-e", "yes", "-f", "UUID,DEVICE,TYPE", "connection", "show"], check=False)
    for line in cp.stdout.splitlines():
        f = _nm_split(line)
        if len(f) >= 3 and f[1] == dev and f[2] in ("802-3-ethernet", "ethernet"):
            proc.run([NMCLI, "connection", "modify", f[0], "connection.autoconnect", "no"], check=False)
            proc.run([NMCLI, "connection", "down", f[0]], check=False, timeout=30)


_PENDING: Dict[str, dict] = {}


def network_apply(change: dict, rollback_seconds: int) -> dict:
    rollback_seconds = v.integer(rollback_seconds, "rollback_seconds", 30, 600)
    for path, p in list(_PENDING.items()):
        if Checkpoint.exists(path):
            raise v.ValidationError("change", "another network change is awaiting confirmation")
        _PENDING.pop(path, None)
    cp = Checkpoint.create(rollback_seconds)
    try:
        summary = _apply_change(change)
    except Exception:
        try:
            Checkpoint.rollback(cp)
        except Exception:
            pass
        raise
    _PENDING[cp] = {"checkpoint": cp, "summary": summary, "created": time.time(),
                    "expires": time.time() + rollback_seconds}
    return _PENDING[cp]


def network_pending() -> Optional[dict]:
    for path, p in list(_PENDING.items()):
        try:
            alive = Checkpoint.exists(path)
        except Exception:
            alive = False
        if alive:
            return p
        _PENDING.pop(path, None)
        p["state"] = "rolled_back"
        return {**p, "state": "rolled_back"}
    return None


def network_confirm(checkpoint: str) -> dict:
    if checkpoint not in _PENDING:
        raise v.ValidationError("checkpoint", "no such pending change (it may already have been rolled back)")
    if not Checkpoint.exists(checkpoint):
        _PENDING.pop(checkpoint, None)
        raise v.ValidationError("checkpoint", "the change was already rolled back automatically")
    Checkpoint.commit(checkpoint)
    p = _PENDING.pop(checkpoint)
    return {**p, "state": "committed"}


def network_rollback(checkpoint: str) -> dict:
    if checkpoint not in _PENDING:
        raise v.ValidationError("checkpoint", "no such pending change")
    res = Checkpoint.rollback(checkpoint)
    p = _PENDING.pop(checkpoint)
    return {**p, "state": "rolled_back", "devices": res}


# ---------------------------------------------------------------- storage
def _lsblk_tree(dev: str) -> dict:
    cp = proc.run(["/usr/bin/lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,SERIAL,MODEL,RM",
                   f"/dev/{dev}"])
    data = json.loads(cp.stdout)["blockdevices"]
    if not data:
        raise v.ValidationError("device", "device not found")
    return data[0]


def _walk(node: dict):
    yield node
    for c in node.get("children", []) or []:
        yield from _walk(c)


def _pv_devices() -> Dict[str, str]:
    if not proc.which("/usr/sbin/pvs"):
        return {}
    cp = proc.run(["/usr/sbin/pvs", "--reportformat", "json", "-o", "pv_name,vg_name"], check=False)
    try:
        rows = json.loads(cp.stdout)["report"][0]["pv"]
    except (ValueError, KeyError, IndexError):
        return {}
    return {r["pv_name"]: r["vg_name"] for r in rows}


def disk_usage_check(dev: str) -> dict:
    """Describe why a disk is (not) safe to format."""
    dev = v.string(dev, "device", 3, 16, v.RE_DEVNAME)
    tree = _lsblk_tree(dev)
    pvs = _pv_devices()
    reasons = []
    for n in _walk(tree):
        if n.get("mountpoint"):
            reasons.append(f"{n['path']} is mounted at {n['mountpoint']}")
        if n.get("path") in pvs:
            reasons.append(f"{n['path']} belongs to LVM volume group {pvs[n['path']] or '(none)'}")
        if n.get("fstype") == "swap":
            reasons.append(f"{n['path']} is a swap device")
        if n.get("type") in ("raid1", "raid5", "md") or n.get("fstype") == "linux_raid_member":
            reasons.append(f"{n['path']} is part of a RAID array")
    existing = [f"{n['path']} ({n.get('fstype')})" for n in _walk(tree) if n.get("fstype")]
    return {"device": dev, "path": f"/dev/{dev}", "size": int(tree.get("size") or 0), "model": tree.get("model"),
            "serial": tree.get("serial"), "in_use": bool(reasons), "reasons": reasons, "existing_data": existing}


def smart(dev: str) -> dict:
    dev = v.string(dev, "device", 3, 16, v.RE_DEVNAME)
    if not proc.which("/usr/sbin/smartctl"):
        return {"available": False, "message": "smartmontools not installed"}
    cp = proc.run(["/usr/sbin/smartctl", "-j", "-a", f"/dev/{dev}"], check=False, timeout=30)
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        return {"available": False, "message": "smartctl returned no data"}
    st = data.get("smart_status", {})
    attrs = []
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        attrs.append({"id": a.get("id"), "name": a.get("name"), "value": a.get("value"), "worst": a.get("worst"),
                      "thresh": a.get("thresh"), "raw": a.get("raw", {}).get("string"),
                      "failing": a.get("when_failed") not in (None, "")})
    nvme = data.get("nvme_smart_health_information_log")
    msgs = [m.get("string") for m in data.get("smartctl", {}).get("messages", [])]
    return {
        "available": "smart_status" in data or bool(nvme), "passed": st.get("passed"),
        "model": data.get("model_name"), "serial": data.get("serial_number"), "firmware": data.get("firmware_version"),
        "temperature": data.get("temperature", {}).get("current"), "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "power_cycles": data.get("power_cycle_count"), "rotation_rate": data.get("rotation_rate"),
        "attributes": attrs, "nvme": nvme, "messages": msgs,
    }


def lvm_report() -> dict:
    out = {"vgs": [], "lvs": []}
    if not proc.which("/usr/sbin/vgs"):
        return out
    try:
        out["vgs"] = json.loads(proc.run(["/usr/sbin/vgs", "--reportformat", "json", "--units", "b", "--nosuffix",
                                          "-o", "vg_name,vg_size,vg_free,pv_count,lv_count"]).stdout)["report"][0]["vg"]
        out["lvs"] = json.loads(proc.run(["/usr/sbin/lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
                                          "-o", "lv_name,vg_name,lv_size,lv_attr,pool_lv,data_percent"]).stdout)["report"][0]["lv"]
    except (proc.CommandError, ValueError, KeyError, IndexError):
        pass
    return out


def _partition_path(dev: str) -> str:
    return f"/dev/{dev}p1" if re.search(r"\d$", dev) else f"/dev/{dev}1"


def create_datastore(dev: str, kind: str, name: str, confirm: str) -> dict:
    """DESTRUCTIVE: erase ``dev`` and create a datastore on it.

    ``confirm`` must equal the full device path typed by the administrator.
    """
    info = disk_usage_check(dev)
    dev = info["device"]
    kind = v.choice(kind, "kind", ("xfs", "ext4", "lvm"))
    name = v.name(name)
    if confirm != info["path"]:
        raise v.ValidationError("confirm", f"type {info['path']} to confirm erasing the disk")
    if info["in_use"]:
        raise v.ValidationError("device", "disk is in use: " + "; ".join(info["reasons"]))
    path = f"/dev/{dev}"
    proc.run(["/usr/sbin/wipefs", "-a", path], timeout=60)
    if kind == "lvm":
        vg = f"lsx_{name}".replace(".", "_").replace("-", "_")
        proc.run(["/usr/sbin/pvcreate", "-y", "-ff", path], timeout=60)
        proc.run(["/usr/sbin/vgcreate", vg, path], timeout=60)
        return {"kind": "lvm", "vg": vg, "device": path}
    proc.run(["/usr/sbin/parted", "-s", "-a", "optimal", path, "mklabel", "gpt", "mkpart", "lsxve", "1MiB", "100%"],
             timeout=60)
    proc.run(["/usr/sbin/partprobe", path], check=False, timeout=30)
    proc.run(["/usr/bin/udevadm", "settle", "--timeout=30"], check=False, timeout=40)
    part = _partition_path(dev)
    label = name[:12] if kind == "xfs" else name[:16]
    if kind == "xfs":
        proc.run(["/usr/sbin/mkfs.xfs", "-f", "-L", label, part], timeout=600)
    else:
        proc.run(["/usr/sbin/mkfs.ext4", "-F", "-L", label, part], timeout=1800)
    uuid = proc.run(["/usr/sbin/blkid", "-s", "UUID", "-o", "value", part]).stdout.strip()
    mnt = DATASTORE_ROOT / name
    mnt.mkdir(parents=True, exist_ok=True)
    with open("/etc/fstab", "a") as fh:
        fh.write(f"UUID={uuid} {mnt} {kind} defaults,nofail,x-systemd.device-timeout=30 0 0  # lsxve datastore {name}\n")
    proc.run([SYSTEMCTL, "daemon-reload"], check=False)
    proc.run(["/usr/bin/mount", str(mnt)], timeout=60)
    if proc.which("/usr/sbin/restorecon"):
        proc.run(["/usr/sbin/restorecon", "-R", str(mnt)], check=False, timeout=120)
    return {"kind": kind, "path": str(mnt), "device": part, "uuid": uuid}


def mounts() -> List[dict]:
    cp = proc.run(["/usr/bin/findmnt", "-J", "-b", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED,AVAIL",
                   "-t", "xfs,ext4,ext3,nfs,nfs4,vfat,btrfs"], check=False)
    try:
        rows = json.loads(cp.stdout)["filesystems"]
    except (ValueError, KeyError):
        return []
    flat = []

    def walk(nodes):
        for n in nodes:
            flat.append({k: n.get(k) for k in ("target", "source", "fstype", "options", "size", "used", "avail")})
            walk(n.get("children", []) or [])
    walk(rows)
    return flat
