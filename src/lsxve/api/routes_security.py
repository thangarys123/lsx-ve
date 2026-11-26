"""Authentication, users, roles, events, tasks, audit log and the live WebSocket."""
from __future__ import annotations

import asyncio
import json
import time

from .. import PRODUCT, __version__
from ..common import db as dbm, security, validate as v
from .auth import COOKIE, user_public
from .context import Ctx, handler
from .httpd import HTTPError, Request, Response, WebSocket, json_response


def register(ctx: Ctx, secure_cookie: bool) -> None:
    r = ctx.router
    db = ctx.db
    auth = ctx.auth

    # -- auth -----------------------------------------------------------
    @handler
    async def login(req: Request) -> Response:
        b = req.json()
        username = v.string(b.get("username", ""), "username", 1, 64)
        password = b.get("password", "")
        if not isinstance(password, str) or not password or len(password) > 256:
            raise HTTPError(401, "Invalid user name or password", "invalid_credentials")
        user, token, csrf = await ctx.run(auth.login, username, password, req.client_ip,
                                          req.headers.get("user-agent", ""))
        ctx.bus.event("info", "auth", f"User {username} logged in from {req.client_ip}", "", "", username)
        resp = json_response({"user": user_public(user, auth.role_permissions(user["role"])), "csrf": csrf})
        resp.cookies.append(auth.cookie(token, secure_cookie))
        return resp

    @handler
    async def logout(req: Request) -> Response:
        auth.logout(req.cookies.get(COOKIE, ""))
        ctx.audit(req, "auth.logout", req.username)
        resp = json_response({"ok": True})
        resp.cookies.append(auth.clear_cookie(secure_cookie))
        return resp

    @handler
    async def session(req: Request) -> Response:
        return json_response({"user": user_public(req.user, req.permissions), "csrf": req.session["csrf"],
                              "product": PRODUCT, "version": __version__,
                              "idle_timeout": ctx.cfg["session_idle_timeout"]})

    @handler
    async def change_password(req: Request) -> Response:
        b = req.json()
        cur = b.get("current_password", "")
        new = v.password(v.require(b, "new_password"), "new_password")
        if not isinstance(cur, str) or not security.verify_password(cur, req.user["password_hash"]):
            ctx.audit(req, "auth.password.change", req.username, "failure")
            raise HTTPError(403, "Current password is incorrect", "bad_password")
        if cur == new:
            raise v.ValidationError("new_password", "must differ from the current password")
        h = await ctx.run(security.hash_password, new)
        db.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed=? WHERE id=?",
                   (h, time.time(), req.user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (req.user["id"], req.session["token_hash"]))
        ctx.audit(req, "auth.password.change", req.username)
        return json_response({"ok": True})

    r.add("POST", "/api/v1/auth/login", login, auth=False, csrf=False, max_body=4096)
    r.add("POST", "/api/v1/auth/logout", logout, "self")
    r.add("GET", "/api/v1/auth/session", session, "self")
    r.add("POST", "/api/v1/auth/password", change_password, "self")

    # -- users ----------------------------------------------------------
    def _role(name) -> str:
        name = v.string(name, "role", 1, 40)
        if not db.one("SELECT name FROM roles WHERE name=?", (name,)):
            raise v.ValidationError("role", "unknown role")
        return name

    def _admins_left(excluding: int) -> int:
        rows = db.all("SELECT u.id, r.permissions FROM users u JOIN roles r ON r.name=u.role WHERE u.enabled=1 AND u.id<>?",
                      (excluding,))
        return sum(1 for x in rows if "security.manage" in json.loads(x["permissions"]))

    @handler
    async def users(req: Request) -> Response:
        rows = db.all("SELECT * FROM users ORDER BY username")
        return json_response([user_public(u) for u in rows])

    @handler
    async def user_create(req: Request) -> Response:
        b = req.json()
        username = v.username(v.require(b, "username"))
        pw = v.password(v.require(b, "password"))
        role = _role(v.require(b, "role"))
        full = v.string(b.get("full_name", ""), "full_name", 0, 100)
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            raise HTTPError(409, "User already exists", "conflict")
        h = await ctx.run(security.hash_password, pw)
        uid = dbm.create_user(db, username, h, role, full, v.boolean(b.get("must_change_password", True),
                                                                      "must_change_password"))
        ctx.audit(req, "security.user.create", username, role=role)
        ctx.bus.event("info", "auth", f"User {username} created with role {role}", "", "", req.username)
        return json_response(user_public(db.one("SELECT * FROM users WHERE id=?", (uid,))), 201)

    @handler
    async def user_update(req: Request) -> Response:
        uid = v.integer(req.params["id"], "id", 1, 10 ** 9)
        u = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPError(404, "No such user")
        b = req.json()
        sets, vals = [], []
        if "full_name" in b:
            sets.append("full_name=?")
            vals.append(v.string(b["full_name"], "full_name", 0, 100))
        if "role" in b:
            role = _role(b["role"])
            if "security.manage" not in auth.role_permissions(role) and _admins_left(uid) == 0:
                raise HTTPError(409, "At least one enabled user must keep user-management rights")
            sets.append("role=?")
            vals.append(role)
        if "enabled" in b:
            en = v.boolean(b["enabled"], "enabled")
            if not en and uid == req.user["id"]:
                raise HTTPError(409, "You cannot disable your own account")
            if not en and _admins_left(uid) == 0:
                raise HTTPError(409, "At least one enabled administrator is required")
            sets.append("enabled=?")
            vals.append(int(en))
        if "password" in b:
            sets += ["password_hash=?", "password_changed=?", "must_change_password=?"]
            vals += [await ctx.run(security.hash_password, v.password(b["password"])), time.time(),
                     int(v.boolean(b.get("must_change_password", True), "must_change_password"))]
        if not sets:
            raise v.ValidationError("body", "no changes supplied")
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        if "password" in b or b.get("enabled") is False or "role" in b:
            db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, req.session["token_hash"]))
        ctx.audit(req, "security.user.update", u["username"], fields=sorted(k for k in b if k != "password"),
                  password_reset="password" in b)
        return json_response(user_public(db.one("SELECT * FROM users WHERE id=?", (uid,))))

    @handler
    async def user_delete(req: Request) -> Response:
        uid = v.integer(req.params["id"], "id", 1, 10 ** 9)
        u = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPError(404, "No such user")
        if uid == req.user["id"]:
            raise HTTPError(409, "You cannot delete your own account")
        if _admins_left(uid) == 0:
            raise HTTPError(409, "At least one enabled administrator is required")
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        ctx.audit(req, "security.user.delete", u["username"])
        ctx.bus.event("warning", "auth", f"User {u['username']} deleted", "", "", req.username)
        return json_response({"ok": True})

    @handler
    async def roles(req: Request) -> Response:
        rows = db.all("SELECT * FROM roles ORDER BY builtin DESC, name")
        counts = {x["role"]: x["n"] for x in db.all("SELECT role, COUNT(*) n FROM users GROUP BY role")}
        for x in rows:
            x["permissions"] = json.loads(x["permissions"])
            x["builtin"] = bool(x["builtin"])
            x["users"] = counts.get(x["name"], 0)
        return json_response({"roles": rows, "permissions": dbm.PERMISSIONS})

    def _perms(lst) -> list:
        return sorted(set(v.list_of(lst, "permissions", lambda x, f: v.choice(x, f, dbm.PERMISSIONS), 64)))

    @handler
    async def role_create(req: Request) -> Response:
        b = req.json()
        name = v.string(v.require(b, "name"), "name", 2, 40, v.RE_NAME)
        if db.one("SELECT name FROM roles WHERE name=?", (name,)):
            raise HTTPError(409, "Role already exists")
        db.execute("INSERT INTO roles(name, description, permissions, builtin) VALUES(?,?,?,0)",
                   (name, v.string(b.get("description", ""), "description", 0, 200), json.dumps(_perms(b.get("permissions", [])))))
        auth.invalidate_roles()
        ctx.audit(req, "security.role.create", name)
        return json_response({"ok": True}, 201)

    @handler
    async def role_update(req: Request) -> Response:
        name = req.params["name"]
        row = db.one("SELECT * FROM roles WHERE name=?", (name,))
        if not row:
            raise HTTPError(404, "No such role")
        if row["builtin"]:
            raise HTTPError(409, "Built-in roles cannot be modified")
        b = req.json()
        perms = _perms(b.get("permissions", json.loads(row["permissions"])))
        db.execute("UPDATE roles SET description=?, permissions=? WHERE name=?",
                   (v.string(b.get("description", row["description"]), "description", 0, 200), json.dumps(perms), name))
        auth.invalidate_roles()
        if "security.manage" not in perms:
            if _admins_left(0) == 0:
                db.execute("UPDATE roles SET permissions=? WHERE name=?", (row["permissions"], name))
                raise HTTPError(409, "This change would leave no administrator")
        ctx.audit(req, "security.role.update", name, permissions=perms)
        return json_response({"ok": True})

    @handler
    async def role_delete(req: Request) -> Response:
        name = req.params["name"]
        row = db.one("SELECT * FROM roles WHERE name=?", (name,))
        if not row:
            raise HTTPError(404, "No such role")
        if row["builtin"]:
            raise HTTPError(409, "Built-in roles cannot be deleted")
        if db.one("SELECT id FROM users WHERE role=?", (name,)):
            raise HTTPError(409, "Role is assigned to users")
        db.execute("DELETE FROM roles WHERE name=?", (name,))
        auth.invalidate_roles()
        ctx.audit(req, "security.role.delete", name)
        return json_response({"ok": True})

    r.add("GET", "/api/v1/users", users, "security.manage")
    r.add("POST", "/api/v1/users", user_create, "security.manage")
    r.add("PATCH", "/api/v1/users/{id}", user_update, "security.manage")
    r.add("DELETE", "/api/v1/users/{id}", user_delete, "security.manage")
    r.add("GET", "/api/v1/roles", roles, "security.manage")
    r.add("POST", "/api/v1/roles", role_create, "security.manage")
    r.add("PUT", "/api/v1/roles/{name}", role_update, "security.manage")
    r.add("DELETE", "/api/v1/roles/{name}", role_delete, "security.manage")

    # -- events / tasks / audit ----------------------------------------
    @handler
    async def events(req: Request) -> Response:
        limit = v.integer(req.q("limit", "200"), "limit", 1, 2000)
        where, params = [], []
        if req.q("severity"):
            where.append("severity=?")
            params.append(v.choice(req.q("severity"), "severity", ("info", "warning", "error")))
        if req.q("category"):
            where.append("category=?")
            params.append(v.choice(req.q("category"), "category", ("vm", "storage", "network", "host", "auth")))
        if req.q("target"):
            where.append("target=?")
            params.append(v.string(req.q("target"), "target", 1, 120))
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return json_response(db.all(sql, (*params, limit)))

    @handler
    async def tasks(req: Request) -> Response:
        st = req.q("state")
        if st:
            v.choice(st, "state", ("active", "queued", "running", "success", "error"))
        return json_response(ctx.tasks.list(v.integer(req.q("limit", "100"), "limit", 1, 1000), st))

    @handler
    async def task(req: Request) -> Response:
        t = ctx.tasks.get(v.string(req.params["id"], "id", 1, 32))
        if not t:
            raise HTTPError(404, "No such task")
        return json_response(t)

    @handler
    async def audit(req: Request) -> Response:
        limit = v.integer(req.q("limit", "200"), "limit", 1, 5000)
        rows = db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        for x in rows:
            try:
                x["details"] = json.loads(x["details"])
            except ValueError:
                pass
        return json_response(rows)

    r.add("GET", "/api/v1/events", events, "logs.view")
    r.add("GET", "/api/v1/tasks", tasks, "host.view")
    r.add("GET", "/api/v1/tasks/{id}", task, "host.view")
    r.add("GET", "/api/v1/audit", audit, "logs.view")

    # -- live updates ---------------------------------------------------
    TYPE_PERMS = {"metrics": "host.view", "task": "host.view", "event": "logs.view", "vm": "vm.view", "ct": "ct.view"}

    async def live(req: Request, ws: WebSocket) -> None:
        q = ctx.bus.subscribe()
        token = req.cookies.get(COOKIE, "")
        await ws.send_json({"type": "hello", "data": {"user": req.user["username"], "version": __version__}})

        async def reader():
            while True:
                await ws.receive()  # client pings / ignored

        rtask = asyncio.ensure_future(reader())
        try:
            last_check = time.monotonic()
            while not rtask.done():
                try:
                    msg = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    msg = {"type": "ping", "data": time.time()}
                if time.monotonic() - last_check > 30:  # re-validate the session periodically
                    last_check = time.monotonic()
                    res = auth.resolve(token, touch=False)
                    if not res:
                        await ws.send_json({"type": "session_expired"})
                        break
                    req.permissions = auth.role_permissions(res[1]["role"])
                need = TYPE_PERMS.get(msg["type"])
                if need and need not in req.permissions:
                    continue
                await ws.send_json(msg)
        finally:
            rtask.cancel()
            ctx.bus.unsubscribe(q)

    r.add("GET", "/api/v1/ws", live, "self", websocket=True)
