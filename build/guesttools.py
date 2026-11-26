"""Build lsx-ve-guest-tools.iso: the ISO that "Install LSX-VE Guest Tools" mounts into a VM.

Contents (the drivers themselves are the unmodified, Microsoft-signed Red Hat VirtIO drivers; only the
wrapper is LSX-VE branded):
  * everything from the upstream virtio-win ISO (VirtIO storage / network / balloon / serial / GPU / input
    drivers for Windows, virtio-win-guest-tools.exe installer with the QEMU guest agent)
  * LSX-VE-Guest-Tools-Setup.cmd - elevated, unattended install for Windows (like VMware Tools setup.exe)
  * linux/install.sh + linux/el9/qemu-guest-agent-*.rpm - guest agent for Linux guests
  * README.txt

Called from build.py; the result is cached in build/cache/guest-tools and lands in the payload as
lsxve/guest-tools/lsx-ve-guest-tools.iso.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

import pycdlib

import repotool

LABEL = "LSXVE_TOOLS"
DEFAULT_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"

SETUP_CMD = r"""@echo off
rem LSX-VE Guest Tools for Windows
rem Installs the VirtIO drivers and the QEMU guest agent (clean shutdown, IP / host name reporting,
rem quiesced snapshots) from the files on this CD. Run as Administrator; asks for elevation if needed.
setlocal
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set "SRC=%~dp0"
echo.
echo  LSX-VE Guest Tools
echo  ==================
echo  Installing VirtIO drivers and the QEMU guest agent from %SRC%
echo.
if not exist "%SRC%virtio-win-guest-tools.exe" (
    echo virtio-win-guest-tools.exe not found on this media.
    pause
    exit /b 1
)
"%SRC%virtio-win-guest-tools.exe" /passive /norestart
set RC=%ERRORLEVEL%
if "%RC%"=="3010" (
    echo.
    echo Installation complete. Restart Windows to finish.
) else if "%RC%"=="0" (
    echo.
    echo Installation complete.
) else (
    echo.
    echo The installer returned code %RC%.
)
pause
exit /b %RC%
"""

INSTALL_SH = """#!/bin/sh
# LSX-VE Guest Tools for Linux: installs and starts the QEMU guest agent.
# The VirtIO drivers are part of every current Linux kernel.
#   sudo sh /run/media/$USER/LSXVE_TOOLS/linux/install.sh      (or wherever the CD is mounted)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" != 0 ]; then echo "run as root (sudo sh $0)" >&2; exit 1; fi
. /etc/os-release 2>/dev/null || true
case " ${ID:-} ${ID_LIKE:-} " in
  *" rhel "*|*" centos "*|*" fedora "*|*" rocky "*|*" almalinux "*)
    if command -v dnf >/dev/null 2>&1 && dnf -q -y install qemu-guest-agent 2>/dev/null; then :;
    elif [ "${VERSION_ID%%.*}" = 9 ] && ls "$HERE"/el9/qemu-guest-agent-*.rpm >/dev/null 2>&1; then
      rpm -Uvh --replacepkgs "$HERE"/el9/qemu-guest-agent-*.rpm
    else
      yum -y install qemu-guest-agent
    fi ;;
  *" debian "*|*" ubuntu "*)
    apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-guest-agent ;;
  *" suse "*|*" opensuse "*|*" sles "*)
    zypper -n install qemu-guest-agent ;;
  *" arch "*)
    pacman -S --noconfirm qemu-guest-agent ;;
  *" alpine "*)
    apk add qemu-guest-agent && rc-update add qemu-guest-agent default ;;
  *)
    echo "unknown distribution: install the qemu-guest-agent package with your package manager" >&2; exit 1 ;;
esac
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now qemu-guest-agent 2>/dev/null || systemctl start qemu-guest-agent
elif command -v rc-service >/dev/null 2>&1; then
  rc-service qemu-guest-agent start
