"""lsxve-admin: local administration CLI (run as root on the host console).

    lsxve-admin list-users
    lsxve-admin reset-password <username>      # prompts, forces change at next login
    lsxve-admin create-admin <username>        # prompts for a password
    lsxve-admin unlock <username>              # re-enable a disabled account

When started as root it drops privileges to the lsxve service account before
touching the database, so file ownership stays correct.
"""
from __future__ import annotations

import getpass
import os
import pwd
import sys
import time

from ..common import config, db as dbm, security, validate


def _drop_privileges() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            pw = pwd.getpwnam("lsxve")
        except KeyError:
            return
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)


def _ask_password(stdin_ok: bool) -> str:
    if stdin_ok and not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != pw:
            raise SystemExit("Passwords do not match")
    validate.password(pw)
    return pw


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    pw = None
    if cmd in ("reset-password", "create-admin"):
        try:
            pw = _ask_password("--stdin" in argv)
        except validate.ValidationError as e:
            print(f"error: {e.message}", file=sys.stderr)
            return 1
    _drop_privileges()
    db = dbm.Database(config.DB_FILE)
    if cmd == "list-users":
        for u in db.all("SELECT username, role, enabled, last_login FROM users ORDER BY username"):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["last_login"])) if u["last_login"] else "never"
            print(f"{u['username']:<24} {u['role']:<16} {'enabled' if u['enabled'] else 'DISABLED':<9} last login {last}")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    user = argv[1]
    if cmd == "reset-password":
        h = security.hash_password(pw)
        cur = db.execute("UPDATE users SET password_hash=?, must_change_password=1, enabled=1, password_changed=? "
                         "WHERE username=?", (h, time.time(), user))
        if not cur.rowcount:
            print(f"error: no such user {user}", file=sys.stderr)
            return 1
        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (user,))
        db.audit("console", "local", "security.user.password_reset", user, "success")
        print(f"Password for {user} reset. It must be changed at the next login.")
        return 0
    if cmd == "create-admin":
        validate.username(user)
        if db.one("SELECT id FROM users WHERE username=?", (user,)):
            print("error: user exists", file=sys.stderr)
            return 1
        dbm.create_user(db, user, security.hash_password(pw), "Administrator", "Administrator")
        db.audit("console", "local", "security.user.create", user, "success")
        print(f"Administrator {user} created.")
        return 0
    if cmd == "unlock":
        db.execute("UPDATE users SET enabled=1 WHERE username=?", (user,))
        db.audit("console", "local", "security.user.unlock", user, "success")
        print(f"{user} enabled.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
