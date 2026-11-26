"""Client used by the (unprivileged) API to call lsxve-agent."""
from __future__ import annotations

import itertools
import json
import os
import socket
import threading
from typing import Any

from ..common import config


class AgentError(Exception):
    def __init__(self, kind: str, message: str, field: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.field = field


class AgentClient:
    def __init__(self, path=None, timeout: float = 120):
        self.path = str(path or config.AGENT_SOCKET)
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._local = threading.local()

    def call(self, op: str, timeout: float = None, **args) -> Any:
        if not hasattr(socket, "AF_UNIX"):
            raise AgentError("unavailable", "Host agent is not available on this platform")
        rid = next(self._ids)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout or self.timeout)
            s.connect(self.path)
        except OSError as e:
            raise AgentError("unavailable", f"Host agent is not reachable ({e.strerror or e})")
        try:
            s.sendall((json.dumps({"id": rid, "op": op, "args": args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            raise AgentError("timeout", f"Host agent did not answer {op} in time")
        finally:
            s.close()
        if not buf:
            raise AgentError("unavailable", "Host agent closed the connection")
        return self._result(buf)

    @staticmethod
    def _result(buf: bytes):
        resp = json.loads(buf)
        if not resp.get("ok"):
            e = resp.get("error", {})
            raise AgentError(e.get("type", "internal"), e.get("message", "agent error"), e.get("field", ""))
        return resp.get("result")

    def call_fd(self, op: str, timeout: float = 30, **args):
        """Call an operation that answers with a file descriptor (e.g. a console pty). Returns (result, fd)."""
        if not hasattr(socket, "AF_UNIX"):
            raise AgentError("unavailable", "Host agent is not available on this platform")
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.path)
        except OSError as e:
            raise AgentError("unavailable", f"Host agent is not reachable ({e.strerror or e})")
        fds: list = []
        try:
            s.sendall((json.dumps({"id": next(self._ids), "op": op, "args": args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk, got, _flags, _addr = socket.recv_fds(s, 65536, 1)
                fds += got
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            raise AgentError("timeout", f"Host agent did not answer {op} in time")
        finally:
            s.close()
        try:
            result = self._result(buf) if buf else None
            if not buf:
                raise AgentError("unavailable", "Host agent closed the connection")
            if not fds:
                raise AgentError("internal", "Host agent did not return a terminal")
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise
        for fd in fds[1:]:
            os.close(fd)
        return result, fds[0]
