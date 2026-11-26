"""Build the LSX-VE installation ISO from the Rocky Linux 9 Minimal ISO.

    python build/build.py [--source Rocky-9.9-x86_64-minimal.iso] [--output dist/]
                          [--skip-web] [--offline]

Steps
  1. extract the source ISO (Rock Ridge names) into build/work/tree
  2. resolve + download the hypervisor RPMs and write the LSXVE repository
  3. add the LSX-VE payload (backend, web console, units, installer script)
  4. generate the kickstart, boot menus (BIOS isolinux + UEFI grub, also
     inside images/efiboot.img), splash, product.img branding, .treeinfo
  5. master a new hybrid ISO (El Torito BIOS + UEFI, isohybrid MBR/GPT)

Requires Python 3.8+ and pycdlib (pip install pycdlib). Works on Linux and
Windows. Network access is needed for step 2 unless --offline is given and
the RPM cache (build/cache) is populated.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import fatimg  # noqa: E402
import guesttools  # noqa: E402
import repotool  # noqa: E402

try:
    import pycdlib
except ImportError:  # pragma: no cover
    sys.exit("pycdlib is required: python -m pip install pycdlib")


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "src/lsxve/__init__.py").read_text())
    return m.group(1)


VERSION = version()
LABEL = "LSX-VE-" + VERSION.rsplit(".", 1)[0].replace(".", "-") + "-x86_64"   # e.g. LSX-VE-1-0-x86_64
WORK = HERE / "work"
TREE = WORK / "tree"
CACHE = HERE / "cache"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# --------------------------------------------------------------------------- 1. extract
def extract(src: Path) -> str:
    stamp = WORK / "extracted.json"
    key = {"src": str(src.resolve()), "size": src.stat().st_size, "mtime": int(src.stat().st_mtime)}
    iso = pycdlib.PyCdlib()
    iso.open(str(src))
    src_label = iso.pvd.volume_identifier.decode().strip()
    if stamp.exists() and json.loads(stamp.read_text()) == key and TREE.exists():
        log("source ISO already extracted")
        iso.close()
        return src_label
    if TREE.exists():
        shutil.rmtree(TREE)
    TREE.mkdir(parents=True)
    log(f"extracting {src.name} (label {src_label})")
    for dirpath, dirs, files in iso.walk(rr_path="/"):
        local = TREE / dirpath.lstrip("/")
        local.mkdir(parents=True, exist_ok=True)
        for f in files:
            iso.get_file_from_iso(str(local / f), rr_path=(dirpath.rstrip("/") + "/" + f))
    iso.close()
    stamp.write_text(json.dumps(key))
    return src_label


# --------------------------------------------------------------------------- 2. repository
def build_repo(cfg: dict, offline: bool) -> list:
    prim = next((TREE / "Minimal/repodata").glob("*-primary.xml.gz"))
    upstream = cfg["upstream"]
    if offline:
        raise SystemExit("--offline: metadata download disabled (not implemented without a cache of metadata)")
    res, pkgs = repotool.resolve(gzip.decompress(prim.read_bytes()), upstream, cfg["packages"], cfg["exclude"], CACHE)
    bad = [l for l in res.log if l.startswith(("UNRESOLVED", "MISSING"))]
    (WORK / "resolve.log").write_text("\n".join(res.log) + "\n")
    if bad:
        for l in bad:
            log(l)
        raise SystemExit("dependency resolution failed (see build/work/resolve.log)")
    log(f"LSXVE repository: {len(pkgs)} packages, {sum(p.size for p in pkgs) / 1048576:.1f} MiB")
    # every program LSX-VE runs must end up installed (@core + packages + dependencies)
    import toolcheck
    comps = next((TREE / "Minimal/repodata").glob("*comps*.xml")).read_text(encoding="utf-8")
    core = toolcheck.core_packages(comps, toolcheck.ks_excludes((ROOT / "installer" / "ks.cfg.in").read_text(encoding="utf-8")))
    pool = repotool.Pool(repotool.parse_primary(gzip.decompress(prim.read_bytes()), "iso"), pkgs)
    installed, gone = toolcheck.closure(pool, core + cfg["packages"], cfg["exclude"])
    missing = gone + toolcheck.missing_tools(installed)
    if missing:
        for m in missing:
            log(f"MISSING TOOL {m}")
        raise SystemExit("required programs would not be installed on the host (see build/toolcheck.py)")
    log(f"tool check: {len(toolcheck.REQUIRED)} programs present in the {len(installed)} installed packages")
    out = TREE / "Hypervisor"   # not "LSXVE": would collide with lsxve/ on case-insensitive filesystems
    if out.exists():
        shutil.rmtree(out)
    repotool.write_repo(pkgs, out, CACHE / "rpms")
    return pkgs


# --------------------------------------------------------------------------- 3. payload
def build_web(skip: bool) -> Path:
    dist = ROOT / "web" / "dist"
    if not skip:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise SystemExit("npm not found; build the web console first or pass --skip-web")
        log("building web console (npm run build)")
        subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True, shell=False)
    if not (dist / "index.html").exists():
        raise SystemExit("web/dist/index.html missing")
    return dist


def payload(dist: Path) -> None:
    p = TREE / "lsxve"
    if p.exists():
        shutil.rmtree(p)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src" / "lsxve", p / "python" / "lsxve", ignore=ign)
    shutil.copytree(dist, p / "web")
    shutil.copytree(ROOT / "packaging", p / "packaging", ignore=shutil.ignore_patterns("install-lsxve.sh"))
    shutil.copy(ROOT / "packaging" / "install-lsxve.sh", p / "install-lsxve.sh")
    (p / "VERSION").write_text(VERSION + "\n")
    for f in p.rglob("*"):  # LF line endings for everything executed on the host
        if f.is_file() and f.suffix in ("", ".sh", ".py", ".service", ".conf", ".rules", ".xml", ".sysctl",
                                         ".sysusers", ".tmpfiles", ".sysconfig", ".lsxve") and f.parent.name != "web":
            b = f.read_bytes()
            if b"\r\n" in b:
                f.write_bytes(b.replace(b"\r\n", b"\n"))


# --------------------------------------------------------------------------- 4. boot config / branding
def kickstart(cfg: dict) -> None:
    setup = (ROOT / "installer" / "lsxve-setup.py").read_text().replace("@VERSION@", VERSION).replace("@LABEL@", LABEL)
    if re.search(r"^%end", setup, re.M):
        raise SystemExit("installer script must not contain a line starting with %end")
    pk = "\n".join(cfg["packages"])
    # ks.cfg: graphical Anaconda installer (default); ks-text.cfg: LSX-VE text-mode setup dialog
    for src, dst in (("ks.cfg.in", "ks.cfg"), ("ks-text.cfg.in", "ks-text.cfg")):
        ks = (ROOT / "installer" / src).read_text()
        mode = "text" if "text" in dst else "gui"
        ks = ks.replace("@SETUP@", setup.rstrip().replace("@MODE@", mode)).replace("@PACKAGES@", pk)
        ks = ks.replace("@VERSION@", VERSION)
        ks = ks.replace("@BASEVER@", cfg.get("base_version", "9"))
        (TREE / dst).write_bytes(ks.replace("\r\n", "\n").encode())


def kargs(extra: str = "", text: bool = False) -> str:
    ks = "ks-text.cfg inst.text" if text else "ks.cfg"
    return f"inst.stage2=hd:LABEL={LABEL} inst.ks=hd:LABEL={LABEL}:/{ks} quiet{(' ' + extra) if extra else ''}"


def isolinux_cfg() -> None:
    memtest = (TREE / "isolinux" / "memtest").exists()
    cfg = f"""default vesamenu.c32
