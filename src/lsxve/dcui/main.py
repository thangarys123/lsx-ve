"""lsxve-dcui: LSX-VE local text console (runs as root on tty1).

The status screen shows the product, hostname, management URL, addresses, NIC
state, CPU / memory and hypervisor health. <F2> opens the customisation menu
after re-authenticating with the root password; <F12> offers restart/shut down.
Like the ESXi console, kernel messages never scribble over this screen: they are
redirected to tty12 (Alt+F12) and the live system log runs on tty11 (Alt+F11).
All host changes go through lsxve-agent (the same validated operations the
web console uses); network changes use NetworkManager checkpoints and revert
automatically unless confirmed.
"""
from __future__ import annotations

import curses
import hmac
import json
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from .. import PRODUCT, __version__
from ..agent.client import AgentClient, AgentError
from ..common import config, proc, validate as v
from ..host import hardware, hostcfg, mgmtbridge

AGENT = AgentClient(timeout=120)
SYSTEMCTL = "/usr/bin/systemctl"
ADMIN_CLI = "/usr/sbin/lsxve-admin"
REFRESH = 5.0
KMSG_VT = 12          # kernel console messages (Alt+F12), like the ESXi vmkernel log screen
TIOCLINUX = 0x541C
TIOCL_SETKMSGREDIRECT = 11
TIOCL_UNBLANKSCREEN = 4
TIOCL_BLANKSCREEN = 14  # blank until TIOCL_UNBLANKSCREEN (key presses alone do not unblank)
FB_BLANK_UNBLANK, FB_BLANK_POWERDOWN = 0, 4

# colour pairs
C_HEAD, C_BODY, C_KEY, C_OK, C_WARN, C_BAD, C_SEL, C_DIM, C_BOX = range(1, 10)


