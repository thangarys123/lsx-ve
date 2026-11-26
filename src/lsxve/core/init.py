"""lsxve-init: LSX-VE Core host initialisation (runs as root once per boot).

* detects hardware and virtualization capabilities, loads KVM modules
* prepares directories, ownership and SELinux labels
* generates the self-signed management certificate on first boot
* makes sure the hypervisor services are running
* writes the console banner (/etc/issue.d) with the management URL
* records a hardware/health snapshot in /run/lsxve/host.json
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import sys
import time
from pathlib import Path

from .. import PRODUCT, __version__
from ..common import config, log, proc
from ..host import hardware

LOG = log.setup("lsxve.init")

LIBVIRT_UNITS = ["virtqemud.socket", "virtqemud-ro.socket", "virtqemud-admin.socket", "virtqemud.service",
                 "virtnetworkd.socket", "virtstoraged.socket", "virtnodedevd.socket", "virtsecretd.socket",
                 "virtinterfaced.socket", "virtlogd.socket", "virtlockd.socket"]


def _chown(path: Path, user: str, group: str, mode: int) -> None:
    try:
        os.chown(path, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
        os.chmod(path, mode)
    except (KeyError, OSError) as e:
        LOG.warning("cannot set ownership of %s: %s", path, e)


def load_kvm() -> dict:
    cpu = hardware.cpu_info()
    module = {"Intel VT-x": "kvm_intel", "AMD-V": "kvm_amd"}.get(cpu["virtualization"] or "")
    if module and not os.path.exists("/dev/kvm"):
        proc.run(["/usr/sbin/modprobe", module], check=False, timeout=30)
        for _ in range(20):
            if os.path.exists("/dev/kvm"):
                break
            time.sleep(0.25)
    st = hardware.kvm_status()
    if not cpu["virtualization"]:
        LOG.warning("CPU does not advertise VT-x/AMD-V - virtual machines will use software emulation")
    elif not st["dev_kvm"]:
        LOG.warning("%s present but /dev/kvm missing: virtualization may be disabled in firmware", cpu["virtualization"])
    return {"cpu_virtualization": cpu["virtualization"], **st}


def prepare_dirs() -> None:
    for d, owner, mode in ((config.STATE_DIR, ("lsxve", "lsxve"), 0o750), (config.ETC_DIR, ("lsxve", "lsxve"), 0o750),
                           (config.TLS_DIR, ("lsxve", "lsxve"), 0o750), (config.LOG_DIR, ("lsxve", "lsxve"), 0o750),
                           (config.RUN_DIR, ("root", "lsxve"), 0o750)):
        d.mkdir(parents=True, exist_ok=True)
        _chown(d, owner[0], owner[1], mode)
    for d in (Path("/lsxve"), config.DATASTORE_ROOT, config.ISO_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)
    if proc.which("/usr/sbin/restorecon"):
        proc.run(["/usr/sbin/restorecon", "-R", "/lsxve", str(config.ETC_DIR), str(config.STATE_DIR),
                  str(config.LOG_DIR)],
                 check=False, timeout=300)


def ensure_certificate() -> None:
    from ..api import tls
    if tls.CERT.exists() and tls.KEY.exists():
        return
    mg = hardware.management_addresses()
    ips = [a.split("/")[0] for a in mg["ipv4"] + mg["ipv6"]]
    LOG.info("generating self-signed TLS certificate for %s %s", hardware.hostname(), ips)
    tls.generate_self_signed(hardware.hostname(), ips)
    for f in (tls.CERT, tls.KEY):
        _chown(f, "lsxve", "lsxve", 0o600 if f == tls.KEY else 0o644)


def ensure_hypervisor() -> None:
    proc.run(["/usr/bin/systemctl", "start", *LIBVIRT_UNITS], check=False, timeout=120)


def ensure_lxc() -> None:
    """Point LXC at datastore1 (or /var/lib/lxc) once; an administrator's own lxc.lxcpath is kept."""
    from ..ct import lxc
    if not lxc.available():
        return
    conf = lxc.LXC_CONF
    text = conf.read_text() if conf.exists() else ""
    if any(l.split("=")[0].strip() == "lxc.lxcpath" for l in text.splitlines() if "=" in l):
        path = Path(lxc.lxcpath())
    else:
        ds1 = config.DATASTORE_ROOT / "datastore1"
        local = os.path.ismount(ds1) or (ds1 / ".lsxve-datastore").is_file()
        path = ds1 / "containers" if local else Path(lxc.DEFAULT_PATH)
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(text + ("" if not text or text.endswith("\n") else "\n") +
                        f"# set by lsxve-init: where LSX-VE keeps LXC containers\nlxc.lxcpath = {path}\n")
        LOG.info("LXC containers will be stored in %s", path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o755)


def write_issue(kvm: dict) -> None:
    mg = hardware.management_addresses()
    url = f"https://{mg['ipv4'][0].split('/')[0]}/" if mg["ipv4"] else "(no IPv4 address - configure networking)"
    lines = [
        "",
        f"  {PRODUCT} {__version__}  -  Linux Server eXtended Virtualization Environment",
        f"  Host: \\n    Kernel: \\r",
        f"  Manage this host from a web browser: {url}",
        "  Local console: press Alt+F1. Shell login: this terminal.",
        "",
    ]
    if not kvm.get("dev_kvm"):
        lines.insert(-1, "  WARNING: hardware virtualization unavailable (enable VT-x/AMD-V in firmware).")
    issue_d = Path("/etc/issue.d")
    issue_d.mkdir(exist_ok=True)
    (issue_d / "50-lsxve.issue").write_text("\n".join(lines) + "\n")


def main() -> None:
    if os.geteuid() != 0:
        print("lsxve-init must run as root", file=sys.stderr)
        sys.exit(1)
    started = time.time()
    prepare_dirs()
    kvm = load_kvm()
    try:
        ensure_certificate()
    except Exception as e:  # never block boot
        LOG.error("certificate generation failed: %s", e)
    ensure_hypervisor()
    try:
        ensure_lxc()
    except OSError as e:
        LOG.error("LXC setup failed: %s", e)
    write_issue(kvm)
    snap = {"version": __version__, "booted": hardware.boot_time(), "initialised": time.time(),
            "kvm": kvm, "cpu": hardware.cpu_info(), "memory": hardware.memory_info(),
            "firmware": hardware.firmware_mode(), "iommu": hardware.iommu_enabled(),
            "management": hardware.management_addresses()}
    (config.RUN_DIR / "host.json").write_text(json.dumps(snap, indent=2))
    LOG.info("host initialised in %.1fs (kvm=%s)", time.time() - started, kvm.get("dev_kvm"))


if __name__ == "__main__":
    main()