timeout 100

menu clear
menu background splash.png
menu title LSX-VE {VERSION} Installer
menu vshift 10
menu rows 10
menu margin 10
menu helpmsgrow 17
menu tabmsgrow 15
menu color border * #00000000 #00000000 none
menu color sel 0 #ffffffff #ff2f6fd6 none
menu color title 0 #ffe6edf7 #00000000 none
menu color tabmsg 0 #ff7f93b3 #00000000 none
menu color unsel 0 #ffc6d3e6 #00000000 none
menu color hotsel 0 #ffffffff #ff2f6fd6 none
menu color hotkey 0 #ff6ea8ff #00000000 none
menu color help 0 #ffc6d3e6 #00000000 none
menu color timeout 0 #ffc6d3e6 #00000000 none
menu color timeout_msg 0 #ff7f93b3 #00000000 none
menu color cmdmark 0 #ff6ea8ff #00000000 none
menu color cmdline 0 #ffffffff #00000000 none
menu tabmsg Press Tab to edit boot options.

label install
  menu label ^Install LSX-VE {VERSION}
  menu default
  kernel vmlinuz
  append initrd=initrd.img {kargs()}
  text help
	Graphical installer. You choose the target disk (nothing is erased
	before you confirm), network, time zone and the root password.
  endtext

