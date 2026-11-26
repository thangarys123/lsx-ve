"""Host settings applied by lsxve-agent (root): remote syslog, SNMP, power policy (tuned), lockdown mode,
support bundles. Configuration files are rendered from validated values; nothing user-supplied is passed
to a shell."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import ssl
import tarfile
import time
from pathlib import Path
from typing import List, Optional

from .. import __version__
from ..common import config, log, proc, validate as v
from . import hardware

LOG = log.get("lsxve.hostcfg")
SYSTEMCTL = "/usr/bin/systemctl"
FWCMD = "/usr/bin/firewall-cmd"
TUNED_ADM = "/usr/sbin/tuned-adm" if os.path.exists("/usr/sbin/tuned-adm") else "/usr/bin/tuned-adm"
RSYSLOG_CONF = Path("/etc/rsyslog.d/90-lsxve-remote.conf")
SYSLOG_JSON = config.ETC_DIR / "syslog.json"
SNMPD_CONF = Path("/etc/snmp/snmpd.conf")
SNMP_JSON = config.ETC_DIR / "snmp.json"
LOGIND_DROPIN = Path("/etc/systemd/logind.conf.d/90-lsxve-lockdown.conf")
RE_COMMUNITY = re.compile(r"^[A-Za-z0-9._~-]{6,32}$")
RE_TEXT = re.compile(r"^[^\x00-\x1f\"\\]{0,128}$")

# ESXi power policies -> tuned profiles
POWER_POLICIES = {
    "virtual-host": ("Balanced for virtualization (recommended)", "Optimized for a KVM host: throughput "
                     "with lower dirty-page writeback and no transparent huge page defragmentation stalls."),
    "throughput-performance": ("High performance", "Maximum CPU frequency, no deep power saving."),
    "balanced": ("Balanced", "Compromise between performance and power use."),
    "powersave": ("Low power", "Lowest power use; reduces performance."),
}


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".lsxve-tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    if proc.which("/usr/sbin/restorecon"):
        proc.run(["/usr/sbin/restorecon", str(path)], check=False)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- remote syslog
def get_syslog() -> dict:
    return {"targets": _load(SYSLOG_JSON, []), "available": proc.which("/usr/sbin/rsyslogd")}


def set_syslog(targets: List[dict]) -> dict:
    """Forward all log messages to remote syslog servers (ESXi Syslog.global.logHost)."""
    clean = []
    for i, t in enumerate(v.list_of(targets, "targets", lambda x, f: x, 4)):
        if not isinstance(t, dict):
            raise v.ValidationError(f"targets[{i}]", "must be an object")
        clean.append({"host": v.host_or_ip(t.get("host"), f"targets[{i}].host"),
                      "port": v.integer(t.get("port", 514), f"targets[{i}].port", 1, 65535),
                      "protocol": v.choice(t.get("protocol", "udp"), f"targets[{i}].protocol", ("udp", "tcp"))})
    if not proc.which("/usr/sbin/rsyslogd"):
        raise v.ValidationError("targets", "rsyslog is not installed")
    lines = ["# Managed by LSX-VE (Host > Manage > System > Syslog). Do not edit."]
    for t in clean:
        lines.append(f'*.* action(type="omfwd" target="{t["host"]}" port="{t["port"]}" protocol="{t["protocol"]}" '
                     f'queue.type="LinkedList" queue.size="10000" action.resumeRetryCount="-1")')
    if clean:
        _write(RSYSLOG_CONF, "\n".join(lines) + "\n")
    elif RSYSLOG_CONF.exists():
        RSYSLOG_CONF.unlink()
    _write(SYSLOG_JSON, json.dumps(clean), 0o640)
    proc.run([SYSTEMCTL, "enable", "rsyslog.service"], check=False)
    proc.run([SYSTEMCTL, "restart", "rsyslog.service"], timeout=60)
    return get_syslog()


# --------------------------------------------------------------------------- SNMP
def get_snmp() -> dict:
    cfg = _load(SNMP_JSON, {"enabled": False, "community": "", "sources": [], "location": "", "contact": ""})
    active = proc.run([SYSTEMCTL, "is-active", "snmpd.service"], check=False).stdout.strip() == "active"
    return {**{k: cfg.get(k) for k in ("enabled", "sources", "location", "contact")},
            "community_set": bool(cfg.get("community")), "running": active,
            "available": proc.which("/usr/sbin/snmpd")}


def set_snmp(enabled: bool, community: Optional[str], sources: List[str], location: str, contact: str) -> dict:
    """SNMP v2c read-only agent (ESXi "esxcli system snmp"): community restricted to source networks."""
    enabled = v.boolean(enabled, "enabled")
    old = _load(SNMP_JSON, {})
    if community:
        community = v.string(community, "community", 6, 32, RE_COMMUNITY)
    else:
        community = old.get("community", "")
    if enabled and not community:
        raise v.ValidationError("community", "a community string is required")
    srcs = v.list_of(sources or [], "sources", lambda x, f: v.ipv4_network(x, f) if x != "default" else x, 16)
    if enabled and not srcs:
        raise v.ValidationError("sources", "list the networks allowed to query the agent (for example 10.0.0.0/8)")
    location = v.string(location or "", "location", 0, 128, RE_TEXT)
    contact = v.string(contact or "", "contact", 0, 128, RE_TEXT)
    if enabled and not proc.which("/usr/sbin/snmpd"):
        raise v.ValidationError("enabled", "net-snmp is not installed")
    if enabled:
        orig = SNMPD_CONF.with_suffix(".conf.lsxve-orig")
        if SNMPD_CONF.exists() and not orig.exists():
            os.replace(SNMPD_CONF, orig)
        lines = ["# Managed by LSX-VE (Host > Manage > System > SNMP). Do not edit.", "agentAddress udp:161",
                 f'sysLocation "{location}"' if location else "", f'sysContact "{contact}"' if contact else "",
                 f"sysName {hardware.hostname()}", "dontLogTCPWrappersConnects yes"]
        lines += [f"rocommunity {community} {s}" for s in srcs]
        _write(SNMPD_CONF, "\n".join(l for l in lines if l) + "\n", 0o600)
        proc.run([SYSTEMCTL, "enable", "snmpd.service"], check=False)
        proc.run([SYSTEMCTL, "restart", "snmpd.service"], timeout=60)
        for args in (["--add-service=snmp"], ["--permanent", "--add-service=snmp"]):
            proc.run([FWCMD, *args], check=False)
    else:
        proc.run([SYSTEMCTL, "disable", "--now", "snmpd.service"], check=False)
        for args in (["--remove-service=snmp"], ["--permanent", "--remove-service=snmp"]):
            proc.run([FWCMD, *args], check=False)
    _write(SNMP_JSON, json.dumps({"enabled": enabled, "community": community, "sources": srcs,
                                  "location": location, "contact": contact}), 0o600)
    return get_snmp()


# --------------------------------------------------------------------------- power policy
def get_power_policy() -> dict:
    active = ""
    if proc.which(TUNED_ADM):
        out = proc.run([TUNED_ADM, "active"], check=False, timeout=30).stdout
        m = re.search(r"Current active profile:\s*(\S+)", out)
        active = m.group(1) if m else ""
    return {"active": active, "available": proc.which(TUNED_ADM),
            "policies": [{"id": k, "label": a, "description": b} for k, (a, b) in POWER_POLICIES.items()]}


def set_power_policy(policy: str) -> dict:
    policy = v.choice(policy, "policy", POWER_POLICIES)
    if not proc.which(TUNED_ADM):
        raise v.ValidationError("policy", "tuned is not installed")
    proc.run([SYSTEMCTL, "enable", "--now", "tuned.service"], check=False, timeout=60)
    proc.run([TUNED_ADM, "profile", policy], timeout=120)
    return get_power_policy()


# --------------------------------------------------------------------------- lockdown mode
def lockdown_mode() -> str:
    return _load(config.LOCKDOWN_FILE, {}).get("mode", "disabled")


def get_lockdown() -> dict:
    return {"mode": lockdown_mode()}


def set_lockdown(mode: str) -> dict:
    """disabled | normal (SSH off and cannot be enabled) | strict (also no local console customization and no
    local login terminals - only the web console manages the host)."""
    mode = v.choice(mode, "mode", ("disabled", "normal", "strict"))
    _write(config.LOCKDOWN_FILE, json.dumps({"mode": mode, "changed": int(time.time())}), 0o644)
    if mode != "disabled":
        proc.run([SYSTEMCTL, "disable", "--now", "sshd.service"], check=False, timeout=60)
    if mode == "strict":
        _write(LOGIND_DROPIN, "# Managed by LSX-VE strict lockdown mode\n[Login]\nNAutoVTs=0\nReserveVT=0\n")
        for n in range(2, 7):
            proc.run([SYSTEMCTL, "stop", f"getty@tty{n}.service"], check=False)
        proc.run([SYSTEMCTL, "restart", "systemd-logind.service"], check=False, timeout=60)
    elif LOGIND_DROPIN.exists():
        LOGIND_DROPIN.unlink()
        proc.run([SYSTEMCTL, "restart", "systemd-logind.service"], check=False, timeout=60)
    LOG.warning("lockdown mode set to %s", mode)
    return get_lockdown()


# --------------------------------------------------------------------------- support information
def cert_fingerprint() -> Optional[str]:
    try:
        pem = (config.TLS_DIR / "lsxve.crt").read_text()
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError):
        return None
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def support_info() -> dict:
    def dmi(k):
        try:
            return Path(f"/sys/class/dmi/id/{k}").read_text().strip()
        except OSError:
            return ""
    return {"product": "LSX-VE", "version": __version__, "hostname": hardware.hostname(),
            "manufacturer": dmi("sys_vendor"), "model": dmi("product_name"), "serial": dmi("product_serial"),
            "uuid": dmi("product_uuid"), "bios": f"{dmi('bios_vendor')} {dmi('bios_version')}".strip(),
            "cert_sha256": cert_fingerprint()}


# --------------------------------------------------------------------------- support bundle
BUNDLE_CMDS = [
    ("journal-lsxve.txt", ["/usr/bin/journalctl", "--no-pager", "-b", "-o", "short-iso", "-u", "lsxve-*"]),
    ("journal-libvirt.txt", ["/usr/bin/journalctl", "--no-pager", "-b", "-o", "short-iso", "-u", "virt*"]),
    ("journal-kernel.txt", ["/usr/bin/journalctl", "--no-pager", "-b", "-k", "-o", "short-iso"]),
    ("journal-previous-boot-errors.txt", ["/usr/bin/journalctl", "--no-pager", "-b", "-1", "-p", "err", "-o", "short-iso"]),
    ("journal-boots.txt", ["/usr/bin/journalctl", "--no-pager", "--list-boots"]),
    ("systemctl-failed.txt", ["/usr/bin/systemctl", "--failed", "--no-pager", "--plain"]),
    ("systemctl-units.txt", ["/usr/bin/systemctl", "list-units", "--no-pager", "--plain", "--all"]),
    ("ip-addr.json", ["/usr/sbin/ip", "-j", "-d", "addr"]),
    ("ip-route.txt", ["/usr/sbin/ip", "route", "show", "table", "all"]),
    ("nmcli-device.txt", ["/usr/bin/nmcli", "device", "show"]),
    ("nmcli-connection.txt", ["/usr/bin/nmcli", "-f", "NAME,UUID,TYPE,DEVICE,AUTOCONNECT,ACTIVE", "connection", "show"]),
    ("firewall.txt", ["/usr/bin/firewall-cmd", "--list-all"]),
    ("lsblk.json", ["/usr/bin/lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINT"]),
    ("findmnt.txt", ["/usr/bin/findmnt", "-l"]),
    ("lvm-lvs.txt", ["/usr/sbin/lvs", "-a", "-o", "+devices"]),
    ("lvm-vgs.txt", ["/usr/sbin/vgs"]),
    ("dmidecode-system.txt", ["/usr/sbin/dmidecode", "-t", "system", "-t", "bios", "-t", "processor", "-t", "memory"]),
    ("rpm-qa.txt", ["/usr/bin/rpm", "-qa", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n"]),
    ("sestatus.txt", ["/usr/sbin/sestatus", "-v"]),
    ("virt-host-validate.txt", ["/usr/bin/virt-host-validate", "qemu"]),
    ("tuned.txt", [TUNED_ADM, "active"]),
    ("lxc-ls.txt", ["/usr/bin/lxc-ls", "-f"]),
]
BUNDLE_FILES = ["/etc/lsxve-release", "/etc/os-release", "/proc/cmdline", "/proc/cpuinfo", "/proc/meminfo",
                "/proc/mounts", "/etc/lxc/lxc.conf", "/var/lib/lsxve/management-bridge.json",
                "/etc/sysconfig/libvirt-guests", "/etc/chrony.conf", "/etc/resolv.conf", "/etc/hosts"]
SECRET_KEY_RE = re.compile(r"(password|secret|token|community|key)", re.I)


def _redacted_json(path: Path) -> Optional[bytes]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    def red(x):
        if isinstance(x, dict):
            return {k: ("***" if SECRET_KEY_RE.search(k) else red(val)) for k, val in x.items()}
        if isinstance(x, list):
            return [red(i) for i in x]
        return x
    return json.dumps(red(data), indent=2).encode()


def _domain_xmls() -> List[tuple]:
    try:
        import libvirt  # type: ignore
        c = libvirt.open("qemu:///system")
    except Exception:  # noqa: BLE001
        return []
    out = []
    try:
        for d in c.listAllDomains(0):
            out.append((f"vms/{d.name()}.xml", d.XMLDesc(0)))  # without VIR_DOMAIN_XML_SECURE: no VNC passwords
        for n in c.listAllNetworks(0):
            out.append((f"networks/{n.name()}.xml", n.XMLDesc(0)))
        for p in c.listAllStoragePools(0):
            out.append((f"datastores/{p.name()}.xml", p.XMLDesc(0)))
    finally:
        c.close()
    return out


def support_bundle() -> dict:
    """Create a .tar.gz of logs and configuration for troubleshooting (ESXi vm-support). Secrets are left out:
    no shadow file, no TLS key, no NetworkManager secrets, secret-looking config keys are masked."""
    d = config.SUPPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = f"lsxve-support-{hardware.hostname()}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    path = d / name
    top = name[:-7]
    with tarfile.open(path, "w:gz") as tf:
        def add(rel: str, data: bytes):
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
            tf.addfile(ti, io.BytesIO(data))
        add("README.txt", (f"LSX-VE {__version__} support bundle\nCreated {time.ctime()}\n\n"
                           + json.dumps(support_info(), indent=2) + "\n").encode())
        for rel, argv in BUNDLE_CMDS:
            if not proc.which(argv[0]):
                continue
            try:
                cp = proc.run(argv, check=False, timeout=120)
                add(f"commands/{rel}", ((cp.stdout or "") + (("\n--- stderr ---\n" + cp.stderr) if cp.stderr else ""))
                    .encode()[:64 * 1024 * 1024])
            except Exception as e:  # noqa: BLE001
                add(f"commands/{rel}", f"failed: {e}\n".encode())
        for f in BUNDLE_FILES:
            try:
                add("files" + f, Path(f).read_bytes()[:16 * 1024 * 1024])
            except OSError:
                pass
        for f in (config.CONFIG_FILE, SNMP_JSON, SYSLOG_JSON, config.LOCKDOWN_FILE, config.AUTOSTART_FILE,
                  config.MAINTENANCE_FILE):
            data = _redacted_json(Path(f))
            if data is not None:
                add("files" + str(f), data)
        for rel, xml in _domain_xmls():
            add(rel, xml.encode())
    os.chmod(path, 0o640)
    try:
        import grp
        import pwd
        os.chown(path, pwd.getpwnam("lsxve").pw_uid, grp.getgrnam("lsxve").gr_gid)
    except (KeyError, OSError):
        pass
    old = sorted(d.glob("lsxve-support-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[3:]:
        p.unlink()
    LOG.info("support bundle %s (%d KiB)", path, path.stat().st_size // 1024)
    return {"name": name, "size": path.stat().st_size}
