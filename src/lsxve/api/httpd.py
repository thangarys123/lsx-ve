"""Minimal, hardened HTTP/1.1 + WebSocket server on asyncio (stdlib only).

Only what LSX-VE needs is implemented: fixed routes, JSON bodies, streaming request
bodies for uploads, static files for the web console and RFC 6455 WebSockets.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..common import log

LOG = log.get("lsxve.http")

MAX_HEADER_BYTES = 16 * 1024
MAX_JSON_BODY = 2 * 1024 * 1024
READ_TIMEOUT = 30
KEEPALIVE_TIMEOUT = 75

STATUS_TEXT = {
    101: "Switching Protocols", 200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    301: "Moved Permanently", 302: "Found", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 411: "Length Required",
    413: "Payload Too Large", 415: "Unsupported Media Type", 422: "Unprocessable Entity",
    429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self' wss: ws:; worker-src 'self' blob:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    ),
}


class HTTPError(Exception):
    def __init__(self, status: int, message: str, code: str = "", details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or STATUS_TEXT.get(status, "error").lower().replace(" ", "_")
        self.details = details


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    client_ip: str
    reader: asyncio.StreamReader
    content_length: int = 0
    body: bytes = b""
    params: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    session: Optional[dict] = None
    user: Optional[dict] = None
    permissions: frozenset = frozenset()
    _json: Any = None

    def json(self) -> Any:
        if self._json is None:
            if not self.body:
                self._json = {}
            else:
                ctype = self.headers.get("content-type", "").split(";")[0].strip()
                if ctype != "application/json":
                    raise HTTPError(415, "Content-Type must be application/json")
                try:
                    self._json = json.loads(self.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise HTTPError(400, "Malformed JSON body")
        return self._json

    def q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        v = self.query.get(name)
        return v[0] if v else default

    @property
    def username(self) -> str:
        return self.user["username"] if self.user else "-"


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    file_path: Optional[Path] = None
    cookies: List[str] = field(default_factory=list)
    chunks: Any = None  # async iterator of bytes: streamed download, connection closes afterwards


def json_response(data: Any, status: int = 200, headers: Optional[dict] = None) -> Response:
    h = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    if headers:
        h.update(headers)
    return Response(status, json.dumps(data, default=str, separators=(",", ":")).encode(), h)


Handler = Callable[[Request], Awaitable[Response]]
WSHandler = Callable[[Request, "WebSocket"], Awaitable[None]]


@dataclass
class Route:
    method: str
    regex: re.Pattern
    handler: Any
    perm: Optional[str]
    auth: bool
    csrf: bool
    stream: bool
    websocket: bool
    max_body: int


class Router:
    def __init__(self):
        self.routes: List[Route] = []

    def add(self, method: str, pattern: str, handler, perm: Optional[str] = None, auth: bool = True,
            csrf: bool = True, stream: bool = False, websocket: bool = False, max_body: int = MAX_JSON_BODY):
        rx = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self.routes.append(Route(method, re.compile(rx), handler, perm, auth, csrf, stream, websocket, max_body))

    def match(self, method: str, path: str) -> Tuple[Optional[Route], Dict[str, str], bool]:
        path_matched = False
        for r in self.routes:
            m = r.regex.match(path)
            if m:
                path_matched = True
                if r.method == method or (method == "HEAD" and r.method == "GET"):
                    return r, {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}, True
        return None, {}, path_matched


# ---------------------------------------------------------------------------
# WebSocket (RFC 6455) - server side
# ---------------------------------------------------------------------------
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClosed(Exception):
    pass


class WebSocket:
    MAX_FRAME = 16 * 1024 * 1024

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, subprotocol: Optional[str]):
        self.reader = reader
        self.writer = writer
        self.subprotocol = subprotocol
        self.closed = False
        self._wlock = asyncio.Lock()

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed and opcode != 0x8:
            raise WebSocketClosed()
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        async with self._wlock:
            self.writer.write(bytes(header) + payload)
            await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(0x1, text.encode("utf-8"))

    async def send_json(self, obj: Any) -> None:
        await self.send_text(json.dumps(obj, default=str, separators=(",", ":")))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(0x2, data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._send_frame(0x8, struct.pack("!H", code) + reason.encode()[:120])
        except (ConnectionError, RuntimeError, WebSocketClosed):
            pass

    async def receive(self) -> Tuple[int, bytes]:
        """Return (opcode, payload) for the next complete text/binary message."""
        message = b""
        msg_opcode = None
        while True:
            b1, b2 = await self.reader.readexactly(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if not b2 & 0x80:
                raise WebSocketClosed("client frames must be masked")
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if n > self.MAX_FRAME or len(message) + n > self.MAX_FRAME:
                await self.close(1009)
                raise WebSocketClosed("frame too large")
            mask = await self.reader.readexactly(4)
            data = _unmask(await self.reader.readexactly(n), mask) if n else b""
            if opcode == 0x8:
                await self.close()
                raise WebSocketClosed("closed by peer")
            if opcode == 0x9:
                await self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                msg_opcode = opcode
                message = data
            elif opcode == 0x0:
                message += data
            else:
                raise WebSocketClosed("bad opcode")
            if fin:
                return msg_opcode or 0x2, message


def _unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
Middleware = Callable[[Request, Route], Awaitable[Optional[Response]]]


class HTTPServer:
    def __init__(self, router: Router, web_root: Path, before: Middleware,
                 server_header: str = "LSX-VE"):
        self.router = router
        self.web_root = web_root
        self.before = before
        self.server_header = server_header
        self.hsts = True

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if isinstance(peer, tuple) else "unix"
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), KEEPALIVE_TIMEOUT)
                except asyncio.LimitOverrunError:
                    await self._write_simple(writer, 400, "Header too large")
                    return
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                    return
                if len(head) > MAX_HEADER_BYTES:
                    await self._write_simple(writer, 400, "Header too large")
                    return
                keep = await self._handle_one(head, reader, writer, ip)
                if not keep:
                    return
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:  # pragma: no cover
            LOG.exception("connection handler crashed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_one(self, head: bytes, reader, writer, ip: str) -> bool:
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if not version.startswith("HTTP/1."):
                raise ValueError
        except ValueError:
            await self._write_simple(writer, 400, "Bad request line")
            return False
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                await self._write_simple(writer, 400, "Bad header")
                return False
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        parsed = urllib.parse.urlsplit(target)
        path = urllib.parse.unquote(parsed.path)
        if "\x00" in path or "/../" in path + "/" or not path.startswith("/"):
            await self._write_simple(writer, 400, "Bad path")
            return False
        if "transfer-encoding" in headers:
            await self._write_simple(writer, 411, "Chunked request bodies are not supported")
            return False
        try:
            clen = int(headers.get("content-length", "0") or 0)
            if clen < 0:
                raise ValueError
        except ValueError:
            await self._write_simple(writer, 400, "Bad Content-Length")
            return False
        req = Request(method=method.upper(), path=path, query=urllib.parse.parse_qs(parsed.query),
                      headers=headers, client_ip=ip, reader=reader, content_length=clen)
        req.cookies = _parse_cookies(headers.get("cookie", ""))
        keep_alive = headers.get("connection", "").lower() != "close" and version == "HTTP/1.1"
        started = time.monotonic()
        resp: Optional[Response] = None
        route = None
        try:
            if path.startswith("/api/"):
                route, params, path_matched = self.router.match(req.method, path)
                if not route:
                    raise HTTPError(405 if path_matched else 404,
                                    "Method not allowed" if path_matched else "No such API endpoint")
                req.params = params
                if not route.stream:
                    if clen > route.max_body:
                        raise HTTPError(413, "Request body too large")
                    if clen:
                        req.body = await asyncio.wait_for(reader.readexactly(clen), READ_TIMEOUT)
                elif clen > route.max_body:
                    raise HTTPError(413, "Upload too large")
                early = await self.before(req, route)
                if early is not None:
                    resp = early
                elif route.websocket:
                    await self._upgrade(req, route, reader, writer)
                    return False
                else:
                    resp = await route.handler(req)
                    if route.stream and req.content_length:
                        # handler must consume the body; if it did not, drop the connection
                        keep_alive = False
            else:
                if clen:
                    raise HTTPError(413, "Unexpected body")
                if req.method not in ("GET", "HEAD"):
                    raise HTTPError(405, "Method not allowed")
                resp = self._static(path)
        except HTTPError as e:
            body = {"error": {"code": e.code, "message": e.message}}
            if e.details is not None:
                body["error"]["details"] = e.details
            resp = json_response(body, e.status)
            if route and route.stream and clen:
                keep_alive = False
        except asyncio.TimeoutError:
            resp = json_response({"error": {"code": "timeout", "message": "Request timed out"}}, 408)
            keep_alive = False
        except asyncio.IncompleteReadError:
            return False
        except Exception as e:  # pragma: no cover
            LOG.exception("unhandled error on %s %s", method, path)
            resp = json_response({"error": {"code": "internal", "message": "Internal server error"}}, 500)
        if resp.chunks is not None:
            keep_alive = False
        await self._write_response(writer, req, resp, keep_alive)
        if path.startswith("/api/"):
            log.kv(LOG, 20, "request", method=req.method, path=path, status=resp.status, ip=ip,
                   user=req.username, ms=round((time.monotonic() - started) * 1000, 1))
        return keep_alive

    def _static(self, path: str) -> Response:
        root = self.web_root
        rel = path.lstrip("/") or "index.html"
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise HTTPError(404, "Not found")
        if not candidate.is_file():
            if "." in rel.rsplit("/", 1)[-1]:
                raise HTTPError(404, "Not found")
            candidate = root / "index.html"  # SPA route
            if not candidate.is_file():
                return Response(503, b"LSX-VE web console is not installed", {"Content-Type": "text/plain"})
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if "/assets/" in path else "no-cache"
        return Response(200, b"", {"Content-Type": ctype, "Cache-Control": cache}, file_path=candidate)

    async def _write_simple(self, writer, status: int, text: str) -> None:
        body = text.encode()
        writer.write(f"HTTP/1.1 {status} {STATUS_TEXT.get(status, '')}\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except ConnectionError:
            pass

    async def _write_response(self, writer, req: Request, resp: Response, keep_alive: bool) -> None:
        headers = dict(SECURITY_HEADERS)
        if self.hsts:
            headers["Strict-Transport-Security"] = "max-age=31536000"
        headers["Server"] = self.server_header
        headers.update(resp.headers)
        if resp.chunks is None:
            size = resp.file_path.stat().st_size if resp.file_path else len(resp.body)
            headers["Content-Length"] = str(size)
        headers["Connection"] = "keep-alive" if keep_alive else "close"
        lines = [f"HTTP/1.1 {resp.status} {STATUS_TEXT.get(resp.status, '')}"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        lines += [f"Set-Cookie: {c}" for c in resp.cookies]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        if req.method != "HEAD":
            if resp.chunks is not None:
                try:
                    async for chunk in resp.chunks:
                        writer.write(chunk)
                        await writer.drain()
                finally:
                    close = getattr(resp.chunks, "aclose", None)
                    if close:
                        await close()
            elif resp.file_path:
                with open(resp.file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(256 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
            else:
                writer.write(resp.body)
        await writer.drain()

    async def _upgrade(self, req: Request, route: Route, reader, writer) -> None:
        key = req.headers.get("sec-websocket-key", "")
        if req.headers.get("upgrade", "").lower() != "websocket" or not key or \
                req.headers.get("sec-websocket-version") != "13":
            await self._write_response(writer, req, json_response(
                {"error": {"code": "bad_request", "message": "WebSocket upgrade required"}}, 400), False)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        offered = [p.strip() for p in req.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        sub = "binary" if "binary" in offered else (offered[0] if offered else None)
        lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Accept: {accept}", f"Server: {self.server_header}"]
        if sub:
            lines.append(f"Sec-WebSocket-Protocol: {sub}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer, sub)
        try:
            await route.handler(req, ws)
        except (WebSocketClosed, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await ws.close()


def _parse_cookies(raw: str) -> Dict[str, str]:
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def redirect_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, https_port: int) -> None:
    """Port 80 listener: redirect every request to HTTPS."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        host = "localhost"
        target = "/"
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) >= 2 and parts[1].startswith("/"):
            target = parts[1]
        for line in lines[1:]:
            if line.lower().startswith("host:"):
                host = line.split(":", 1)[1].strip().split(":")[0]
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,255}", host):
            host = "localhost"
        port = "" if https_port == 443 else f":{https_port}"
        loc = f"https://{host}{port}{target}"
        writer.write(f"HTTP/1.1 301 Moved Permanently\r\nLocation: {loc}\r\nContent-Length: 0\r\n"
                     f"Connection: close\r\n\r\n".encode("latin-1"))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