label text
  menu label Install LSX-VE {VERSION} (^text mode)
  kernel vmlinuz
  append initrd=initrd.img {kargs(text=True)}
  text help
	Text-mode LSX-VE setup dialog: hardware check, disk selection with
	typed confirmation, network, time and administrator account.
  endtext

label basicvideo
  menu label Install LSX-VE {VERSION} (^basic graphics mode)
  kernel vmlinuz
  append initrd=initrd.img {kargs('nomodeset')}

menu separator

label rescue
  menu label ^Rescue an installed LSX-VE system
  kernel vmlinuz
  append initrd=initrd.img inst.stage2=hd:LABEL={LABEL} inst.rescue quiet
{('''
label memtest
  menu label Run a ^memory test
  kernel memtest
''') if memtest else ''}
label local
  menu label Boot from ^local drive
  localboot 0xffff
"""
    (TREE / "isolinux" / "isolinux.cfg").write_text(cfg, newline="\n")
    (TREE / "isolinux" / "boot.msg").write_text(f"\nLSX-VE {VERSION} - press Enter to boot the installer.\n",
                                               newline="\n")


def grub_cfg() -> str:
    return f"""set default="0"

function load_video {{
  insmod efi_gop
  insmod efi_uga
  insmod video_bochs
  insmod video_cirrus
  insmod all_video
}}

load_video
set gfxpayload=keep
insmod gzio
insmod part_gpt
insmod ext2

set timeout=10
set menu_color_normal=light-gray/black
set menu_color_highlight=white/blue

search --no-floppy --set=root -l '{LABEL}'