# ----------------------------------------------------------------- data collection
def _unit_active(unit: str) -> str:
    try:
        return proc.run([SYSTEMCTL, "is-active", unit], check=False, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _vm_counts() -> Optional[Tuple[int, int]]:
    try:
        import libvirt  # type: ignore
        libvirt.registerErrorHandler(lambda *a: None, None)
        c = libvirt.openReadOnly(config.load()["libvirt_uri"])
        try:
            doms = c.listAllDomains(0)
            return sum(1 for d in doms if d.isActive()), len(doms)
        finally:
            c.close()
    except Exception:
        return None


def _login_hint() -> Optional[str]:
    """Until the first web login, say which account to use (read-only look at the API database)."""
    if (config.ETC_DIR / "initial-admin-password").exists():
        return f"First login: user 'admin', password in {config.ETC_DIR / 'initial-admin-password'}"
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{config.DB_FILE}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute("SELECT username, last_login FROM users").fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - database not created yet or locked
        if (config.ETC_DIR / "firstboot-admin.json").exists():
            return "First login: user 'admin' with the root password set during installation"
        return None
    if rows and not any(r[1] for r in rows) and any(r[0] == "admin" for r in rows):
        return "First login: user 'admin' with the root password set during installation"
    return None


def collect() -> dict:
    mg = hardware.management_addresses()
    cpu = hardware.cpu_info()
    mem = hardware.memory_info()
    kvm = hardware.kvm_status()
    nics = [n for n in hardware.physical_nics() if n["kind"] in ("physical", "bridge", "bond", "vlan")]
    return {
        "hostname": hardware.hostname(), "mg": mg, "cpu": cpu, "mem": mem, "kvm": kvm, "nics": nics,
        "uptime": hardware.uptime(),
        "services": {u: _unit_active(u) for u in ("lsxve-api.service", "lsxve-agent.service", "virtqemud.service")},
        "vms": _vm_counts(),
        "hint": _login_hint(),
        "bridge": _bridge_state(),
        "lockdown": hostcfg.lockdown_mode(),
        "maintenance": _maintenance(),
    }


def _maintenance() -> bool:
    try:
        return bool(json.loads(config.MAINTENANCE_FILE.read_text()).get("enabled"))
    except (OSError, ValueError):
        return False


def _bridge_state() -> dict:
    try:
        return mgmtbridge.status()
    except Exception:  # noqa: BLE001
        return {"status": "unknown"}


def mgmt_url(mg: dict) -> str:
    if mg["ipv4"]:
        return f"https://{mg['ipv4'][0].split('/')[0]}/"
    if mg["ipv6"]:
        return f"https://[{mg['ipv6'][0].split('/')[0]}]/"
    return ""


# ----------------------------------------------------------------- root password
def verify_root_password(pw: str) -> Tuple[bool, str]:
    try:
        import crypt  # noqa: deprecated in 3.11+, available on Rocky 9 (3.9)
        import spwd
        h = spwd.getspnam("root").sp_pwdp
    except (ImportError, KeyError, PermissionError) as e:
        return False, f"cannot verify password ({e})"
    if not h or h[0] in "!*":
        return False, "the root account is locked; set a root password from a login shell (Alt+F2)"
    return hmac.compare_digest(crypt.crypt(pw, h) or "", h), "invalid password"


# ----------------------------------------------------------------- UI helpers
class UI:
    def __init__(self, scr):
        self.scr = scr
        curses.curs_set(0)
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        curses.init_pair(C_HEAD, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_BODY, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(C_KEY, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(C_OK, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(C_BAD, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_DIM, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(C_BOX, curses.COLOR_WHITE, curses.COLOR_BLUE)
        scr.keypad(True)

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, max(0, x), text, max(0, w - max(0, x) - 1), attr)
            except curses.error:
                pass

    def fill(self, y: int, attr: int) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h:
            self.put(y, 0, " " * (w - 1), attr)

    def frame(self, title: str, footer: str) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.fill(0, curses.color_pair(C_HEAD) | curses.A_BOLD)
        self.put(0, 2, f"{PRODUCT} {__version__}  |  {title}", curses.color_pair(C_HEAD) | curses.A_BOLD)
        self.fill(h - 1, curses.color_pair(C_HEAD))
        self.put(h - 1, 2, footer, curses.color_pair(C_HEAD))

    def message(self, title: str, lines: List[str], attr_key: int = C_BODY) -> None:
        while True:
            self.frame(title, "<Enter> OK")
            for i, l in enumerate(lines[: self.scr.getmaxyx()[0] - 4]):
                self.put(2 + i, 4, l, curses.color_pair(attr_key))
            self.scr.refresh()
            if self.scr.getch() in (10, 13, 27, curses.KEY_ENTER):
                return

    def confirm(self, title: str, lines: List[str], yes_key: str = "F11") -> bool:
        code = curses.KEY_F11 if yes_key == "F11" else ord("y")
        while True:
            self.frame(title, f"<{yes_key}> Confirm   <Esc> Cancel")
            for i, l in enumerate(lines):
                self.put(2 + i, 4, l, curses.color_pair(C_BODY))
            self.scr.refresh()
            k = self.scr.getch()
            if k == code:
                return True
            if k == 27:
                return False

    def input(self, title: str, prompt: str, initial: str = "", secret: bool = False, hint: str = "") -> Optional[str]:
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                self.frame(title, "<Enter> Accept   <Esc> Cancel")
                self.put(3, 4, prompt, curses.color_pair(C_BODY) | curses.A_BOLD)
                shown = ("*" * len(buf)) if secret else "".join(buf)
                self.put(5, 4, "[" + shown.ljust(48)[:60] + "]", curses.color_pair(C_SEL))
                if hint:
                    self.put(7, 4, hint, curses.color_pair(C_DIM))
                self.scr.move(5, 5 + min(len(shown), 59))
                self.scr.refresh()
                k = self.scr.get_wch()
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif isinstance(k, str) and k.isprintable() and len(buf) < 255:
                    buf.append(k)
        finally:
            curses.curs_set(0)

    def menu(self, title: str, items: List[Tuple[str, str]], info: Callable[[int], List[str]] = None,
             sel: int = 0) -> Optional[int]:
        while True:
            self.frame(title, "<Up/Down> Select   <Enter> Open   <Esc> Back")
            h, w = self.scr.getmaxyx()
            for i, (label, _) in enumerate(items):
                attr = curses.color_pair(C_SEL) if i == sel else curses.color_pair(C_BODY)
                self.put(2 + i, 2, f" {label:<38}", attr)
            desc = items[sel][1]
            self.put(2, 44, desc, curses.color_pair(C_DIM))
            if info:
                for j, l in enumerate(info(sel)):
                    self.put(4 + j, 44, l, curses.color_pair(C_BODY))
            self.scr.refresh()
            k = self.scr.getch()
            if k in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(items)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(items)
            elif k in (10, 13, curses.KEY_ENTER):
                return sel
            elif k == 27:
                return None

    def pager(self, title: str, text: str) -> None:
        lines = text.splitlines() or ["(empty)"]
        top = max(0, len(lines) - 1)
        while True:
            h, w = self.scr.getmaxyx()
            page = h - 3
            top = max(0, min(top, len(lines) - page))
            self.frame(title, f"<Up/Down/PgUp/PgDn> Scroll   <Esc> Back      line {top + 1}/{len(lines)}")
            for i, l in enumerate(lines[top: top + page]):
                self.put(1 + i, 1, l, curses.color_pair(C_BODY))
            self.scr.refresh()
            k = self.scr.getch()
            if k == curses.KEY_UP:
                top -= 1
            elif k == curses.KEY_DOWN:
                top += 1
            elif k == curses.KEY_PPAGE:
                top -= page
            elif k == curses.KEY_NPAGE:
                top += page
            elif k == curses.KEY_HOME:
                top = 0
            elif k == curses.KEY_END:
                top = len(lines)
            elif k in (27, 10, 13, ord("q")):
                return

    def busy(self, title: str, text: str) -> None:
        self.frame(title, "Please wait ...")
        self.put(3, 4, text, curses.color_pair(C_BODY))
        self.scr.refresh()


# ----------------------------------------------------------------- status screen
def draw_status(ui: UI, st: dict) -> None:
    scr = ui.scr
    h, w = scr.getmaxyx()
    ui.frame("Direct Console", "<F2> Customize System/View Logs   <F12> Shut Down/Restart   "
                               "Alt+F11 system log   Alt+F12 kernel log")
    B, D = curses.color_pair(C_BODY), curses.color_pair(C_DIM)
    y = 2
    ui.put(y, 4, f"{PRODUCT} - Linux Server eXtended Virtualization Environment {__version__}", B | curses.A_BOLD)
    y += 1
    cpu, mem = st["cpu"], st["mem"]
    ui.put(y, 4, f"{cpu.get('model') or 'Unknown CPU'}", D)
    y += 1
    ui.put(y, 4, f"{cpu.get('sockets', 1)} socket(s), {cpu.get('cores', '?')} cores, {cpu.get('threads', '?')} threads"
                 f"   |   {mem.get('total', 0) / 1024 ** 3:.1f} GiB memory", D)
    y += 2
    url = mgmt_url(st["mg"])
    ui.put(y, 4, "To manage this host, browse to:", B)
    y += 1
    if url:
        ui.put(y, 6, url, curses.color_pair(C_KEY) | curses.A_BOLD)
        if st.get("hint"):
            y += 1
            ui.put(y, 6, st["hint"], D)
    else:
        ui.put(y, 6, "No management address - press F2 and configure the management network", curses.color_pair(C_BAD))
    y += 2
    ui.put(y, 4, f"Hostname:  {st['hostname']}", B)
    y += 1
    mg = st["mg"]
    ui.put(y, 4, f"Interface: {mg['interface'] or '-'}    Gateway: {mg['gateway'] or '-'}", B)
    y += 1
    ui.put(y, 4, f"IPv4:      {', '.join(mg['ipv4']) or '(none)'}", B)
    y += 1
    for a in mg["ipv6"][:2]:
        ui.put(y, 4, f"IPv6:      {a}", B)
        y += 1
    y += 1
    ui.put(y, 4, "Network adapters", B | curses.A_BOLD)
    y += 1
    for n in st["nics"][: max(1, h - y - 9)]:
        link = "up" if n["carrier"] else "down"
        sp = f"{n['speed_mbps']} Mb/s" if n.get("speed_mbps") else ""
        attr = curses.color_pair(C_OK if n["carrier"] else C_WARN)
        extra = f"member of {n['master']}" if n.get("master") else n["kind"]
        ui.put(y, 6, f"{n['name']:<14} {n['mac']:<18} link {link:<5} {sp:<11} {extra}", attr)
        y += 1
    y += 1
    kvm = st["kvm"]
    virt = cpu.get("virtualization")
    if kvm.get("dev_kvm"):
        ui.put(y, 4, f"Hypervisor: KVM ({virt}) available", curses.color_pair(C_OK))
    elif virt:
        ui.put(y, 4, f"Hypervisor: {virt} detected but /dev/kvm missing - check firmware settings",
               curses.color_pair(C_BAD))
    else:
        ui.put(y, 4, "Hypervisor: no hardware virtualization - VMs run in software emulation", curses.color_pair(C_WARN))
    y += 1
    br = st.get("bridge") or {}
    bs = br.get("status")
    if bs == "done" and br.get("bridge_present"):
        port = f", port {br['port']}" if br.get("port") else ""
        ui.put(y, 4, f"VM network: bridged ({br.get('bridge')}{port}) - VMs get addresses from the physical LAN",
               curses.color_pair(C_OK))
    elif bs in ("failed", "skipped"):
        ui.put(y, 4, f"VM network: not bridged - {br.get('reason', bs)}"[:w - 6] + "  (F2 to retry)",
               curses.color_pair(C_BAD))
    elif bs == "disabled":
        ui.put(y, 4, "VM network: bridging disabled in /etc/lsxve/lsxve.conf", D)
    else:
        ui.put(y, 4, "VM network: setting up bridge...", curses.color_pair(C_WARN))
    y += 1
    svc = st["services"]
    parts = []
    for unit, label in (("virtqemud.service", "libvirt"), ("lsxve-agent.service", "agent"), ("lsxve-api.service", "web")):
        parts.append(f"{label}: {svc.get(unit)}")
    bad = any(svc.get(u) != "active" for u in ("lsxve-api.service", "lsxve-agent.service"))
    ui.put(y, 4, "Services:   " + "   ".join(parts), curses.color_pair(C_WARN if bad else C_OK))
    y += 1
    vms = st["vms"]
    up = st["uptime"]
    ui.put(y, 4, f"VMs:        {f'{vms[0]} running / {vms[1]} total' if vms else 'unavailable'}"
                 f"     Uptime: {int(up // 86400)}d {int(up % 86400 // 3600)}h {int(up % 3600 // 60)}m", B)
    y += 1
    flags = []
    if st.get("maintenance"):
        flags.append("MAINTENANCE MODE (VMs cannot be powered on)")
    if st.get("lockdown") not in (None, "disabled"):
        flags.append(f"Lockdown mode: {st['lockdown']}")
    if flags and y < h - 1:
        ui.put(y, 4, "   ".join(flags), curses.color_pair(C_WARN) | curses.A_BOLD)
    scr.refresh()


# ----------------------------------------------------------------- actions
def _agent(ui: UI, title: str, op: str, **args):
    try:
        return AGENT.call(op, **args)
    except AgentError as e:
        ui.message(title, [f"{'Invalid ' + e.field + ': ' if e.field else ''}{e.message}"], C_BAD)
    except Exception as e:  # noqa
        ui.message(title, [f"Error: {e}"], C_BAD)
    return None


def bridge_network(ui: UI) -> None:
    T = "Bridged VM Network"
    st = _agent(ui, T, "network.bridge_status")
    if st is None:
        return
    lines = [f"Status:  {st.get('status')}", f"Bridge:  {st.get('bridge') or '-'}"
             f"{' (present)' if st.get('bridge_present') else ''}", f"Port:    {st.get('port') or '-'}"]
    if st.get("reason"):
        lines.append(f"Reason:  {st['reason']}")
    lines += ["", "VMs and containers on network \"default\" are connected to this bridge and get",
              "their addresses from the physical network (no NAT)."]
    if st.get("status") == "done" and st.get("bridge_present"):
        ui.message(T, lines)
        return
    lines += ["", "Retry now? The management connection is interrupted for a few seconds.",
              "If the host loses its address or gateway, the change is rolled back automatically."]
    if not ui.confirm(T, lines):
        return
    try:
        res = AGENT.call("network.bridge_retry", timeout=420)
    except Exception as e:  # noqa: BLE001
        ui.message(T, [f"Error: {getattr(e, 'message', e)}"], C_BAD)
        return
    ok = res.get("status") == "done"
    ui.message(T, [f"Result: {res.get('status')}", res.get("reason") or
                   f"Bridge {res.get('bridge')} active: {', '.join(res.get('addresses') or [])}"], C_OK if ok else C_BAD)


def configure_network(ui: UI) -> None:
    T = "Configure Management Network"
    state = _agent(ui, T, "network.state")
    if not state:
        return
    # bridge/bond ports carry no IP configuration of their own
    conns = [c for c in state["connections"] if c["type"] in ("802-3-ethernet", "bridge", "bond", "vlan")
             and not c.get("master")]
    if not conns:
        ui.message(T, ["No configurable network connections were found."])
        return
    mg = hardware.management_addresses()
    pre = next((i for i, c in enumerate(conns) if c.get("device") == mg["interface"]), 0)

    def info(i):
        c = conns[i]
        ip4 = c.get("ipv4", {})
        return [f"Device:   {c.get('device') or '(inactive)'}", f"Type:     {c['type']}",
                f"IPv4:     {ip4.get('method')} {', '.join(ip4.get('addresses', []))}",
                f"Gateway:  {ip4.get('gateway') or '-'}", f"DNS:      {', '.join(ip4.get('dns', [])) or '-'}",
                "", "Marked * = carries the current management address"]

    items = [(("* " if c.get("device") == mg["interface"] else "  ") + c["name"][:34], "IPv4 settings for this connection")
             for c in conns]
    i = ui.menu(T, items, info, pre)
    if i is None:
        return
    c = conns[i]
    mode = ui.menu(T, [("Use DHCP", "Obtain address automatically"), ("Set static IPv4 address", "Manual configuration")])
    if mode is None:
        return
    change = {"op": "configure", "uuid": c["uuid"]}
    if mode == 0:
        change["ipv4"] = {"method": "auto", "dns": []}
    else:
        cur = c.get("ipv4", {})
        try:
            addr = ui.input(T, "IPv4 address / prefix", (cur.get("addresses") or [""])[0], hint="example: 192.168.10.20/24")
            if addr is None:
                return
            addr = v.ipv4_interface(addr.strip(), "address")
            gw = ui.input(T, "Default gateway (blank for none)", cur.get("gateway") or "")
            if gw is None:
                return
            gw = v.ip_address(gw.strip(), "gateway", 4) if gw.strip() else ""
            dns = ui.input(T, "DNS servers (comma separated)", ",".join(cur.get("dns", [])))
            if dns is None:
                return
            dns_l = [v.ip_address(x.strip(), "dns", 4) for x in dns.split(",") if x.strip()]
        except v.ValidationError as e:
            ui.message(T, [f"Invalid {e.field}: {e.message}"], C_BAD)
            return
        change["ipv4"] = {"method": "manual", "addresses": [addr], "gateway": gw, "dns": dns_l}
    if not ui.confirm(T, [f"Apply new IPv4 settings to '{c['name']}'?", "",
                          "Active web sessions to this host may be interrupted.",
                          "The change reverts automatically after 60 seconds unless you confirm it."]):
        return
    ui.busy(T, "Applying network configuration ...")
    pend = _agent(ui, T, "network.apply", change=change, rollback_seconds=60)
    if not pend:
        return
    ui.scr.timeout(1000)
    try:
        while True:
            left = int(pend["expires"] - time.time())
            if left <= 0:
                ui.scr.timeout(-1)
                ui.message(T, ["The change was not confirmed and has been rolled back."], C_WARN)
                return
            mg = hardware.management_addresses()
            ui.frame(T, "<F11> Keep settings   <Esc> Revert now")
            ui.put(3, 4, pend["summary"], curses.color_pair(C_BODY))
            ui.put(5, 4, f"New management address: {', '.join(mg['ipv4']) or '(none yet)'}", curses.color_pair(C_KEY))
            ui.put(7, 4, f"Keep these network settings? Reverting automatically in {left} s.",
                   curses.color_pair(C_WARN) | curses.A_BOLD)
            ui.scr.refresh()
            k = ui.scr.getch()
            if k == curses.KEY_F11:
                ui.scr.timeout(-1)
                if _agent(ui, T, "network.confirm", checkpoint=pend["checkpoint"]):
                    ui.message(T, ["Network configuration saved."], C_OK)
                return
            if k == 27:
                ui.scr.timeout(-1)
                if _agent(ui, T, "network.rollback", checkpoint=pend["checkpoint"]):
                    ui.message(T, ["Previous network configuration restored."], C_OK)
                return
    finally:
        ui.scr.timeout(-1)


def set_hostname(ui: UI) -> None:
    T = "Hostname"
    name = ui.input(T, "Host name (FQDN allowed)", hardware.hostname())
    if name is None:
        return
    if _agent(ui, T, "host.set_hostname", hostname=name.strip()):
        ui.message(T, [f"Hostname set to {name.strip()}."], C_OK)


def restart_management(ui: UI) -> None:
    T = "Restart Management Agents"
    if not ui.confirm(T, ["Restart the LSX-VE web console and host agent?", "",
                          "Virtual machines keep running. Web sessions will reconnect."]):
        return
    ui.busy(T, "Restarting lsxve-agent and lsxve-api ...")
    cp = proc.run([SYSTEMCTL, "restart", "lsxve-agent.service", "lsxve-api.service"], check=False, timeout=120)
    ui.message(T, ["Management services restarted." if cp.returncode == 0 else f"Restart failed: {cp.stderr.strip()}"],
               C_OK if cp.returncode == 0 else C_BAD)


def toggle_ssh(ui: UI) -> None:
    T = "SSH"
    if hostcfg.lockdown_mode() != "disabled":
        ui.message(T, ["The host is in lockdown mode: SSH stays disabled.",
                       "Disable lockdown mode first (Configure Lockdown Mode)."], C_WARN)
        return
    st = _agent(ui, T, "host.get_ssh")
    if st is None:
        return
    active = (st.get("service") or {}).get("active") == "active"
    i = ui.menu(T, [("Disable SSH" if active else "Enable SSH", f"SSH is currently {'running' if active else 'stopped'}")])
    if i is None:
        return
    if _agent(ui, T, "host.set_ssh", enabled=not active, permit_root_login=st.get("permit_root_login", "prohibit-password")):
        ui.message(T, [f"SSH {'disabled' if active else 'enabled'}."], C_OK)


def reset_web_admin(ui: UI) -> None:
    T = "Reset Web Administrator Password"
    user = ui.input(T, "Web console user name", "admin")
    if not user:
        return
    pw = ui.input(T, f"New password for {user}", secret=True,
                  hint="8+ characters, at least 3 of: upper, lower, digit, symbol")
    if pw is None:
        return
    if ui.input(T, "Repeat password", secret=True) != pw:
        ui.message(T, ["Passwords do not match."], C_BAD)
        return
    try:
        v.password(pw)
    except v.ValidationError as e:
        ui.message(T, [e.message], C_BAD)
        return
    cp = subprocess.run([ADMIN_CLI, "reset-password", user, "--stdin"], input=pw + "\n", capture_output=True,
                        text=True, timeout=60)
    ui.message(T, [(cp.stdout or cp.stderr).strip() or "done"], C_OK if cp.returncode == 0 else C_BAD)


def test_network(ui: UI) -> None:
    T = "Test Management Network"
    mg = hardware.management_addresses()
    targets = []
    if mg["gateway"]:
        targets.append(("gateway", mg["gateway"]))
    dns = _agent(ui, T, "host.get_dns") or {}
    for d in (dns.get("servers") or dns.get("effective") or [])[:2]:
        targets.append(("DNS", d))
    lines = [f"Hostname {hardware.hostname()}   interface {mg['interface'] or '-'}", ""]
    if not targets:
        lines.append("No gateway or DNS server configured.")
    for label, t in targets:
        ui.busy(T, f"Pinging {label} {t} ...")
        cp = proc.run(["/usr/bin/ping", "-c", "3", "-W", "2", t], check=False, timeout=20)
        ok = cp.returncode == 0
        summ = next((l for l in cp.stdout.splitlines() if "packets transmitted" in l), cp.stderr.strip())
        lines.append(f"{'OK  ' if ok else 'FAIL'}  {label:<8} {t:<40} {summ}")
    ui.message(T, lines)


def view_logs(ui: UI) -> None:
    T = "System Logs"
    choices = [("LSX-VE management (api/agent/init)", ["-u", "lsxve-api", "-u", "lsxve-agent", "-u", "lsxve-init"]),
               ("Hypervisor (libvirt / QEMU)", ["-u", "virtqemud", "-u", "virtnetworkd", "-u", "virtstoraged"]),
               ("Kernel messages", ["-k"]),
               ("Complete system journal (this boot)", [])]
    i = ui.menu(T, [(c[0], "journalctl, current boot") for c in choices])
    if i is None:
        return
    cp = proc.run(["/usr/bin/journalctl", "--no-pager", "-b", "-n", "2000", "-o", "short-iso", *choices[i][1]],
                  check=False, timeout=60)
    ui.pager(choices[i][0], cp.stdout or cp.stderr)


def diagnostics(ui: UI) -> None:
    T = "Diagnostics"
    ui.busy(T, "Collecting diagnostics ...")
    out = [f"{PRODUCT} {__version__}", ""]
    try:
        snap = json.loads((config.RUN_DIR / "host.json").read_text())
        out.append(f"Initialised at boot: {time.ctime(snap.get('initialised', 0))}")
    except (OSError, ValueError):
        out.append("lsxve-init has not run this boot")
    out += ["", "== Services =="]
    for u in ("lsxve-init", "lsxve-agent", "lsxve-api", "virtqemud", "virtnetworkd", "virtstoraged", "virtlogd",
              "NetworkManager", "firewalld", "chronyd", "sshd"):
        out.append(f"  {u:<16} {_unit_active(u + '.service')}")
    out += ["", "== Hardware virtualization (virt-host-validate qemu) =="]
    if proc.which("/usr/bin/virt-host-validate"):
        cp = proc.run(["/usr/bin/virt-host-validate", "qemu"], check=False, timeout=60)
        out += (cp.stdout + cp.stderr).splitlines()
    else:
        out.append("  virt-host-validate not installed")
    out += ["", "== Failed units =="]
    cp = proc.run([SYSTEMCTL, "--failed", "--no-legend", "--plain", "--no-pager"], check=False, timeout=30)
    out += ["  " + l for l in cp.stdout.splitlines()] or ["  none"]
    out += ["", "== Listening management ports =="]
    fw = _agent(ui, T, "firewall.status")
    if fw:
        out.append(f"  firewalld: {'running' if fw.get('running') else 'stopped'}  services: {', '.join(fw.get('services', []))}")
    ui.pager(T, "\n".join(out))


def support_information(ui: UI) -> None:
    """ESXi "View Support Information": serial number, UUID and the TLS certificate thumbprint that users
    compare with what their browser shows on first connect."""
    try:
        i = hostcfg.support_info()
    except Exception as e:  # noqa: BLE001
        ui.message("Support Information", [f"Error: {e}"], C_BAD)
        return
    fp = i.get("cert_sha256") or "(no certificate yet)"
    lines = [f"Product:        {i['product']} {i['version']}",
             f"Hostname:       {i['hostname']}",
             f"Manufacturer:   {i.get('manufacturer') or '-'}",
             f"Model:          {i.get('model') or '-'}",
             f"Serial number:  {i.get('serial') or '-'}",
             f"System UUID:    {i.get('uuid') or '-'}",
             f"BIOS:           {i.get('bios') or '-'}",
             "",
             "Web console TLS certificate SHA-256 thumbprint:",
             "  " + fp[:48],
             "  " + fp[48:]]
    ui.pager("Support Information", "\n".join(lines))


def lockdown(ui: UI) -> None:
    T = "Lockdown Mode"
    mode = hostcfg.lockdown_mode()
    items = [("Disabled", "SSH and local console are available"),
             ("Normal", "SSH disabled and cannot be enabled; this console stays available"),
             ("Strict", "Also no local customization and no login terminals: only the web console")]
    i = ui.menu(f"{T} (currently {mode})", items)
    if i is None:
        return
    new = ("disabled", "normal", "strict")[i]
    if new == mode:
        return
    if new == "strict" and not ui.confirm(T, ["Strict lockdown disables this console's customization menu and",
                                              "all local login terminals. The host can then be managed only",
                                              "from the web console by an administrator. Continue?"]):
        return
    if _agent(ui, T, "host.set_lockdown", mode=new) is not None:
        ui.message(T, [f"Lockdown mode is now {new}."], C_OK)


def power_menu(ui: UI, authed: bool) -> None:
    if hostcfg.lockdown_mode() == "strict":
        ui.message("Shut Down / Restart", ["The host is in strict lockdown mode.",
                                           "Shut down or restart it from the web console."], C_WARN)
        return
    if not authed and not authenticate(ui):
        return
    i = ui.menu("Shut Down / Restart", [("Restart host", "Reboot the host"), ("Shut down host", "Power off the host")])
    if i is None:
        return
    action = ("reboot", "poweroff")[i]
    vms = _vm_counts()
    warn = f"{vms[0]} virtual machine(s) are running and will be shut down." if vms and vms[0] else "No VMs are running."
    if ui.confirm("Shut Down / Restart", [f"{'Restart' if i == 0 else 'Shut down'} {hardware.hostname()}?", "", warn]):
        ui.busy("Shut Down / Restart", "Requested ...")
        proc.run([SYSTEMCTL, action], check=False, timeout=30)
        time.sleep(30)


def authenticate(ui: UI) -> bool:
    for _ in range(3):
        pw = ui.input("Authentication Required", "Enter the root password", secret=True,
                      hint="Customization of this host requires the root password.")
        if pw is None:
            return False
        ok, why = verify_root_password(pw)
        if ok:
            return True
        ui.message("Authentication Required", [why], C_BAD)
        time.sleep(1)
    return False


def customize(ui: UI) -> None:
    if hostcfg.lockdown_mode() == "strict":
        i = ui.menu("Strict Lockdown Mode", [("View Support Information", "Serial number and certificate thumbprint"),
                                             ("View System Logs", "Journal of LSX-VE, hypervisor and kernel")],
                    info=lambda _i: ["Local customization is disabled.", "Manage this host from the web console;",
                                     "lockdown mode is changed under Host > Manage > Security."])
        if i == 0:
            support_information(ui)
        elif i == 1:
            if authenticate(ui):
                view_logs(ui)
        return
    if not authenticate(ui):
        return
    items = [("Configure Management Network", "IP address, gateway and DNS of the management interface"),
             ("Test Management Network", "Ping the gateway and DNS servers"),
             ("Bridged VM Network", "Status / retry moving the management NIC onto bridge br0"),
             ("Hostname", "Change the host name"),
             ("Restart Management Agents", "Restart the web console and host agent"),
             ("Enable / Disable SSH", "Remote shell access"),
             ("Reset Web Administrator Password", "Recover access to the web console"),
             ("Configure Lockdown Mode", "Restrict local and SSH access to the host"),
             ("View Support Information", "Serial number, UUID, certificate thumbprint"),
             ("View System Logs", "Journal of LSX-VE, hypervisor and kernel"),
             ("Diagnostics", "Service status and virtualization self-test"),
             ("Shut Down / Restart", "Reboot or power off the host")]
    actions = [configure_network, test_network, bridge_network, set_hostname, restart_management, toggle_ssh, reset_web_admin,
               lockdown, support_information, view_logs, diagnostics, lambda u: power_menu(u, True)]
    sel = 0
    while True:
        i = ui.menu("System Customization", items, sel=sel)
        if i is None:
            return
        sel = i
        actions[i](ui)


def blank_after_seconds() -> int:
    """Idle time after which the console display is switched off (0 = never). Laptops default to 10
    minutes: the panel otherwise stays lit forever because the host never suspends."""
    n = config.load().get("console_blank_minutes", -1)
    if n < 0:
        n = 10 if hardware.is_laptop() else 0
    return int(n) * 60


def set_display(on: bool) -> None:
    """Blank / unblank the text console and switch the panel backlight off / on."""
    import fcntl
    try:
        fcntl.ioctl(sys.stdin.fileno(), TIOCLINUX, bytes([TIOCL_UNBLANKSCREEN if on else TIOCL_BLANKSCREEN]))
    except OSError:
        pass
    for bl in hardware.backlights():
        try:
            with open(os.path.join(bl, "bl_power"), "w") as fh:
                fh.write(str(FB_BLANK_UNBLANK if on else FB_BLANK_POWERDOWN))
        except OSError:
            pass


def run(scr) -> None:
    ui = UI(scr)
    curses.raw()  # Ctrl+C / Ctrl+Z must not kill the console
    scr.timeout(int(REFRESH * 1000))
    st, last = None, 0.0
    blank_s, idle_since, blanked = blank_after_seconds(), time.time(), False
    set_display(True)  # a restarted DCUI must not leave the panel dark
    while True:
        if blanked:
            k = scr.getch()
            if k == -1:
                continue
            set_display(True)  # any key wakes the display; the key itself is swallowed
            blanked, idle_since, last = False, time.time(), 0
            scr.clearok(True)
            continue
        if blank_s and time.time() - idle_since >= blank_s:
            scr.erase()
            scr.refresh()
            set_display(False)
            blanked = True
            continue
        if time.time() - last >= REFRESH or st is None:
            try:
                st = collect()
            except Exception as e:  # never die on tty1
                st = st or {"hostname": "?", "mg": {"interface": None, "ipv4": [], "ipv6": [], "gateway": None},
                            "cpu": {}, "mem": {}, "kvm": {}, "nics": [], "uptime": 0, "services": {}, "vms": None}
                st["error"] = str(e)
            last = time.time()
            scr.clearok(True)  # full repaint: wipes anything another writer left on tty1
        draw_status(ui, st)
        k = scr.getch()
        if k != -1:
            idle_since = time.time()
        if k == curses.KEY_F2:
            scr.timeout(-1)
            customize(ui)
            scr.timeout(int(REFRESH * 1000))
            last = 0
        elif k == curses.KEY_F12:
            scr.timeout(-1)
            power_menu(ui, False)
            scr.timeout(int(REFRESH * 1000))
            last = 0
        if k in (curses.KEY_F2, curses.KEY_F12):
            idle_since = time.time()
        elif k == curses.KEY_RESIZE:
            pass


def redirect_kernel_messages(vt: int = KMSG_VT) -> bool:
    """Send kernel console output (printk) to tty<vt> instead of the console showing this UI."""
    import fcntl
    try:
        with open(f"/dev/tty{vt}", "w") as t:
            t.write("\x1b[2J\x1b[H")
            t.write(f"{PRODUCT} kernel log - kernel console messages appear here (Alt+F1 returns to the "
                    f"Direct Console; full history: F2 > View System Logs)\n\n")
        fcntl.ioctl(sys.stdin.fileno(), TIOCLINUX, bytes([TIOCL_SETKMSGREDIRECT, vt]))
        return True
    except OSError as e:  # not a VT (serial / ssh test run) or no CAP_SYS_ADMIN
        print(f"lsxve-dcui: kernel message redirect not applied: {e}", file=sys.stderr)
        return False


def main() -> None:
    os.environ.setdefault("TERM", "linux")
    os.environ.setdefault("ESCDELAY", "25")
    redirect_kernel_messages()
    while True:
        try:
            curses.wrapper(run)
        except KeyboardInterrupt:
            continue
        except Exception as e:  # restart the UI instead of leaving tty1 dead
            print(f"lsxve-dcui error: {e}", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    main()
