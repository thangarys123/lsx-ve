"""Authentication, sessions, CSRF, rate limiting and RBAC enforcement."""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from ..common import security
from ..common.db import Database
from .httpd import HTTPError, Request, Response, Route, json_response

COOKIE = "lsxve_session"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string."""

    def __init__(self):
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < now - window:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            if len(self._hits) > 50000:
                for k in [k for k, v in self._hits.items() if not v][:10000]:
                    del self._hits[k]
            return True

    def count(self, key: str, window: float) -> int:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            while dq and dq[0] < now - window:
                dq.popleft()
            return len(dq)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class AuthService:
    def __init__(self, db: Database, cfg: dict):
        self.db = db
        self.cfg = cfg
        self.limiter = RateLimiter()
        self._perm_cache: Dict[str, Tuple[float, frozenset]] = {}

    # -- roles -----------------------------------------------------------
    def role_permissions(self, role: str) -> frozenset:
        cached = self._perm_cache.get(role)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        r = self.db.one("SELECT permissions FROM roles WHERE name=?", (role,))
        perms = frozenset(json.loads(r["permissions"])) if r else frozenset()
        self._perm_cache[role] = (time.monotonic() + 5, perms)
        return perms

    def invalidate_roles(self) -> None:
        self._perm_cache.clear()

    # -- login -----------------------------------------------------------
    def login(self, username: str, password: str, ip: str, ua: str) -> Tuple[dict, str, str]:
        if not self.limiter.hit(f"login-ip:{ip}", 20, 300):
            self.db.audit(username, ip, "auth.login", username, "rate_limited")
            raise HTTPError(429, "Too many login attempts from this address. Try again later.", "rate_limited")
        max_fail = self.cfg["login_max_failures"]
        lock_s = self.cfg["login_lockout_seconds"]
        if self.limiter.count(f"login-fail:{username}", lock_s) >= max_fail:
            security.dummy_verify(password)
            self.db.audit(username, ip, "auth.login", username, "locked")
            raise HTTPError(429, "Account temporarily locked after repeated failures. Try again later.", "locked")
        user = self.db.one("SELECT * FROM users WHERE username=?", (username,))
        if not user:
            security.dummy_verify(password)
        ok = bool(user) and security.verify_password(password, user["password_hash"]) and bool(user["enabled"])
        if not ok:
            self.limiter.hit(f"login-fail:{username}", 10 ** 6, lock_s)
            self.db.audit(username, ip, "auth.login", username, "failure")
            raise HTTPError(401, "Invalid user name or password", "invalid_credentials")
        self.limiter.clear(f"login-fail:{username}")
        if security.needs_rehash(user["password_hash"]):
            # e.g. an account imported with the root password hash by the GUI installer
            self.db.execute("UPDATE users SET password_hash=? WHERE id=?",
                            (security.hash_password(password), user["id"]))
        token = security.new_token()
        csrf = security.new_token(24)
        now = time.time()
        self.db.execute("INSERT INTO sessions(token_hash, user_id, csrf, created, last_seen, ip, user_agent)"
                        " VALUES(?,?,?,?,?,?,?)", (security.token_digest(token), user["id"], csrf, now, now, ip, ua[:200]))
        self.db.execute("UPDATE users SET last_login=? WHERE id=?", (now, user["id"]))
        self.db.audit(username, ip, "auth.login", username, "success")
        return user, token, csrf

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (security.token_digest(token),))

    def resolve(self, token: str, touch: bool = True) -> Optional[Tuple[dict, dict]]:
        if not token or len(token) > 100:
            return None
        th = security.token_digest(token)
        s = self.db.one("SELECT * FROM sessions WHERE token_hash=?", (th,))
        if not s:
            return None
        now = time.time()
        if now - s["last_seen"] > self.cfg["session_idle_timeout"] or \
                now - s["created"] > self.cfg["session_absolute_timeout"]:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None
        user = self.db.one("SELECT * FROM users WHERE id=?", (s["user_id"],))
        if not user or not user["enabled"]:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None
        if touch and now - s["last_seen"] > 15:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
        return s, user

    def cookie(self, token: str, secure: bool = True) -> str:
        attrs = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if secure:
            attrs.append("Secure")
        return "; ".join(attrs)

    def clear_cookie(self, secure: bool = True) -> str:
        return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if secure else "")

    def purge_expired(self) -> None:
        now = time.time()
        self.db.execute("DELETE FROM sessions WHERE last_seen < ? OR created < ?",
                        (now - self.cfg["session_idle_timeout"], now - self.cfg["session_absolute_timeout"]))

    # -- per request middleware -----------------------------------------
    def check_origin(self, req: Request) -> bool:
        origin = req.headers.get("origin")
        if not origin:
            return req.method in SAFE_METHODS
        host = req.headers.get("host", "")
        allowed = {f"https://{host}", f"http://{host}"} | set(self.cfg.get("trusted_origins", []))
        return origin in allowed

    async def middleware(self, req: Request, route: Route) -> Optional[Response]:
        if not self.limiter.hit(f"api:{req.client_ip}", self.cfg["api_rate_per_minute"], 60):
            raise HTTPError(429, "Rate limit exceeded", "rate_limited")
        state_changing = req.method not in SAFE_METHODS or route.websocket
        if state_changing and not self.check_origin(req):
            raise HTTPError(403, "Cross-origin request rejected", "bad_origin")
        if not route.auth:
            return None
        res = self.resolve(req.cookies.get(COOKIE, ""))
        if not res:
            raise HTTPError(401, "Authentication required", "unauthenticated")
        req.session, req.user = res
        req.permissions = self.role_permissions(req.user["role"])
        if route.csrf and req.method not in SAFE_METHODS:
            sent = req.headers.get("x-csrf-token", "")
            if not sent or not security.consteq(sent, req.session["csrf"]):
                raise HTTPError(403, "Missing or invalid CSRF token", "csrf")
        if req.user["must_change_password"] and route.perm is not None and not route.perm.startswith("self"):
            raise HTTPError(403, "Password change required before continuing", "password_change_required")
        if route.perm and not route.perm.startswith("self") and route.perm not in req.permissions:
            raise HTTPError(403, f"Permission '{route.perm}' required", "forbidden")
        return None


def user_public(u: dict, perms: frozenset = frozenset()) -> dict:
    return {
        "id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"],
        "enabled": bool(u["enabled"]), "must_change_password": bool(u["must_change_password"]),
        "created": u["created"], "last_login": u["last_login"], "permissions": sorted(perms),
    }