menuentry 'Install LSX-VE {VERSION}' --class fedora --class gnu-linux --class gnu --class os {{
	linuxefi /images/pxeboot/vmlinuz {kargs()}
	initrdefi /images/pxeboot/initrd.img
}}
menuentry 'Install LSX-VE {VERSION} (text mode)' --class fedora --class gnu-linux --class gnu --class os {{
	linuxefi /images/pxeboot/vmlinuz {kargs(text=True)}
	initrdefi /images/pxeboot/initrd.img
}}
menuentry 'Install LSX-VE {VERSION} (basic graphics mode)' --class fedora --class gnu-linux --class gnu --class os {{
	linuxefi /images/pxeboot/vmlinuz {kargs('nomodeset')}
	initrdefi /images/pxeboot/initrd.img
}}
menuentry 'Rescue an installed LSX-VE system' --class fedora --class gnu-linux --class gnu --class os {{
	linuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL={LABEL} inst.rescue quiet
	initrdefi /images/pxeboot/initrd.img
}}
menuentry 'UEFI firmware settings' {{
	fwsetup
}}
"""


def efi_boot() -> None:
    g = grub_cfg()
    for p in (TREE / "EFI" / "BOOT").glob("*.cfg"):
        if p.name.lower() == "grub.cfg":
            p.write_text(g, newline="\n")
    img = TREE / "images" / "efiboot.img"
    fat = fatimg.FatImage(img.read_bytes())
    fat.write("/EFI/BOOT/grub.cfg", g.encode())
    assert fat.read("/EFI/BOOT/grub.cfg") == g.encode()
    img.write_bytes(fat.bytes())


# 5x7 bitmap glyphs for the splash wordmark
GLYPHS = {
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "-": ["00000", "00000", "00000", "01110", "00000", "00000", "00000"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
}


def png(width: int, height: int, rows, alpha: bool = False) -> bytes:
    """rows: flat RGB (or RGBA with alpha=True) byte values per row."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def splash() -> None:
    w, h = 640, 480
    pix = [[0, 0, 0] * w for _ in range(h)]
    for y in range(h):  # navy -> graphite vertical gradient
        t = y / (h - 1)
        r, g, b = int(14 + 6 * t), int(24 + 4 * t), int(44 - 14 * t)
        row = pix[y]
        for x in range(w):
            row[3 * x:3 * x + 3] = [r, g, b]
    text, scale = "LSX-VE", 7
    tw = len(text) * 6 * scale - scale
    x0, y0 = (w - tw) // 2, 56
    for i, ch in enumerate(text):
        for gy, line in enumerate(GLYPHS[ch]):
            for gx, bit in enumerate(line):
                if bit == "1":
                    for yy in range(scale):
                        for xx in range(scale):
                            X, Y = x0 + (i * 6 + gx) * scale + xx, y0 + gy * scale + yy
                            pix[Y][3 * X:3 * X + 3] = [47, 111, 214] if ch in "-" else [230, 237, 247]
    for x in range(x0, x0 + tw):  # accent bar
        for y in range(y0 + 7 * scale + 14, y0 + 7 * scale + 18):
            pix[y][3 * x:3 * x + 3] = [47, 111, 214]
    (TREE / "isolinux" / "splash.png").write_bytes(png(w, h, pix))


def cpio_newc(files: dict) -> bytes:
    out = io.BytesIO()
    ino = 1
    def entry(name: str, data: bytes, mode: int):
        nonlocal ino
        nb = name.encode() + b"\x00"
        hdr = "070701" + "".join(f"{v:08x}" for v in (ino, mode, 0, 0, 1, int(time.time()), len(data), 0, 0, 0, 0,
                                                        len(nb), 0))
        out.write(hdr.encode() + nb)
        out.write(b"\x00" * ((4 - (110 + len(nb)) % 4) % 4))
        out.write(data)
        out.write(b"\x00" * ((4 - len(data) % 4) % 4))
        ino += 1
    for name, data in files.items():
        entry(name, data, 0o100644)
    entry("TRAILER!!!", b"", 0)
    return out.getvalue()


def product_img() -> None:
    stamp = (f"[Main]\nProduct=LSX-VE\nVersion={VERSION.rsplit('.', 1)[0]}\nBugURL=your distribution provided bug "
             f"reporting tool\nIsFinal=True\nUUID={time.strftime('%Y%m%d%H%M')}.x86_64\nVariant=LSXVE\n"
             f"[Compose]\nLSX-VE={VERSION}\n")
    ana = ROOT / "installer" / "anaconda"
    files = {
        ".buildstamp": stamp.encode(),
        # read after the Rocky Linux profile: LSX-VE partitioning, hidden spokes, stylesheet
        "etc/anaconda/conf.d/90-lsxve.conf": (ana / "90-lsxve.conf").read_bytes().replace(b"\r\n", b"\n"),
        "usr/share/anaconda/pixmaps/lsxve.css": (ana / "lsxve.css").read_bytes().replace(b"\r\n", b"\n"),
    }
    files.update(anaconda_pixmaps())
    (TREE / "images" / "product.img").write_bytes(gzip.compress(cpio_newc(files), 9, mtime=0))


