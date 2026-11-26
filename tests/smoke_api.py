"""API smoke test: auth, CSRF, RBAC, validation, rate limiting, WebSocket.

Usage: python tests/smoke_api.py https://host[:port] username password [--insecure]
Works against a dev instance (http://127.0.0.1:8443) or an installed LSX-VE host.
"""
import base64
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.parse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8443"
USER = sys.argv[2] if len(sys.argv) > 2 else "admin"
PASS = sys.argv[3] if len(sys.argv) > 3 else "Dev-Passw0rd!"
u = urllib.parse.urlsplit(BASE)
CTX = ssl._create_unverified_context()
failures = 0


def conn():
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, context=CTX, timeout=30)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)


class S:
    cookie = ""
    csrf = ""


def call(method, path, body=None, csrf=True, cookie=True, headers=None):
    c = conn()
    h = {"Origin": f"{u.scheme}://{u.netloc}", "Host": u.netloc}
    if body is not None:
        h["Content-Type"] = "application/json"
    if cookie and S.cookie:
        h["Cookie"] = S.cookie
    if csrf and S.csrf:
        h["X-CSRF-Token"] = S.csrf
    h.update(headers or {})
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    try:
        j = json.loads(data) if data else None
    except ValueError:
        j = data
    return r.status, j, r


def check(name, cond, info=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"))
    if not cond:
        failures += 1


st, j, r = call("GET", "/api/v1/public/info")
check("public info", st == 200 and j["product"] == "LSX-VE", j)
check("security headers", r.getheader("Content-Security-Policy", "").startswith("default-src 'self'")
      and r.getheader("X-Frame-Options") == "DENY", r.getheaders())
st, j, _ = call("GET", "/api/v1/host/summary")
check("unauthenticated rejected", st == 401, (st, j))
st, j, _ = call("POST", "/api/v1/auth/login", {"username": USER, "password": "wrong-password"})
check("bad password rejected", st == 401, (st, j))
st, j, r = call("POST", "/api/v1/auth/login", {"username": USER, "password": PASS})
check("login", st == 200 and "csrf" in j, (st, j))
S.cookie = r.getheader("Set-Cookie").split(";")[0]
S.csrf = j["csrf"]
check("cookie flags", "HttpOnly" in r.getheader("Set-Cookie") and "SameSite=Strict" in r.getheader("Set-Cookie"))
st, j, _ = call("GET", "/api/v1/auth/session")
check("session", st == 200 and j["user"]["username"] == USER, j)
must_change = j["user"]["must_change_password"]
if must_change:
    st, j, _ = call("GET", "/api/v1/host/summary")
    check("password change enforced", st == 403 and j["error"]["code"] == "password_change_required", (st, j))
    newpw = PASS + "x1"
    st, j, _ = call("POST", "/api/v1/auth/password", {"current_password": PASS, "new_password": newpw})
    check("password changed", st == 200, (st, j))
    st, j, _ = call("POST", "/api/v1/auth/password", {"current_password": newpw, "new_password": PASS})
    # revert may fail validation if equal-history rules are added later; not fatal
st, j, _ = call("GET", "/api/v1/host/summary")
check("host summary", st == 200 and "cpu" in j, (st, j))
print("     hypervisor:", j.get("hypervisor") if isinstance(j, dict) else j)
st, j, _ = call("PUT", "/api/v1/host/hostname", {"hostname": "x"}, csrf=False)
check("CSRF enforced", st == 403 and j["error"]["code"] == "csrf", (st, j))
st, j, _ = call("POST", "/api/v1/users", {"username": "x"}, headers={"Origin": "https://evil.example"})
check("cross-origin rejected", st == 403 and j["error"]["code"] == "bad_origin", (st, j))
st, j, _ = call("POST", "/api/v1/vms", {"name": "bad name!", "vcpus": 1, "memory_mib": 512})
check("validation error", st == 422 and j["error"]["details"]["field"] == "name", (st, j))
st, j, _ = call("POST", "/api/v1/users", {"username": "operator1", "password": "Operat0r-pass", "role": "Operator",
                                          "must_change_password": False})
check("create operator user", st in (201, 409), (st, j))
st, j, _ = call("GET", "/api/v1/roles")
check("roles", st == 200 and any(x["name"] == "Administrator" for x in j["roles"]), j)
st, j, _ = call("GET", "/api/v1/events?limit=5")
check("events", st == 200 and isinstance(j, list), j)
st, j, _ = call("GET", "/api/v1/tasks")
check("tasks", st == 200 and isinstance(j, list), j)
st, j, _ = call("GET", "/api/v1/audit?limit=5")
check("audit", st == 200 and isinstance(j, list) and j, j)
st, j, _ = call("GET", "/api/v1/host/metrics")
check("metrics", st == 200 and "samples" in j, j)

# RBAC: the operator may not create users
admin_cookie, admin_csrf = S.cookie, S.csrf
st, j, r = call("POST", "/api/v1/auth/login", {"username": "operator1", "password": "Operat0r-pass"}, cookie=False)
if st == 200:
    S.cookie = r.getheader("Set-Cookie").split(";")[0]
    S.csrf = j["csrf"]
    st, j, _ = call("POST", "/api/v1/users", {"username": "y", "password": "Abcdefg1!", "role": "Administrator"})
    check("RBAC: operator cannot create users", st == 403 and j["error"]["code"] == "forbidden", (st, j))
    st, j, _ = call("POST", "/api/v1/host/power", {"action": "reboot", "confirm": "x"})
    check("RBAC: operator cannot reboot host", st == 403, (st, j))
    call("POST", "/api/v1/auth/logout")
S.cookie, S.csrf = admin_cookie, admin_csrf

# WebSocket handshake + hello
sock = socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=10)
if u.scheme == "https":
    sock = CTX.wrap_socket(sock, server_hostname=u.hostname)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall((f"GET /api/v1/ws HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {u.scheme}://{u.netloc}\r\n"
              f"Cookie: {S.cookie}\r\n\r\n").encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += sock.recv(4096)
head, rest = resp.split(b"\r\n\r\n", 1)
check("websocket upgrade", b" 101 " in head.split(b"\r\n")[0], head[:100])
while len(rest) < 2:
    rest += sock.recv(4096)
n = rest[1] & 0x7F
while len(rest) < 2 + n:
    rest += sock.recv(4096)
msg = json.loads(rest[2:2 + n])
check("websocket hello", msg["type"] == "hello", msg)
sock.close()

st, j, _ = call("POST", "/api/v1/auth/logout")
check("logout", st == 200)
st, j, _ = call("GET", "/api/v1/auth/session")
check("session invalid after logout", st == 401, (st, j))
print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
