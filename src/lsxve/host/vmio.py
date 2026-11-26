"""VM import / export file work for lsxve-agent (root): OVA unpacking, disk conversion with qemu-img and
OVA packing. Every path must resolve inside the LSX-VE storage roots; nothing is ever overwritten."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

from ..common import config, log, proc, validate as v

LOG = log.get("lsxve.vmio")
QEMU_IMG = "/usr/bin/qemu-img"
ROOTS = tuple(Path(x) for x in ("/lsxve", "/var/lib/libvirt/images", "/srv", "/mnt", "/data"))  # = datastore prefixes
DISK_FORMATS = ("vmdk", "qcow2", "raw", "vpc", "vhdx", "vdi")
OVA_MEMBER_EXT = (".ovf", ".vmdk", ".mf", ".cert", ".nvram", ".iso", ".img")
TEMP_RE = re.compile(r"^(import|export)-[0-9a-f]{8}-[A-Za-z0-9._-]{1,120}$")


def _inside_roots(path: str, must_exist: bool) -> Path:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise v.ValidationError("path", "absolute path required")
    p = Path(os.path.realpath(path))
    if not any(p == r or r in p.parents for r in ROOTS):
        raise v.ValidationError("path", f"{path} is outside the LSX-VE storage")
    if must_exist and not p.is_file():
        raise v.ValidationError("path", f"{path} does not exist")
    if not must_exist and (p.exists() or os.path.islink(path)):
        raise v.ValidationError("path", f"{path} already exists")
    if not must_exist and not p.parent.is_dir():
        raise v.ValidationError("path", f"directory of {path} does not exist")
    return p


def _relabel(p: Path) -> None:
    if proc.which("/usr/sbin/restorecon"):
        proc.run(["/usr/sbin/restorecon", str(p)], check=False)


def read_ovf(path: str) -> dict:
    """The OVF descriptor of an .ova (first .ovf member) or an .ovf file."""
    p = _inside_roots(path, True)
    if p.suffix.lower() == ".ovf":
        if p.stat().st_size > 4 * 1024 * 1024:
            raise v.ValidationError("path", "OVF descriptor too large")
        return {"ovf": p.read_text(encoding="utf-8", errors="replace"), "members": [], "dir": str(p.parent)}
    with tarfile.open(p, "r:") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        ovf = next((m for m in members if m.name.lower().endswith(".ovf")), None)
        if ovf is None:
            raise v.ValidationError("path", "the OVA contains no .ovf descriptor")
        if ovf.size > 4 * 1024 * 1024:
            raise v.ValidationError("path", "OVF descriptor too large")
        text = tf.extractfile(ovf).read().decode("utf-8", errors="replace")
        return {"ovf": text, "members": [{"name": os.path.basename(m.name), "size": m.size} for m in members],
                "dir": str(p.parent)}


def extract_ova(path: str, dest_dir: str, tag: str) -> dict:
    """Unpack the disks of an OVA into dest_dir as import-<tag>-<name> (regular files only, no paths)."""
    p = _inside_roots(path, True)
    tag = v.string(tag, "tag", 8, 8, re.compile(r"^[0-9a-f]{8}$"))
    dest = Path(os.path.realpath(dest_dir))
    if not any(dest == r or r in dest.parents for r in ROOTS) or not dest.is_dir():
        raise v.ValidationError("dest_dir", "invalid destination")
    out: Dict[str, str] = {}
    with tarfile.open(p, "r:") as tf:
        members = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(OVA_MEMBER_EXT)]
        need = sum(m.size for m in members)
        st = os.statvfs(dest)
        if need > st.f_bavail * st.f_frsize:
            raise v.ValidationError("dest_dir", f"not enough free space to unpack the OVA ({need // 2 ** 20} MiB)")
        for m in members:
            base = os.path.basename(m.name)
            if not re.match(r"^[A-Za-z0-9._ ()+-]{1,120}$", base) or base in out:
                raise v.ValidationError("path", f"unsupported file name in the OVA: {base!r}")
            target = dest / f"import-{tag}-{base.replace(' ', '_')}"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            with os.fdopen(fd, "wb") as fh, tf.extractfile(m) as src:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            out[base] = str(target)
    LOG.info("unpacked %s: %s", p, ", ".join(out))
    return {"files": out}


def image_info(path: str) -> dict:
    p = _inside_roots(path, True)
    cp = proc.run([QEMU_IMG, "info", "--output=json", "-U", str(p)], timeout=60)
    info = json.loads(cp.stdout)
    return {"format": info.get("format"), "virtual_size": info.get("virtual-size"),
            "actual_size": info.get("actual-size"), "backing": info.get("backing-filename")}


def convert(src: str, dst: str, dst_format: str = "qcow2", src_format: Optional[str] = None,
            stream_optimized: bool = False) -> dict:
    """qemu-img convert (no backing files: an image that references another file is refused)."""
    s = _inside_roots(src, True)
    d = _inside_roots(dst, False)
    dst_format = v.choice(dst_format, "dst_format", ("qcow2", "vmdk", "raw"))
    info = image_info(str(s))
    if info["backing"]:
        raise v.ValidationError("src", "images with a backing file cannot be imported; consolidate them first")
    if src_format is not None:
        src_format = v.choice(src_format, "src_format", DISK_FORMATS)
    else:
        src_format = info["format"] if info["format"] in DISK_FORMATS else "raw"
    argv = [QEMU_IMG, "convert", "-f", src_format, "-O", dst_format]
    if dst_format == "vmdk" and stream_optimized:
        argv += ["-o", "subformat=streamOptimized"]
    if dst_format == "qcow2":
        argv += ["-o", "preallocation=metadata"]
    proc.run([*argv, str(s), str(d)], timeout=6 * 3600)
    os.chmod(d, 0o600)
    _relabel(d)
    LOG.info("converted %s (%s) -> %s (%s)", s, src_format, d, dst_format)
    return {"path": str(d), "size": d.stat().st_size, "virtual_size": info["virtual_size"]}


def pack_ova(ovf_text: str, ovf_name: str, disks: List[str], out: str) -> dict:
    """Write an OVA (tar: .ovf first, then .mf with SHA256 digests, then the disks)."""
    o = _inside_roots(out, False)
    if not o.name.endswith(".ova"):
        raise v.ValidationError("out", "the output must be an .ova file")
    ovf_name = v.string(ovf_name, "ovf_name", 5, 120, re.compile(r"^[A-Za-z0-9._-]+\.ovf$"))
    paths = [_inside_roots(p, True) for p in disks]
    ovf_bytes = ovf_text.encode("utf-8")
    lines = [f"SHA256({ovf_name})= {hashlib.sha256(ovf_bytes).hexdigest()}"]
    names = []
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        name = re.sub(r"^export-[0-9a-f]{8}-", "", p.name)
        names.append(name)
        lines.append(f"SHA256({name})= {h.hexdigest()}")
    mf = ("\n".join(lines) + "\n").encode()
    import io
    fd = os.open(o, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w|", format=tarfile.USTAR_FORMAT) as tf:
        for name, data in ((ovf_name, ovf_bytes), (ovf_name[:-4] + ".mf", mf)):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), 0o644
            tf.addfile(ti, io.BytesIO(data))
        for p, name in zip(paths, names):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = p.stat().st_size, 0o644
            with open(p, "rb") as fh:
                tf.addfile(ti, fh)
    _relabel(o)
    return {"path": str(o), "size": o.stat().st_size}


def cleanup(paths: List[str]) -> dict:
    """Remove temporary import-/export- files created by this module."""
    removed = []
    for path in paths:
        p = Path(os.path.realpath(path))
        if not any(r in p.parents for r in ROOTS) or not TEMP_RE.match(p.name) or not p.is_file():
            continue
        p.unlink()
        removed.append(str(p))
    return {"removed": removed}


def guest_tools_iso() -> Optional[str]:
    p = config.GUEST_TOOLS_ISO
    return str(p) if p.is_file() else None