def anaconda_pixmaps() -> dict:
    """Branding images for the graphical installer (sidebar wordmark, sidebar and top bar backgrounds)."""
    out = {}
    w, h = 230, 900  # sidebar background: navy gradient
    rows = []
    for y in range(h):
        t = y / (h - 1)
        rows.append([int(18 - 6 * t), int(33 - 9 * t), int(58 - 16 * t)] * w)
    out["usr/share/anaconda/pixmaps/sidebar-bg.png"] = png(w, h, rows)
    out["usr/share/anaconda/pixmaps/topbar-bg.png"] = png(32, 32, [[18, 33, 58] * 32 for _ in range(32)])
    text, scale = "LSX-VE", 5  # transparent wordmark with accent bar
    tw = len(text) * 6 * scale - scale
    w, h = tw + 20, 7 * scale + 30
    pix = [[0, 0, 0, 0] * w for _ in range(h)]

    def put(x, y, c):
        pix[y][4 * x:4 * x + 4] = c
    for i, ch in enumerate(text):
        for gy, line in enumerate(GLYPHS[ch]):
            for gx, bit in enumerate(line):
                if bit == "1":
                    for yy in range(scale):
                        for xx in range(scale):
                            put(10 + (i * 6 + gx) * scale + xx, 6 + gy * scale + yy,
                                [47, 111, 214, 255] if ch == "-" else [255, 255, 255, 255])
    for x in range(10, 10 + tw):
        for y in range(6 + 7 * scale + 10, 6 + 7 * scale + 14):
            put(x, y, [47, 111, 214, 255])
    out["usr/share/anaconda/pixmaps/sidebar-logo.png"] = png(w, h, pix, alpha=True)
    return out


def treeinfo() -> None:
    p = TREE / ".treeinfo"
    t = p.read_text()
    t = re.sub(r"^variants = .*$", "variants = Minimal,LSXVE", t, flags=re.M)
    t = re.sub(r"^(\[tree\][^\[]*?)variants = Minimal", r"\1variants = Minimal,LSXVE", t, flags=re.S)
    t = re.sub(r"\n*\[variant-LSXVE\][^\[]*", "\n", t).rstrip()  # idempotent re-runs
    t += ("\n\n[variant-LSXVE]\nid = LSXVE\nname = LSX-VE Hypervisor\npackages = Hypervisor/Packages\n"
          "repository = Hypervisor\ntype = variant\nuid = LSXVE\n")
    sha = hashlib.sha256((TREE / "images" / "efiboot.img").read_bytes()).hexdigest()
    t = re.sub(r"^images/efiboot.img = sha256:[0-9a-f]+", f"images/efiboot.img = sha256:{sha}", t, flags=re.M)
    p.write_text(t, newline="\n")
    (TREE / "LSX-VE-README.txt").write_text(
        f"LSX-VE {VERSION} - Linux Server eXtended Virtualization Environment\n\n"
        "Boot this media (UEFI or legacy BIOS) to install an LSX-VE virtualization host.\n"
        "After installation, manage the host with a web browser at https://<management-ip>/\n\n"
        "Based on Rocky Linux 9 (see LICENSE, EULA, COMMUNITY-CHARTER). LSX-VE adds the\n"
        "packages in Hypervisor/ (unmodified Rocky Linux RPMs) and the lsxve/ management payload.\n", newline="\n")


# --------------------------------------------------------------------------- 5. master
class Namer:
    """Map real names to unique ISO9660 level-3 8.3 identifiers (Rock Ridge keeps the real name)."""

    def __init__(self):
        self.used = {}

    def name(self, parent: str, real: str, is_dir: bool) -> str:
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


