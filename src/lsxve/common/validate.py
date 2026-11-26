"""Input validation helpers shared by the API and the privileged agent.

Every value coming from a client passes through one of these functions before it
is used. They return the normalised value or raise ValidationError.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable, Optional


class ValidationError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


RE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
RE_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
RE_USERNAME = re.compile(r"^[a-z_][a-z0-9_.-]{1,31}$")
RE_IFNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}$")
RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RE_DEVNAME = re.compile(r"^(sd[a-z]{1,2}|vd[a-z]{1,2}|xvd[a-z]{1,2}|nvme\d+n\d+|hd[a-z]|mmcblk\d+)$")
RE_TZ = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
RE_IQN = re.compile(r"^(iqn\.\d{4}-\d{2}\.[a-z0-9.-]+(:[^\s]{1,200})?|eui\.[0-9A-Fa-f]{16})$")
RE_EXPORT = re.compile(r"^/[A-Za-z0-9_./-]{0,250}$")
RE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()+-]{0,200}$")


def require(d: Any, field: str):
    if not isinstance(d, dict) or field not in d:
        raise ValidationError(field, "is required")
    return d[field]


def string(v: Any, field: str, min_len: int = 0, max_len: int = 255, pattern: Optional[re.Pattern] = None,
           strip: bool = True) -> str:
    if not isinstance(v, str):
        raise ValidationError(field, "must be a string")
    if strip:
        v = v.strip()
    if any(ord(c) < 32 for c in v):
        raise ValidationError(field, "contains control characters")
    if len(v) < min_len or len(v) > max_len:
        raise ValidationError(field, f"length must be between {min_len} and {max_len}")
    if pattern is not None and not pattern.match(v):
        raise ValidationError(field, "has an invalid format")
    return v


def text(v: Any, field: str, max_len: int = 4000) -> str:
    """Free text (descriptions) - newlines allowed, other control chars not."""
    if not isinstance(v, str):
        raise ValidationError(field, "must be a string")
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise ValidationError(field, "contains control characters")
    if len(v) > max_len:
        raise ValidationError(field, f"must be at most {max_len} characters")
    return v


def integer(v: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, str) and re.fullmatch(r"-?\d{1,18}", v.strip()):
            v = int(v)
        else:
            raise ValidationError(field, "must be an integer")
    if v < lo or v > hi:
        raise ValidationError(field, f"must be between {lo} and {hi}")
    return v


def boolean(v: Any, field: str) -> bool:
    if not isinstance(v, bool):
        raise ValidationError(field, "must be true or false")
    return v


def choice(v: Any, field: str, options: Iterable[str]) -> str:
    opts = list(options)
    if v not in opts:
        raise ValidationError(field, f"must be one of: {', '.join(opts)}")
    return v


def name(v: Any, field: str = "name") -> str:
    return string(v, field, 1, 63, RE_NAME)


def uuid(v: Any, field: str = "uuid") -> str:
    return string(v, field, 36, 36, RE_UUID).lower()


def hostname(v: Any, field: str = "hostname") -> str:
    v = string(v, field, 1, 253).lower().rstrip(".")
    if not all(RE_HOSTNAME_LABEL.match(p) for p in v.split(".")):
        raise ValidationError(field, "is not a valid hostname")
    return v


def username(v: Any, field: str = "username") -> str:
    return string(v, field, 2, 32, RE_USERNAME)


def password(v: Any, field: str = "password") -> str:
    if not isinstance(v, str):
        raise ValidationError(field, "must be a string")
    if len(v) < 8 or len(v) > 256:
        raise ValidationError(field, "must be 8-256 characters")
    classes = sum(bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if classes < 3:
        raise ValidationError(field, "must contain at least 3 of: lowercase, uppercase, digit, symbol")
    if any(ord(c) < 32 for c in v):
        raise ValidationError(field, "contains control characters")
    return v


def ifname(v: Any, field: str = "interface") -> str:
    return string(v, field, 1, 15, RE_IFNAME)


def ipv4_interface(v: Any, field: str) -> str:
    """Address with prefix, e.g. 192.168.1.10/24"""
    try:
        raw = string(v, field, 9, 18)
        if "/" not in raw:
            raise ValueError("prefix required")
        iface = ipaddress.IPv4Interface(raw)
    except ValueError:
        raise ValidationError(field, "must be an IPv4 address with prefix (a.b.c.d/nn)")
    if iface.network.prefixlen in (0,) or iface.ip.is_multicast or iface.ip.is_unspecified:
        raise ValidationError(field, "is not a usable host address")
    return str(iface)


def ipv6_interface(v: Any, field: str) -> str:
    try:
        raw = string(v, field, 4, 49)
        if "/" not in raw:
            raise ValueError("prefix required")
        iface = ipaddress.IPv6Interface(raw)
    except ValueError:
        raise ValidationError(field, "must be an IPv6 address with prefix")
    return str(iface)


def ipv4_network(v: Any, field: str) -> str:
    """Network or single address, e.g. 10.0.0.0/8 or 192.168.1.20 (stored as a.b.c.d/nn)."""
    try:
        net = ipaddress.IPv4Network(string(v, field, 7, 18), strict=False)
    except ValueError:
        raise ValidationError(field, "must be an IPv4 network (a.b.c.d/nn) or address")
    return str(net)


def ip_address(v: Any, field: str, version: Optional[int] = None) -> str:
    try:
        ip = ipaddress.ip_address(string(v, field, 2, 45))
    except ValueError:
        raise ValidationError(field, "must be an IP address")
    if version and ip.version != version:
        raise ValidationError(field, f"must be an IPv{version} address")
    return str(ip)


def host_or_ip(v: Any, field: str) -> str:
    s = string(v, field, 1, 253)
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return hostname(s, field)


def list_of(v: Any, field: str, fn, max_items: int = 16) -> list:
    if not isinstance(v, list):
        raise ValidationError(field, "must be a list")
    if len(v) > max_items:
        raise ValidationError(field, f"must have at most {max_items} items")
    return [fn(x, f"{field}[{i}]") for i, x in enumerate(v)]


def mac(v: Any, field: str = "mac") -> str:
    s = string(v, field, 17, 17, re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")).lower()
    if int(s[:2], 16) & 1:
        raise ValidationError(field, "must be a unicast address")
    return s
