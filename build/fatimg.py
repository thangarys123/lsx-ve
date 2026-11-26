"""Minimal FAT12/FAT16 image editor (enough to rewrite files in efiboot.img).

Supports reading a file by path and replacing the contents of an existing
file (clusters are freed and re-allocated). Long-file-name entries are
matched case-insensitively against the 8.3 short name.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple


class FatImage:
    def __init__(self, data: bytes):
        self.d = bytearray(data)
        (self.bps, self.spc, self.reserved, self.nfats, self.root_entries, total16, _media, self.fatsz,
         ) = struct.unpack_from("<HBHBHHBH", self.d, 11)
        total32 = struct.unpack_from("<I", self.d, 32)[0]
        self.total = total16 or total32
        if self.fatsz == 0:
            raise ValueError("FAT32 is not supported")
        self.root_start = (self.reserved + self.nfats * self.fatsz) * self.bps
        self.root_size = self.root_entries * 32
        self.data_start = self.root_start + ((self.root_size + self.bps - 1) // self.bps) * self.bps
        self.csize = self.bps * self.spc
        self.nclusters = (self.total * self.bps - self.data_start) // self.csize
        self.fat12 = self.nclusters < 4085
        self.eoc = 0xFFF if self.fat12 else 0xFFFF

    # ------------------------------------------------------------- FAT table
    def _fat_get(self, n: int) -> int:
        base = self.reserved * self.bps
        if self.fat12:
            off = base + n + n // 2
            v = struct.unpack_from("<H", self.d, off)[0]
            return (v >> 4) if n & 1 else (v & 0xFFF)
        return struct.unpack_from("<H", self.d, base + n * 2)[0]

    def _fat_set(self, n: int, val: int) -> None:
        for f in range(self.nfats):
            base = (self.reserved + f * self.fatsz) * self.bps
            if self.fat12:
                off = base + n + n // 2
                v = struct.unpack_from("<H", self.d, off)[0]
                v = ((v & 0x000F) | (val << 4)) if n & 1 else ((v & 0xF000) | (val & 0xFFF))
                struct.pack_into("<H", self.d, off, v & 0xFFFF)
            else:
                struct.pack_into("<H", self.d, base + n * 2, val)

    def _chain(self, first: int) -> List[int]:
        out, c = [], first
        while 2 <= c < (0xFF8 if self.fat12 else 0xFFF8):
            out.append(c)
            c = self._fat_get(c)
            if len(out) > self.nclusters:
                raise ValueError("FAT loop")
        return out

    def _coff(self, c: int) -> int:
        return self.data_start + (c - 2) * self.csize

    # ------------------------------------------------------------- directories
    def _dir_entries(self, cluster: Optional[int]) -> List[Tuple[int, bytes]]:
        """Return (absolute offset, 32-byte entry) for a directory (None = root)."""
        regions = [(self.root_start, self.root_size)] if cluster is None else \
            [(self._coff(c), self.csize) for c in self._chain(cluster)]
        out = []
        for start, size in regions:
            for off in range(start, start + size, 32):
                e = bytes(self.d[off:off + 32])
                if e[0] == 0:
                    return out
                out.append((off, e))
        return out

    @staticmethod
    def _short(e: bytes) -> str:
        name = e[0:8].decode("ascii", "replace").rstrip()
        ext = e[8:11].decode("ascii", "replace").rstrip()
        return f"{name}.{ext}" if ext else name

    def _lookup(self, path: str) -> Tuple[int, bytes]:
        parts = [p for p in path.strip("/").split("/") if p]
        cluster = None
        for i, part in enumerate(parts):
            lfn = []
            found = None
            for off, e in self._dir_entries(cluster):
                if e[0] == 0xE5:
                    lfn = []
                    continue
                if e[11] == 0x0F:
                    chars = e[1:11] + e[14:26] + e[28:32]
                    lfn.insert(0, chars.decode("utf-16-le", "ignore").split("\x00")[0])
                    continue
                if e[11] & 0x08:  # volume label
                    lfn = []
                    continue
                long_name = "".join(lfn)
                lfn = []
                if part.upper() in (self._short(e).upper(), long_name.upper()):
                    found = (off, e)
                    break
            if not found:
                raise FileNotFoundError(path)
            if i < len(parts) - 1:
                cluster = struct.unpack_from("<H", found[1], 26)[0]
            else:
                return found
        raise FileNotFoundError(path)

    def read(self, path: str) -> bytes:
        off, e = self._lookup(path)
        first = struct.unpack_from("<H", e, 26)[0]
        size = struct.unpack_from("<I", e, 28)[0]
        buf = b"".join(bytes(self.d[self._coff(c):self._coff(c) + self.csize]) for c in self._chain(first))
        return buf[:size]

    def write(self, path: str, content: bytes) -> None:
        off, e = self._lookup(path)
        first = struct.unpack_from("<H", e, 26)[0]
        for c in self._chain(first):
            self._fat_set(c, 0)
        need = max(1, (len(content) + self.csize - 1) // self.csize)
        free = [c for c in range(2, self.nclusters + 2) if self._fat_get(c) == 0][:need]
        if len(free) < need:
            raise IOError("FAT image is full")
        for i, c in enumerate(free):
            self._fat_set(c, free[i + 1] if i + 1 < len(free) else self.eoc)
            chunk = content[i * self.csize:(i + 1) * self.csize]
            o = self._coff(c)
            self.d[o:o + self.csize] = chunk.ljust(self.csize, b"\x00")
        struct.pack_into("<H", self.d, off + 26, free[0] if content else 0)
        struct.pack_into("<I", self.d, off + 28, len(content))

    def bytes(self) -> bytes:
        return bytes(self.d)