def master(out_iso: Path) -> None:
    log(f"mastering {out_iso.name} (volume {LABEL})")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, sys_ident="LINUX", vol_ident=LABEL, app_ident_str=f"LSX-VE {VERSION}",
            pub_ident_str="LSX-VE", preparer_ident_str="LSX-VE build.py", rock_ridge="1.09")
    namer = Namer()
    isopath = {"": ""}
    boot = {}
    count = 0
    for dirpath, dirs, files in os.walk(TREE):
        rel = os.path.relpath(dirpath, TREE).replace("\\", "/")
        rel = "" if rel == "." else rel
        dirs.sort()
        for d in dirs:
            r = f"{rel}/{d}" if rel else d
            ip = isopath[rel] + "/" + namer.name(isopath[rel], d, True)
            isopath[r] = ip
            iso.add_directory(ip, rr_name=d, file_mode=0o040555)
        for f in sorted(files):
            r = f"{rel}/{f}" if rel else f
            if r == "isolinux/boot.cat":
                continue
            ip = isopath[rel] + "/" + namer.name(isopath[rel], f, False) + ";1"
            iso.add_file(os.path.join(dirpath, f), ip, rr_name=f, file_mode=0o100444)
            boot[r] = ip
            count += 1
    log(f"{count} files added")
    iso.add_eltorito(boot["isolinux/isolinux.bin"], bootcatfile=isopath["isolinux"] + "/BOOT.CAT;1",
                     rr_bootcatname="boot.cat", boot_load_size=4, boot_info_table=True)
    iso.add_eltorito(boot["images/efiboot.img"], efi=True, media_name="noemul")
    # pycdlib pads a hybrid image to a whole 1 MiB "cylinder" and then writes the backup GPT
    # (33 x 512 bytes) into the last sectors of that size. If the ISO is already a cylinder
    # multiple, the padding is 0 and the backup GPT overwrites the tail of the last file.
    # Make sure the padding always has room for it.
    cyl = 64 * 32 * 512
    def tail_room() -> int:
        size = iso.pvd.space_size * iso.logical_block_size
        return (cyl - size % cyl) % cyl
    pad = 0
    while tail_room() < 64 * 1024:
        pad += 128 * 1024
        if pad > cyl:
            raise SystemExit("cannot align ISO for isohybrid GPT")
        if pad > 128 * 1024:
            iso.rm_file("/PADDING.BIN;1", rr_name="padding.bin")
        iso.add_fp(io.BytesIO(b"\0" * pad), pad, "/PADDING.BIN;1", rr_name="padding.bin", file_mode=0o100444)
    if pad:
        log(f"added {pad // 1024} KiB alignment padding for the backup GPT")
    iso.add_isohybrid(efi=True)
    out_iso.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_iso.with_suffix(".iso.part")
    last = [0.0]
    def prog(done, total, _):
        if time.time() - last[0] > 5 or done == total:
            last[0] = time.time()
            print(f"  writing {done * 100 // max(total, 1)}%", flush=True)
    iso.write(str(tmp), progress_cb=prog)
    iso.close()
    os.replace(tmp, out_iso)
    h = hashlib.sha256()
    with open(out_iso, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    (out_iso.parent / (out_iso.name + ".sha256")).write_text(f"{h.hexdigest()}  {out_iso.name}\n", newline="\n")
    log(f"done: {out_iso} ({out_iso.stat().st_size / 1048576:.0f} MiB) sha256 {h.hexdigest()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(ROOT / "Rocky-9.9-x86_64-minimal.iso"))
    ap.add_argument("--output", default=str(ROOT / "dist"))
    ap.add_argument("--skip-web", action="store_true", help="use the existing web/dist build")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--skip-guest-tools", action="store_true", help="do not build lsx-ve-guest-tools.iso")
    a = ap.parse_args()
    cfg = json.loads((HERE / "packages.json").read_text())
    src = Path(a.source)
    WORK.mkdir(parents=True, exist_ok=True)
    src_label = extract(src)
    cfg["base_version"] = re.search(r"^version = (.*)$", (TREE / ".treeinfo").read_text(), re.M).group(1)
    log(f"base: Rocky Linux {cfg['base_version']} ({src_label})")
    dist = build_web(a.skip_web)
    build_repo(cfg, a.offline)
    payload(dist)
    if a.skip_guest_tools:
        log("guest tools ISO skipped (--skip-guest-tools): 'Install LSX-VE Guest Tools' will report it missing")
    else:
        guesttools.build(cfg, CACHE, WORK, TREE / "lsxve" / "guest-tools", VERSION)
    kickstart(cfg)
    isolinux_cfg()
    splash()
    efi_boot()
    product_img()
    treeinfo()
    master(Path(a.output) / f"LSX-VE-{VERSION}-x86_64.iso")


if __name__ == "__main__":
    main()
