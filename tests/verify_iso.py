"""Static verification of a built LSX-VE ISO (no virtualization required).

    python tests/verify_iso.py dist/LSX-VE-1.0.0-x86_64.iso

Checks: volume label, El Torito BIOS + UEFI entries, isohybrid MBR + GPT,
boot menus (isolinux, EFI grub.cfg on the ISO and inside efiboot.img) point
at the right label and kickstart, kickstart structure, .treeinfo variant,
LSXVE repository integrity (every RPM present with the sha256 from the
metadata), every %packages entry resolvable from Minimal + LSXVE, no
Cockpit/Webmin anywhere, payload files present, product.img branding.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import fatimg  # noqa: E402
import pycdlib  # noqa: E402
import repotool  # noqa: E402

FAIL = []


def check(ok: bool, what: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + what)
    if not ok:
        FAIL.append(what)


def main(path: str) -> int:
    iso = pycdlib.PyCdlib()
    iso.open(path)

    def read(p: str) -> bytes:
        b = io.BytesIO()
        iso.get_file_from_iso_fp(b, rr_path=p)
        return b.getvalue()

    def exists(p: str) -> bool:
        try:
            iso.get_record(rr_path=p)
            return True
        except pycdlib.pycdlibexception.PyCdlibException:
            return False

    label = iso.pvd.volume_identifier.decode().strip()
    check(label.startswith("LSX-VE-"), f"volume label {label}")
    check(iso.has_rock_ridge() is not None, "Rock Ridge present")

    bc = iso.eltorito_boot_catalog
    check(bc is not None, "El Torito boot catalog")
    check(bc.initial_entry.sector_count == 4 and bc.initial_entry.boot_indicator == 0x88, "BIOS boot entry (isolinux)")
    efi = [e for s in bc.sections if s.platform_id == 0xEF for e in s.section_entries]
    check(len(efi) == 1, "UEFI boot entry (platform 0xEF)")
    check(iso.isohybrid_mbr is not None, "isohybrid MBR")
    with open(path, "rb") as fh:
        head = fh.read(1024)
    parts = [head[446 + i * 16:462 + i * 16] for i in range(4)]
    check(any(p[4] == 0xEF for p in parts), "MBR EFI system partition entry (USB UEFI boot)")
    check(head[512:520] == b"EFI PART", "GPT header present")
    check(head[510:512] == b"\x55\xaa", "MBR boot signature")
    iso_end = iso.pvd.space_size * iso.logical_block_size
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        total = fh.tell()
        fh.seek(-512, 2)
        backup_hdr = fh.read(512)
    check(backup_hdr[:8] == b"EFI PART", "backup GPT header at end of image")
    check(total - iso_end >= 33 * 512, f"backup GPT lies in padding after the ISO data ({total - iso_end} bytes)")

    isocfg = read("/isolinux/isolinux.cfg").decode()
    check(f"LABEL={label}" in isocfg and "inst.ks=hd:LABEL=" in isocfg, "isolinux.cfg uses new label + kickstart")
    default = isocfg.split("menu default", 1)[1].split("\nlabel ", 1)[0]
    check(f":/ks.cfg quiet" in default and "inst.text" not in default, "isolinux default entry = graphical installer")
    check(":/ks-text.cfg inst.text" in isocfg, "isolinux offers text-mode installer")
    check("Rocky" not in isocfg, "isolinux menu rebranded")
    gcfg = read("/EFI/BOOT/grub.cfg").decode()
    check(f"-l '{label}'" in gcfg and "inst.ks=" in gcfg, "EFI grub.cfg (ISO) uses new label + kickstart")
    first = gcfg.split("menuentry ", 2)[1]
    check(":/ks.cfg quiet" in first and "inst.text" not in first and ":/ks-text.cfg inst.text" in gcfg,
          "EFI grub default entry = graphical installer, text mode offered")
    fat = fatimg.FatImage(read("/images/efiboot.img"))
    check(fat.read("/EFI/BOOT/grub.cfg").decode() == gcfg, "efiboot.img grub.cfg identical to ISO grub.cfg")
    check(fat.read("/EFI/BOOT/BOOTX64.EFI")[:2] == b"MZ", "efiboot.img shim present (Secure Boot chain unchanged)")
    for f in ("/isolinux/vmlinuz", "/isolinux/initrd.img", "/images/pxeboot/vmlinuz", "/images/pxeboot/initrd.img",
              "/images/install.img", "/isolinux/splash.png", "/isolinux/ldlinux.c32", "/isolinux/vesamenu.c32"):
        check(exists(f), f"file {f}")

    def balanced(k):
        return k.count("%end") == k.count("\n%packages") + k.count("\n%pre") + k.count("\n%post")
    gks = read("/ks.cfg").decode()
    check(balanced(gks), "GUI kickstart sections balanced")
    body = "\n".join(l for l in gks.splitlines() if not l.startswith("#"))
    check("\ngraphical\n" in body and "inst.text" not in body, "GUI kickstart uses the graphical installer")
    static = body.split("\n%pre ")[0]
    check(not re.search(r"^(clearpart|autopart|part|zerombr|ignoredisk|rootpw)\b", static, re.M)
          and "%include /tmp/lsxve-ks.cfg" in gks,
          "GUI kickstart: disk chosen and confirmed in %pre, root password in the GUI")
    gpre = gks.split("\n%pre ", 1)[1].split("\n%end", 1)[0].split("\n", 1)[1]
    check('MODE = "gui"' in gpre and "Type the device name" in gpre, "GUI %pre runs the disk step (typed confirmation)")
    check("-cockpit*" in gks and "lsxve/python" in gks and "firstboot-admin.json" in gks,
          "GUI kickstart: excludes cockpit, copies payload, imports web admin")
    ks = read("/ks-text.cfg").decode()
    check(balanced(ks), "text kickstart sections balanced")
    check("%include /tmp/lsxve-ks.cfg" in ks, "text kickstart includes installer-generated settings")
    check("-cockpit*" in ks, "text kickstart excludes cockpit")
    check("clearpart" not in ks.split("\n%pre ")[0], "no unconditional disk wipe in static kickstart")
    pre = ks.split("\n%pre ", 1)[1].split("\n%end", 1)[0].split("\n", 1)[1]
    try:
        compile(pre, "lsxve-setup", "exec")
        check(True, "%pre installer script compiles")
    except SyntaxError as e:
        check(False, f"%pre installer script compiles ({e})")
    check("Type the device name" in pre and 'MODE = "text"' in pre, "installer requires typed disk confirmation")
    check("/lsxve/iso --fstype={FSTYPE}" in pre and 'FSTYPE = "ext4"' in pre and "sfdisk" in pre and "BOOT_GIB, SWAP_GIB, ISO_GIB, ROOT_MIN_GIB = 1, 1, 32, 16" in pre,
          "fixed partition layout: boot 1G, swap 1G, isos 32G, root rest")
    check(".lsxve-datastore" in ks and ".lsxve-datastore" in gks, "datastore1 directory created on root")
    pk = ks.split("\n%packages", 1)[1].split("\n%end", 1)[0].splitlines()[1:]
    gpk = gks.split("\n%packages", 1)[1].split("\n%end", 1)[0].splitlines()[1:]
    check(gpk == pk, "GUI and text kickstarts install the same packages")
    want = [l.strip() for l in pk if l.strip() and not l.startswith(("-", "@", "#"))]

    ti = read("/.treeinfo").decode()
    check("[variant-LSXVE]" in ti and "repository = Hypervisor" in ti and "Minimal,LSXVE" in ti, ".treeinfo LSXVE variant")

    def repo(base):
        md = ET.fromstring(read(f"{base}/repodata/repomd.xml"))
        href = [d.find("r:location", repotool.NS).get("href") for d in md.findall("r:data", repotool.NS)
                if d.get("type") == "primary"][0]
        raw = read(f"{base}/{href}")
        ck = [d.find("r:checksum", repotool.NS).text for d in md.findall("r:data", repotool.NS)
              if d.get("type") == "primary"][0]
        check(hashlib.sha256(raw).hexdigest() == ck, f"{base} primary checksum matches repomd")
        return repotool.parse_primary(repotool.decompress(href, raw), "iso" if base == "/Minimal" else "x")

    minimal, extra = repo("/Minimal"), repo("/Hypervisor")
    bad = []
    for p in extra:
        data = read(f"/Hypervisor/{p.href}")
        if hashlib.sha256(data).hexdigest() != p.checksum or len(data) != p.size:
            bad.append(p.filename())
    check(not bad, f"LSXVE: {len(extra)} RPMs present with matching sha256" + (f" (bad: {bad[:5]})" if bad else ""))
    names = {p.name for p in minimal} | {p.name for p in extra}
    missing = [n for n in want if n not in names]
    check(not missing, f"all {len(want)} %packages entries available" + (f" (missing {missing})" if missing else ""))
    check(not any(re.match(r"cockpit|webmin", p.name) for p in extra), "no cockpit/webmin packages in LSXVE")
    names_x = {p.name for p in extra}
    check({"lxc", "lxc-libs", "lxc-templates", "lxcfs"} <= names_x, "LXC packages in the Hypervisor repository")
    check({"epel-release", "htop", "mc", "nload"} <= names_x and {"nano", "net-tools"} <= names,
          "admin tools available (epel-release, htop, mc, nano, net-tools, nload)")
    check(not any(p.name.startswith("vim") for p in extra) and "\n-vim*" in ks and "\n-vim*" in gks,
          "vim excluded from the installation")
    # dependency closure: every requirement of LSXVE packages satisfiable by Minimal + LSXVE
    pool = repotool.Pool(minimal, extra)
    unres = []
    for p in extra:
        for r in p.requires:
            if r.name.startswith(("rpmlib(", "(")):
                continue
            if not pool.providers(r):
                unres.append(f"{p.name}: {r}")
    check(not unres, "LSXVE requirements satisfiable" + (f" ({unres[:5]})" if unres else ""))

    for f in ("/lsxve/install-lsxve.sh", "/lsxve/python/lsxve/api/app.py", "/lsxve/python/lsxve/dcui/main.py",
              "/lsxve/python/lsxve/core/init.py", "/lsxve/web/index.html", "/lsxve/packaging/systemd/lsxve-api.service",
              "/lsxve/VERSION", "/lsxve/python/lsxve/ct/lxc.py", "/lsxve/python/lsxve/host/mgmtbridge.py", "/lsxve/python/lsxve/api/routes_ct.py",
              "/lsxve/packaging/systemd/lsxve-ct@.service", "/lsxve/packaging/systemd/lsxve-syslog-console.service",
              "/lsxve/packaging/systemd/lsxve-vm-autostart.service", "/lsxve/packaging/bin/lsxve-vm-autostart",
              "/lsxve/python/lsxve/api/alarms.py", "/lsxve/python/lsxve/api/routes_admin.py",
              "/lsxve/python/lsxve/host/vswitch.py", "/lsxve/python/lsxve/host/vmio.py", "/lsxve/python/lsxve/host/hostcfg.py",
              "/lsxve/python/lsxve/hv/ovf.py", "/lsxve/python/lsxve/hv/autostart.py"):
        check(exists(f), f"payload {f}")
    check({"rsyslog", "net-snmp"} <= names, "rsyslog and net-snmp available (remote syslog, SNMP agent)")
    gt = "/lsxve/guest-tools/lsx-ve-guest-tools.iso"
    if exists(gt):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = os.path.join(td, "gt.iso")
            with open(tmp, "wb") as fh:
                iso.get_file_from_iso_fp(fh, rr_path=gt)
            g = pycdlib.PyCdlib()
            g.open(tmp)
            label = g.pvd.volume_identifier.decode().strip()
            jfiles = {f for _d, _x, fs in g.walk(joliet_path="/") for f in fs} if g.has_joliet() else set()
            g.close()
        check(label == "LSXVE_TOOLS" and {"LSX-VE-Guest-Tools-Setup.cmd", "virtio-win-guest-tools.exe", "install.sh",
                                          "README.txt"} <= jfiles,
              "lsx-ve-guest-tools.iso: Joliet names, setup wrapper, virtio-win installer, Linux installer")
        check(exists("/lsxve/guest-tools/VERSION"), "guest tools VERSION file")
    else:
        check(False, "payload contains lsx-ve-guest-tools.iso")
    inst = read("/lsxve/install-lsxve.sh").decode()
    check("lsxve-vm-autostart.service" in inst and "guest-tools/lsx-ve-guest-tools.iso" in inst,
          "install script enables VM autostart and installs the guest tools ISO")
    check(b"\r\n" not in read("/lsxve/install-lsxve.sh"), "install script has LF line endings")
    logind = read("/lsxve/packaging/etc/lsxve-logind.conf").decode() if exists("/lsxve/packaging/etc/lsxve-logind.conf") else ""
    check("HandleLidSwitch=ignore" in logind and exists("/lsxve/packaging/etc/lsxve-sleep.conf")
          and "systemctl mask sleep.target suspend.target" in inst,
          "host never suspends (lid ignored, sleep targets masked)")
    broken = []
    for dp, _ds, fs in iso.walk(rr_path="/lsxve/python"):
        for f in fs:
            if f.endswith(".py"):
                src = read(dp.rstrip("/") + "/" + f)
                try:
                    compile(src, f, "exec")
                except (SyntaxError, ValueError) as e:
                    broken.append(f"{f}: {e}")
    check(not broken, "every payload .py file in the ISO compiles" + (f" ({broken[:3]})" if broken else ""))
    root = Path(__file__).resolve().parents[1] / "src"
    diff = []
    for dp, _ds, fs in iso.walk(rr_path="/lsxve/python/lsxve"):
        for f in fs:
            rp = dp.rstrip("/") + "/" + f
            local = root / rp[len("/lsxve/python/"):]
            if local.exists() and local.read_bytes().replace(b"\r\n", b"\n") != read(rp):
                diff.append(rp)
    check(not diff, "payload identical to src/ (byte for byte)" + (f" ({diff[:3]})" if diff else ""))
    prod = gzip.decompress(read("/images/product.img"))
    check(prod.startswith(b"070701") and b"Product=LSX-VE" in prod, "product.img (cpio newc) brands installer")
    check(b"etc/anaconda/conf.d/90-lsxve.conf\0" in prod and b"StorageSpoke" in prod
          and b"hidden_spokes" in prod, "product.img: Anaconda drop-in (storage spoke hidden, hidden spokes)")
    check(all(n in prod for n in (b"pixmaps/lsxve.css\0", b"pixmaps/sidebar-logo.png\0", b"pixmaps/sidebar-bg.png\0",
                                   b"pixmaps/topbar-bg.png\0")), "product.img: graphical installer branding")
    iso.close()
    print(f"\n{len(FAIL)} failure(s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/LSX-VE-1.0.0-x86_64.iso"))
