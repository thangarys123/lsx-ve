"""Resolve, download and publish the LSX-VE extra package repository.

The Rocky Linux Minimal ISO does not contain the hypervisor stack (qemu-kvm,
libvirt, OVMF, swtpm, python3-libvirt, ...). This tool computes the RPM
dependency closure of the requested packages against the Minimal repository on
the source ISO plus the official Rocky Linux BaseOS/AppStream repositories,
downloads the missing RPMs (sha256 verified against the signed-repo metadata)
and writes a self-contained yum repository (repodata/primary + filelists).

Only packages that the ISO cannot already satisfy are added. If an upstream
package replaces one present on the ISO, all sibling sub-packages built from
the same source RPM are refreshed too, so version-locked families stay
consistent (glibc, systemd, ...).

Pure standard library; works on Linux and Windows.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import lzma
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

NS = {"c": "http://linux.duke.edu/metadata/common", "rpm": "http://linux.duke.edu/metadata/rpm",
      "r": "http://linux.duke.edu/metadata/repo"}
C = "{%s}" % NS["c"]
R = "{%s}" % NS["rpm"]
ARCHES = ("x86_64", "noarch")
UA = "lsxve-build/1.0 (+repo mirror client)"


# --------------------------------------------------------------------------- rpm version compare
def _rpmvercmp(a: str, b: str) -> int:
    if a == b:
        return 0
    seg = re.compile(r"(~|\^|[0-9]+|[A-Za-z]+)")
    xa, xb = seg.findall(a), seg.findall(b)
    # rpm also strips separators; findall does that. Handle ~ and ^ semantics.
    i = 0
    while True:
        ca = xa[i] if i < len(xa) else None
        cb = xb[i] if i < len(xb) else None
        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            continue
        if ca == "^" or cb == "^":
            if ca is None:
                return -1
            if cb is None:
                return 1
            if ca != "^":
                return 1
            if cb != "^":
                return -1
            i += 1
            continue
        if ca is None or cb is None:
            if ca is None and cb is None:
                return 0
            return -1 if ca is None else 1
        if ca.isdigit() and cb.isdigit():
            na, nb = int(ca), int(cb)
            if na != nb:
                return 1 if na > nb else -1
        elif ca.isdigit():
            return 1
        elif cb.isdigit():
            return -1
        elif ca != cb:
            return 1 if ca > cb else -1
        i += 1


def evr_cmp(a: Tuple[str, str, Optional[str]], b: Tuple[str, str, Optional[str]]) -> int:
    ea, eb = int(a[0] or 0), int(b[0] or 0)
    if ea != eb:
        return 1 if ea > eb else -1
    c = _rpmvercmp(a[1] or "", b[1] or "")
    if c:
        return c
    if a[2] is None or b[2] is None:  # release omitted in a requirement -> ignored
        return 0
    return _rpmvercmp(a[2], b[2])


OPS = {"EQ": lambda c: c == 0, "GE": lambda c: c >= 0, "LE": lambda c: c <= 0, "GT": lambda c: c > 0,
       "LT": lambda c: c < 0}


# --------------------------------------------------------------------------- metadata model
class Dep:
    __slots__ = ("name", "flags", "evr", "pre")

    def __init__(self, name, flags=None, evr=None, pre=False):
        self.name, self.flags, self.evr, self.pre = name, flags, evr, pre

    def __repr__(self):
        return self.name + (f" {self.flags} {self.evr}" if self.flags else "")


class Pkg:
    __slots__ = ("name", "arch", "evr", "href", "checksum", "ctype", "size", "srpm", "provides", "requires", "files",
                 "xml", "repo", "base_url")

    def nevra(self):
        e, v, r = self.evr
        return f"{self.name}-{(e + ':') if e and e != '0' else ''}{v}-{r}.{self.arch}"

    def filename(self):
        return self.href.rsplit("/", 1)[-1]


def _dep(el) -> Dep:
    fl = el.get("flags")
    evr = (el.get("epoch", "0"), el.get("ver"), el.get("rel")) if fl else None
    return Dep(el.get("name"), fl, evr, el.get("pre") == "1")


def parse_primary(data: bytes, repo: str, base_url: str = "") -> List[Pkg]:
    out = []
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag != C + "package":
            continue
        if el.get("type") != "rpm":
            el.clear()
            continue
        p = Pkg()
        p.name = el.findtext(C + "name")
        p.arch = el.findtext(C + "arch")
        v = el.find(C + "version")
        p.evr = (v.get("epoch", "0"), v.get("ver"), v.get("rel"))
        p.href = el.find(C + "location").get("href")
        ck = el.find(C + "checksum")
        p.checksum, p.ctype = ck.text, ck.get("type")
        p.size = int(el.find(C + "size").get("package"))
        fmt = el.find(C + "format")
        p.srpm = fmt.findtext(R + "sourcerpm") or ""
        p.provides = [_dep(e) for e in fmt.findall(f"{R}provides/{R}entry")]
        p.requires = [_dep(e) for e in fmt.findall(f"{R}requires/{R}entry")]
        p.files = [f.text for f in fmt.findall(C + "file")]
        p.repo, p.base_url = repo, base_url
        p.xml = ET.tostring(el, encoding="unicode") if repo != "iso" else None
        out.append(p)
        el.clear()
    return out


def decompress(name: str, data: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
        except ImportError:
            import zstandard  # type: ignore
            return zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 31)
        return zstd.decompress(data)
    if name.endswith(".bz2"):
        import bz2
        return bz2.decompress(data)
    return data


def fetch(url: str, dest: Optional[Path] = None, sha256: Optional[str] = None, retries: int = 4) -> bytes:
    if dest and dest.exists() and sha256:
        if hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
            return dest.read_bytes()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if sha256 and hashlib.sha256(data).hexdigest() != sha256:
                raise IOError(f"checksum mismatch for {url}")
            if dest:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
            return data
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (attempt + 1))
    raise IOError(f"download failed: {url}: {last}")


def load_remote_repo(base_url: str, repo: str, cache: Path) -> List[Pkg]:
    base_url = base_url.rstrip("/") + "/"
    repomd = fetch(base_url + "repodata/repomd.xml")
    root = ET.fromstring(repomd)
    for d in root.findall("r:data", NS):
        if d.get("type") == "primary":
            href = d.find("r:location", NS).get("href")
            ck = d.find("r:checksum", NS).text
            raw = fetch(base_url + href, cache / "meta" / repo / href.rsplit("/", 1)[-1], ck)
            return parse_primary(decompress(href, raw), repo, base_url)
    raise IOError(f"no primary metadata in {base_url}")


# --------------------------------------------------------------------------- resolver
class Pool:
    def __init__(self, iso: List[Pkg], remote: List[Pkg]):
        self.iso = [p for p in iso if p.arch in ARCHES]
        self.remote = [p for p in remote if p.arch in ARCHES]
        self.iso_names = {p.name for p in self.iso}
        self.by_prov: Dict[str, List[Tuple[Pkg, Dep]]] = {}
        self.by_file: Dict[str, List[Pkg]] = {}
        self.by_name: Dict[str, List[Pkg]] = {}
        for p in self.iso + self.remote:
            for d in p.provides:
                self.by_prov.setdefault(d.name, []).append((p, d))
            for f in p.files:
                self.by_file.setdefault(f, []).append(p)
            self.by_name.setdefault(p.name, []).append(p)

    @staticmethod
    def _matches(prov: Dep, req: Dep) -> bool:
        if not req.flags or not prov.flags or not prov.evr or not prov.evr[1]:
            return True
        if prov.flags != "EQ":  # range provides are rare
            return True
        c = evr_cmp(prov.evr, (req.evr[0], req.evr[1], req.evr[2]))
        return OPS[req.flags](c)

    def providers(self, req: Dep) -> List[Pkg]:
        if req.name.startswith("/"):
            cands = list(self.by_file.get(req.name, []))
            cands += [p for p, d in self.by_prov.get(req.name, [])]
        else:
            cands = [p for p, d in self.by_prov.get(req.name, []) if self._matches(d, req)]
        uniq = {id(p): p for p in cands}
        return list(uniq.values())


def tokenize(s: str) -> List[str]:
    return re.findall(r"\(|\)|[^\s()]+", s)


def parse_rich(tokens: List[str], i: int = 0):
    """Return (tree, next_index). tree = ('dep', Dep) | (op, [children...])."""
    assert tokens[i] == "("
    i += 1
    items, ops = [], []
    while tokens[i] != ")":
        if tokens[i] == "(":
            t, i = parse_rich(tokens, i)
            items.append(t)
        elif tokens[i] in ("and", "or", "if", "else", "with", "without", "unless"):
            ops.append(tokens[i])
            i += 1
        else:
            name = tokens[i]
            i += 1
            if i < len(tokens) and tokens[i] in ("=", ">=", "<=", ">", "<"):
                op = {"=": "EQ", ">=": "GE", "<=": "LE", ">": "GT", "<": "LT"}[tokens[i]]
                evr = tokens[i + 1]
                i += 2
                m = re.match(r"(?:(\d+):)?([^-]+)(?:-(.+))?$", evr)
                items.append(("dep", Dep(name, op, (m.group(1) or "0", m.group(2), m.group(3)))))
            else:
                items.append(("dep", Dep(name)))
    return (ops[0] if ops else "single", items, ops), i + 1


class Resolver:
    def __init__(self, pool: Pool, exclude: Iterable[str] = ()):
        self.pool = pool
        self.exclude = [re.compile(x) for x in exclude]
        self.selected: Dict[str, Pkg] = {}     # name -> remote pkg chosen for the extra repo
        self.satisfied_iso: Set[str] = set()   # iso package names relied on
        self.log: List[str] = []

    def excluded(self, name: str) -> bool:
        return any(x.match(name) for x in self.exclude)

    def _best(self, cands: List[Pkg], want_name: Optional[str] = None) -> Optional[Pkg]:
        cands = [c for c in cands if not self.excluded(c.name)]
        if not cands:
            return None
        def key(p: Pkg):
            return (p.name == want_name, p.arch == "x86_64")
        # highest EVR per name, prefer exact name
        best = None
        for p in cands:
            if best is None:
                best = p
                continue
            kb, kp = key(best), key(p)
            if kp > kb or (kp == kb and p.name == best.name and evr_cmp(p.evr, best.evr) > 0):
                best = p
            elif kp == kb and p.name != best.name and len(p.name) < len(best.name):
                best = p
        return best

    def is_installed_choice(self, req: Dep) -> bool:
        """Already satisfied by the selection or by an ISO package that stays in place."""
        for p in self.pool.providers(req):
            if p.repo == "iso" and p.name not in self.selected:
                return True
            if p.repo != "iso" and self.selected.get(p.name) is p:
                return True
        return False

    def satisfy(self, req: Dep, why: str) -> bool:
        if req.name.startswith("rpmlib(") or req.name.startswith("config("):
            return True
        if req.name.startswith("("):
            tree, _ = parse_rich(tokenize(req.name))
            return self._satisfy_tree(tree, why)
        if self.is_installed_choice(req):
            return True
        provs = self.pool.providers(req)
        iso = [p for p in provs if p.repo == "iso"]
        if iso:
            # ISO package satisfies (it would be installed from the ISO repo)
            self.satisfied_iso.add(iso[0].name)
            return True
        remote = [p for p in provs if p.repo != "iso"]
        best = self._best(remote, req.name)
        if not best:
            self.log.append(f"UNRESOLVED {req} (required by {why})")
            return False
        self.add(best, why)
        return True

    def _holds(self, tree) -> bool:
        kind, items, ops = tree
        if kind in ("single", "and", "with"):
            return all(self._holds_item(i) for i in items)
        if kind == "or":
            return any(self._holds_item(i) for i in items)
        if kind == "if":
            return (not self._holds_item(items[1])) or self._holds_item(items[0])
        return False

    def _holds_item(self, item) -> bool:
        if item[0] == "dep":
            return self.is_installed_choice(item[1])
        return self._holds(item)

    def _sat_item(self, item, why) -> bool:
        if item[0] == "dep":
            return self.satisfy(item[1], why)
        return self._satisfy_tree(item, why)

    def _satisfy_tree(self, tree, why) -> bool:
        kind, items, ops = tree
        if kind == "single":
            return self._sat_item(items[0], why)
        if kind in ("and", "with"):
            return all(self._sat_item(i, why) for i in items)
        if kind == "or":
            if any(self._holds_item(i) for i in items):
                return True
            for i in items:
                if self._sat_item(i, why):
                    return True
            return False
        if kind == "if":
            # A if B [else C]
            cond = items[1]
            if self._holds_item(cond):
                return self._sat_item(items[0], why)
            if "else" in ops and len(items) > 2:
                return self._sat_item(items[2], why)
            return True
        if kind == "unless":
            if not self._holds_item(items[1]):
                return self._sat_item(items[0], why)
            if "else" in ops and len(items) > 2:
                return self._sat_item(items[2], why)
            return True
        if kind == "without":
            return self._sat_item(items[0], why)
        return True

    def add(self, p: Pkg, why: str) -> None:
        if self.selected.get(p.name) is p:
            return
        self.selected[p.name] = p
        self.log.append(f"+ {p.nevra()}  ({why})")
        for r in p.requires:
            self.satisfy(r, p.name)
        if p.name in self.pool.iso_names:
            # refresh version-locked siblings from the same source package
            for q in self.pool.iso:
                if q.srpm and q.srpm.rsplit("-", 2)[0] == p.srpm.rsplit("-", 2)[0] and q.name not in self.selected:
                    up = self._best([x for x in self.pool.by_name.get(q.name, []) if x.repo != "iso"], q.name)
                    if up and evr_cmp(up.evr, q.evr) > 0:
                        self.add(up, f"sibling of {p.name}")

    def request(self, name: str) -> bool:
        if self.excluded(name):
            raise ValueError(f"{name} is excluded")
        if name in self.pool.iso_names:
            self.satisfied_iso.add(name)
            iso_p = [p for p in self.pool.iso if p.name == name][0]
            for r in iso_p.requires:
                self.satisfy(r, name)
            return True
        cands = [p for p in self.pool.by_name.get(name, []) if p.repo != "iso"]
        best = self._best(cands, name)
        if not best:
            self.log.append(f"MISSING requested package {name}")
            return False
        self.add(best, "requested")
        return True

    def fixpoint(self) -> None:
        """Re-evaluate conditional (rich) dependencies until nothing changes."""
        for _ in range(10):
            before = len(self.selected)
            for p in list(self.selected.values()):
                for r in p.requires:
                    if r.name.startswith("("):
                        self.satisfy(r, p.name)
            if len(self.selected) == before:
                return


# --------------------------------------------------------------------------- repo writer
def write_repo(pkgs: List[Pkg], outdir: Path, rpm_cache: Path) -> None:
    pkgdir = outdir / "Packages"
    pkgdir.mkdir(parents=True, exist_ok=True)
    total = len(pkgs)
    for i, p in enumerate(sorted(pkgs, key=lambda x: x.name), 1):
        cached = rpm_cache / p.filename()
        print(f"  [{i}/{total}] {p.filename()} ({p.size / 1048576:.1f} MiB)", flush=True)
        if p.ctype != "sha256":
            raise IOError(f"unsupported checksum type {p.ctype}")
        fetch(p.base_url + p.href, cached, p.checksum)
        dst = pkgdir / p.filename()
        if not dst.exists() or dst.stat().st_size != cached.stat().st_size:
            dst.write_bytes(cached.read_bytes())
    # primary: reuse the upstream XML of each package, relocate href
    prim = ['<?xml version="1.0" encoding="UTF-8"?>',
            f'<metadata xmlns="{NS["c"]}" xmlns:rpm="{NS["rpm"]}" packages="{len(pkgs)}">']
    fl = ['<?xml version="1.0" encoding="UTF-8"?>',
          f'<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="{len(pkgs)}">']
    ET.register_namespace("", NS["c"])
    ET.register_namespace("rpm", NS["rpm"])
    for p in sorted(pkgs, key=lambda x: x.name):
        el = ET.fromstring(p.xml)
        el.find(C + "location").set("href", "Packages/" + p.filename())
        el.find(C + "location").attrib.pop("{http://www.w3.org/XML/1998/namespace}base", None)
        s = ET.tostring(el, encoding="unicode")
        s = s.replace(f' xmlns="{NS["c"]}"', "").replace(f' xmlns:rpm="{NS["rpm"]}"', "")
        s = s.replace("ns0:", "").replace(":ns0", "")
        prim.append(s)
        e, v, r = p.evr
        files = "".join(f"<file>{_xml_escape(f)}</file>" for f in p.files)
        fl.append(f'<package pkgid="{p.checksum}" name="{p.name}" arch="{p.arch}">'
                  f'<version epoch="{e}" ver="{v}" rel="{r}"/>{files}</package>')
    prim.append("</metadata>")
    fl.append("</filelists>")
    rd = outdir / "repodata"
    if rd.exists():
        for f in rd.iterdir():
            f.unlink()
    rd.mkdir(parents=True, exist_ok=True)
    entries = []
    for kind, text in (("primary", "\n".join(prim)), ("filelists", "\n".join(fl))):
        raw = text.encode()
        gz = gzip.compress(raw, 9, mtime=0)
        sha = hashlib.sha256(gz).hexdigest()
        name = f"{sha}-{kind}.xml.gz"
        (rd / name).write_bytes(gz)
        entries.append(f'  <data type="{kind}">\n'
                       f'    <checksum type="sha256">{sha}</checksum>\n'
                       f'    <open-checksum type="sha256">{hashlib.sha256(raw).hexdigest()}</open-checksum>\n'
                       f'    <location href="repodata/{name}"/>\n'
                       f'    <timestamp>{int(time.time())}</timestamp>\n'
                       f'    <size>{len(gz)}</size>\n    <open-size>{len(raw)}</open-size>\n  </data>')
    (rd / "repomd.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                   f'<repomd xmlns="{NS["r"]}" xmlns:rpm="{NS["rpm"]}">\n'
                                   f'  <revision>{int(time.time())}</revision>\n' + "\n".join(entries) +
                                   "\n</repomd>\n")


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def resolve(iso_primary: bytes, remote_urls: Dict[str, object], requested: List[str], exclude: List[str],
            cache: Path) -> Tuple[Resolver, List[Pkg]]:
    """remote_urls: name -> base URL, or name -> {"url": ..., "only": [regex, ...]}.

    With "only", just the packages whose names match one of the patterns are taken from that
    repository (used for third-party repositories such as EPEL, so they can never replace or
    satisfy dependencies with anything but the packages explicitly allowed)."""
    iso = parse_primary(iso_primary, "iso")
    remote: List[Pkg] = []
    for name, src in remote_urls.items():
        url, only = (src["url"], src.get("only")) if isinstance(src, dict) else (src, None)
        print(f"loading repository metadata: {name}", flush=True)
        pkgs = load_remote_repo(url, name, cache)
        if only:
            pats = [re.compile(x) for x in only]
            pkgs = [p for p in pkgs if any(x.fullmatch(p.name) for x in pats)]
        remote += pkgs
    pool = Pool(iso, remote)
    res = Resolver(pool, exclude)
    for n in requested:
        res.request(n)
    res.fixpoint()
    return res, list(res.selected.values())


if __name__ == "__main__":
    print(__doc__)