fi
echo "LSX-VE Guest Tools: QEMU guest agent installed and running."
"""

README = """LSX-VE Guest Tools {version}
==============================

This disc is mounted into a virtual machine by "Install LSX-VE Guest Tools" in the LSX-VE web console.
Unmount it from the same menu when you are done.

Windows
-------
Run LSX-VE-Guest-Tools-Setup.cmd (it asks for administrator rights). It installs:
  * VirtIO drivers: storage (viostor, vioscsi), network (NetKVM), memory balloon, serial, input, GPU,
    file system (viofs), RNG
  * the QEMU guest agent: clean shutdown and restart from the console, guest host name and IP addresses
    on the VM summary page, file system freeze for consistent snapshots

Installing Windows on a VirtIO disk: in Windows Setup choose "Load driver" and browse this disc to
viostor\\<version>\\amd64 (VirtIO block) or vioscsi\\<version>\\amd64 (VirtIO SCSI); NetKVM\\<version>\\amd64
for the network adapter.

Linux
-----
  sudo sh <mount point>/linux/install.sh
installs and starts qemu-guest-agent (VirtIO drivers are already in the Linux kernel). A Rocky / RHEL /
Alma 9 package is included for hosts without internet access.

Credits
-------
The Windows drivers and installers are the virtio-win project's binaries ({virtio}), distributed
unmodified under their licenses (see the license files on this disc). LSX-VE adds only this README and
the setup wrappers.
"""


def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 10 * 1048576:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    print(f"[build] downloading {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": repotool.UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if time.time() - last > 10:
                last = time.time()
                print(f"  {done >> 20} / {total >> 20} MiB", flush=True)
    os.replace(tmp, dest)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _guest_agent_rpm(appstream_url: str, cache: Path) -> Path:
    pkgs = [p for p in repotool.load_remote_repo(appstream_url, "appstream", cache)
            if p.name == "qemu-guest-agent" and p.arch == "x86_64"]
    if not pkgs:
        raise IOError("qemu-guest-agent not found in AppStream")
    import functools
    best = max(pkgs, key=functools.cmp_to_key(lambda a, b: repotool.evr_cmp(a.evr, b.evr)))
    dest = cache / "rpms" / best.filename()
    repotool.fetch(best.base_url + best.href, dest, best.checksum)
    return dest


def _extract(src: Path, tree: Path) -> str:
    iso = pycdlib.PyCdlib()
    iso.open(str(src))
    label = iso.pvd.volume_identifier.decode(errors="replace").strip()
    if iso.has_joliet():
        kw = "joliet_path"
    elif iso.has_rock_ridge():
        kw = "rr_path"
    else:
        kw = "iso_path"
    for dirpath, dirs, files in iso.walk(**{kw: "/"}):
        local = tree / dirpath.lstrip("/")
        local.mkdir(parents=True, exist_ok=True)
        for f in files:
            name = f[:-2] if kw == "iso_path" and f.endswith(";1") else f
            iso.get_file_from_iso(str(local / name), **{kw: dirpath.rstrip("/") + "/" + f})
    iso.close()
    return label


class _Namer:
    def __init__(self):
        self.used = {}

    def name(self, parent: str, real: str, is_dir: bool) -> str:
        import re
        used = self.used.setdefault(parent, set())
        up = real.upper()
        if is_dir:
            base, ext = re.sub(r"[^A-Z0-9_]", "_", up)[:8], ""
        else:
            b, _, e = up.rpartition(".") if "." in up.lstrip(".") else (up, "", "")
            base = re.sub(r"[^A-Z0-9_]", "_", b)[:8] or "F"
            ext = re.sub(r"[^A-Z0-9_]", "_", e)[:3]
        cand = base + (f".{ext}" if ext else "")
        n = 0
        while cand in used:
            n += 1
            suf = str(n)
            cand = base[:8 - len(suf) - 1] + "_" + suf + (f".{ext}" if ext else "")
        used.add(cand)
        return cand


def _master(tree: Path, out: Path, version: str) -> None:
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, sys_ident="WIN32", vol_ident=LABEL, joliet=3, rock_ridge="1.09",
            app_ident_str=f"LSX-VE Guest Tools {version}", pub_ident_str="LSX-VE")
    namer = _Namer()
    paths = {"": ("", "")}
    seen = {}
    for dirpath, dirs, files in os.walk(tree):
        rel = os.path.relpath(dirpath, tree).replace("\\", "/")
        rel = "" if rel == "." else rel
        dirs.sort()
        ip, jp = paths[rel]
        for d in dirs:
            r = f"{rel}/{d}" if rel else d
            nip = ip + "/" + namer.name(ip, d, True)
            njp = jp + "/" + d[:64]
            paths[r] = (nip, njp)
            iso.add_directory(nip, rr_name=d, joliet_path=njp, file_mode=0o040555)
        for f in sorted(files):
            mode = 0o100555 if f.endswith((".sh", ".cmd", ".exe")) else 0o100444
            src = os.path.join(dirpath, f)
            nip, njp = ip + "/" + namer.name(ip, f, False) + ";1", jp + "/" + f[:64]
            # virtio-win repeats the same driver binaries for many Windows versions; the upstream ISO stores
            # them once, so do the same (hard links to one extent) instead of doubling the image size
            key = (os.path.getsize(src), _sha256(Path(src)))
            first = seen.get(key)
            if first:
                iso.add_hard_link(iso_old_path=first, iso_new_path=nip, rr_name=f)
                iso.add_hard_link(iso_old_path=first, joliet_new_path=njp)
                continue
            seen[key] = nip
            iso.add_file(src, nip, rr_name=f, joliet_path=njp, file_mode=mode)
    tmp = out.with_suffix(".part")
    iso.write(str(tmp))
    iso.close()
    os.replace(tmp, out)


def build(cfg: dict, cache: Path, work: Path, out_dir: Path, version: str) -> Path:
    gt = cfg.get("guest_tools", {})
    url = gt.get("virtio_win_url", DEFAULT_URL)
    src = cache / "virtio-win.iso"
    _download(url, src)
    rpm = _guest_agent_rpm(cfg["upstream"]["appstream"], cache)
    key = {"virtio": _sha256(src), "rpm": rpm.name, "version": version, "layout": 2}
    built = cache / "guest-tools" / "lsx-ve-guest-tools.iso"
    stamp = cache / "guest-tools" / "stamp.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (built.exists() and stamp.exists() and json.loads(stamp.read_text()).get("key") == key):
        tree = work / "guest-tools"
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True)
        print("[build] extracting virtio-win", flush=True)
        virtio = _extract(src, tree)
        (tree / "linux" / "el9").mkdir(parents=True, exist_ok=True)
        shutil.copy(rpm, tree / "linux" / "el9" / rpm.name)
        (tree / "linux" / "install.sh").write_text(INSTALL_SH, newline="\n")
        (tree / "LSX-VE-Guest-Tools-Setup.cmd").write_text(SETUP_CMD.replace("\n", "\r\n"), newline="")
        (tree / "README.txt").write_text(README.format(version=version, virtio=virtio).replace("\n", "\r\n"),
                                         newline="")
        print(f"[build] mastering lsx-ve-guest-tools.iso ({virtio})", flush=True)
        built.parent.mkdir(parents=True, exist_ok=True)
        _master(tree, built, version)
        stamp.write_text(json.dumps({"key": key, "virtio": virtio}))
        shutil.rmtree(tree, ignore_errors=True)
    info = json.loads(stamp.read_text())
    shutil.copy(built, out_dir / "lsx-ve-guest-tools.iso")
    (out_dir / "VERSION").write_text(f"{version} ({info['virtio']}, {rpm.name[:-4]})\n", newline="\n")
    print(f"[build] guest tools: {built.stat().st_size >> 20} MiB ({info['virtio']})", flush=True)
    return out_dir / "lsx-ve-guest-tools.iso"
