"""lsxve-agent: the only privileged LSX-VE process.

Listens on a Unix socket (/run/lsxve/agent.sock, mode 0660, group lsxve) and
executes a *fixed* set of operations. Requests are JSON lines:
    {"id": 1, "op": "host.set_hostname", "args": {"hostname": "esx01"}}
The peer's UID is verified with SO_PEERCRED: only root and the lsxve service
account are served. There is no generic "run command" operation.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import socket
import socketserver
import struct
import sys
import threading
import traceback

from .. import __version__
from ..common import config, log, proc
from ..common.validate import ValidationError
from ..ct import lxc
from ..host import hostcfg, mgmtbridge, ops, vmio, vswitch

LOG = log.setup("lsxve.agent")

OPS = {
    "ping": lambda: {"version": __version__},
    "host.set_hostname": ops.set_hostname,
    "host.get_time": ops.get_time,
    "host.set_timezone": ops.set_timezone,
    "host.list_timezones": ops.list_timezones,
    "host.set_ntp": ops.set_ntp,
    "host.get_dns": ops.get_dns,
    "host.set_dns": ops.set_dns,
    "host.list_services": ops.list_services,
    "host.service_action": ops.service_action,
    "host.get_ssh": ops.get_ssh,
    "host.set_ssh": ops.set_ssh,
    "host.power": ops.power,
    "host.set_root_password": ops.set_root_password,
    "firewall.status": ops.firewall_status,
    "firewall.change": ops.firewall_change,
    "network.state": ops.network_state,
    "network.apply": ops.network_apply,
    "network.pending": ops.network_pending,
    "network.confirm": ops.network_confirm,
    "network.rollback": ops.network_rollback,
    "storage.smart": ops.smart,
    "storage.disk_check": ops.disk_usage_check,
    "storage.create_datastore": ops.create_datastore,
    "storage.lvm": ops.lvm_report,
    "storage.mounts": ops.mounts,
    "network.bridge_status": mgmtbridge.status,
    "network.bridge_retry": mgmtbridge.retry,
    "network.vswitch_backing_create": vswitch.ensure_backing,
    "network.vswitch_backing_remove": vswitch.remove_backing,
    "vmio.read_ovf": vmio.read_ovf,
    "vmio.image_info": vmio.image_info,
    "vmio.extract_ova": vmio.extract_ova,
    "vmio.convert": vmio.convert,
    "vmio.pack_ova": vmio.pack_ova,
    "vmio.cleanup": vmio.cleanup,
    "host.get_syslog": hostcfg.get_syslog,
    "host.set_syslog": hostcfg.set_syslog,
    "host.get_snmp": hostcfg.get_snmp,
    "host.set_snmp": hostcfg.set_snmp,
    "host.get_power_policy": hostcfg.get_power_policy,
    "host.set_power_policy": hostcfg.set_power_policy,
    "host.get_lockdown": hostcfg.get_lockdown,
    "host.set_lockdown": hostcfg.set_lockdown,
    "host.support_info": hostcfg.support_info,
    "host.support_bundle": hostcfg.support_bundle,
    "ct.status": lxc.status,
    "ct.list": lxc.list_containers,
    "ct.get": lxc.get,
    "ct.images": lxc.images,
    "ct.create": lxc.create,
    "ct.action": lxc.action,
    "ct.update": lxc.update,
    "ct.delete": lxc.delete,
    "ct.clone": lxc.clone,
    "ct.snapshots": lxc.snapshots,
    "ct.snapshot_create": lxc.snapshot_create,
    "ct.snapshot_restore": lxc.snapshot_restore,
    "ct.snapshot_delete": lxc.snapshot_delete,
}
# operations whose result carries a file descriptor (sent with SCM_RIGHTS on the same message)
FD_OPS = {"ct.console": lxc.open_console}
READ_ONLY = {"ping", "network.bridge_status", "ct.status", "ct.list", "ct.get", "ct.images", "ct.snapshots",
             "vmio.read_ovf", "vmio.image_info", "host.get_syslog", "host.get_snmp", "host.get_power_policy",
             "host.get_lockdown", "host.support_info"}
# long-running file work on unique (O_EXCL) paths: logged, but not serialised behind host changes
CONCURRENT = {"vmio.extract_ova", "vmio.convert", "vmio.pack_ova", "vmio.cleanup", "host.support_bundle"}
# container operations run concurrently with each other (per-container work, possibly long downloads),
# but never two at once for the same container
_ct_locks: dict = {}
_ct_locks_guard = threading.Lock()


def _ct_lock(name) -> threading.Lock:
    with _ct_locks_guard:
        return _ct_locks.setdefault(str(name), threading.Lock())

# operations that change state are logged at info level with their (non-secret) args
_SECRET_ARGS = {"password", "root_password", "community"}
_lock = threading.Lock()  # serialise mutating operations


def _redact(v):
    if isinstance(v, dict):
        return {k: ("***" if k in _SECRET_ARGS else _redact(x)) for k, x in v.items()}
    return v


def _allowed_uids() -> set:
    uids = {0}
    try:
        uids.add(pwd.getpwnam("lsxve").pw_uid)
    except KeyError:
        pass
    return uids


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        if uid not in _allowed_uids():
            LOG.warning("rejected agent connection from uid %s pid %s", uid, pid)
            return
        for raw in self.rfile:
            if len(raw) > 1024 * 1024:
                break
            req = {}
            try:
                req = json.loads(raw)
                rid = req.get("id")
                op = req.get("op")
                args = req.get("args") or {}
                if (op not in OPS and op not in FD_OPS) or not isinstance(args, dict):
                    raise ValidationError("op", f"unknown operation {op!r}")
                safe_args = {k: ("***" if k in _SECRET_ARGS else _redact(v)) for k, v in args.items()}
                if op in FD_OPS:
                    log.kv(LOG, 20, "operation", op=op, args=safe_args, uid=uid)
                    result, fd = FD_OPS[op](**args)
                    try:
                        line = (json.dumps({"id": rid, "ok": True, "result": result, "fd": True}) + "\n").encode()
                        socket.send_fds(self.request, [line], [fd])
                    finally:
                        os.close(fd)
                    continue
                mutating = not op.endswith((".state", ".status", ".pending", ".get_time", ".get_dns", ".get_ssh",
                                            ".list_services", ".list_timezones", ".smart", ".disk_check", ".lvm",
                                            ".mounts")) and op not in READ_ONLY
                if op in CONCURRENT:
                    log.kv(LOG, 20, "operation", op=op, args=safe_args, uid=uid)
                    result = OPS[op](**args)
                elif mutating and op.startswith("ct."):
                    log.kv(LOG, 20, "operation", op=op, args=safe_args, uid=uid)
                    target = args.get("name") or (args.get("spec") or {}).get("name", "")
                    with _ct_lock(target):
                        result = OPS[op](**args)
                elif mutating:
                    log.kv(LOG, 20, "operation", op=op, args=safe_args, uid=uid)
                    with _lock:
                        result = OPS[op](**args)
                else:
                    result = OPS[op](**args)
                resp = {"id": rid, "ok": True, "result": result}
            except ValidationError as e:
                resp = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False,
                        "error": {"type": "validation", "field": e.field, "message": e.message}}
            except lxc.CtError as e:
                resp = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False,
                        "error": {"type": e.kind, "message": e.message}}
            except proc.CommandError as e:
                resp = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False, "error": {"type": "command", "message": str(e)}}
            except TypeError as e:
                resp = {"id": None, "ok": False, "error": {"type": "validation", "field": "args", "message": str(e)}}
            except Exception as e:
                LOG.error("operation failed: %s\n%s", e, traceback.format_exc())
                resp = {"id": None, "ok": False, "error": {"type": "internal", "message": str(e)}}
            self.wfile.write((json.dumps(resp, default=str) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _startup() -> None:
    """Once per agent start: bridged management network first (VMs and containers attach to it), then
    containers marked for autostart."""
    try:
        with _lock:
            mgmtbridge.ensure()
    except Exception as e:  # noqa: BLE001 - never take the agent down
        LOG.error("bridged networking setup failed: %s", e)
    lxc.autostart_all()


def main() -> None:
    if os.geteuid() != 0:
        print("lsxve-agent must run as root", file=sys.stderr)
        sys.exit(1)
    path = str(config.AGENT_SOCKET)
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        gid = grp.getgrnam("lsxve").gr_gid
    except KeyError:
        gid = 0
    os.chown(config.RUN_DIR, 0, gid)
    os.chmod(config.RUN_DIR, 0o750)
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o117)
    srv = Server(path, Handler)
    os.umask(old)
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    LOG.info("lsxve-agent %s listening on %s", __version__, path)
    threading.Thread(target=_startup, name="startup", daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
