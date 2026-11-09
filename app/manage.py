"""Admin CLI, for setting up the first admin and getting back in if locked out.

    python -m app.manage set-password [USER]       # set a password; creates USER as an admin if new (default: admin)
    python -m app.manage add-user USER [admin|node_owner] [CALLSIGN]
    python -m app.manage set-role USER admin|node_owner
    python -m app.manage enable USER               # make an account active (approve or re-enable it)
    python -m app.manage list-users
    python -m app.manage logout-all                # end every session
"""
import getpass
import sys

from . import auth, db


def _ask_password() -> str | None:
    pw = getpass.getpass("New password: ")
    if len(pw) < auth.MIN_PASSWORD:
        print(f"Password must be at least {auth.MIN_PASSWORD} characters.")
        return None
    if pw != getpass.getpass("Repeat: "):
        print("Passwords do not match.")
        return None
    return pw


def _get(c, username: str):
    return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def main(argv: list[str]) -> int:
    db.init()
    cmd, args = (argv[1] if len(argv) > 1 else ""), argv[2:]

    if cmd == "set-password":
        username = args[0] if args else "admin"
        pw = _ask_password()
        if pw is None:
            return 1
        with db.session() as c:
            u = _get(c, username)
            if u is None:
                auth.create_user(c, username, pw, role="admin", status="active")
                print(f"Created admin account {username}.")
            else:
                c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(pw), u["id"]))
                auth.end_user_sessions(c, u["id"])
                print(f"Password set for {username}.")
        return 0

    if cmd == "add-user" and args:
        username, role = args[0], (args[1] if len(args) > 1 else "node_owner")
        call = (args[2] if len(args) > 2 else "").upper()
        if role not in auth.ROLES or not auth.USERNAME_RE.match(username):
            print(__doc__)
            return 1
        if call and not auth.BASE_CALL_RE.match(call):
            print("Callsign must be a base callsign without an SSID, e.g. K7ABC.")
            return 1
        pw = _ask_password()
        if pw is None:
            return 1
        with db.session() as c:
            if _get(c, username):
                print(f"{username} already exists.")
                return 1
            auth.create_user(c, username, pw, role=role, status="active", callsign=call)
        print(f"Created {role} {username}.")
        return 0

    if cmd == "set-role" and len(args) == 2 and args[1] in auth.ROLES:
        with db.session() as c:
            if c.execute("UPDATE users SET role=? WHERE username=?", (args[1], args[0])).rowcount == 0:
                print(f"No user {args[0]}.")
                return 1
        print(f"{args[0]} is now {args[1]}.")
        return 0

    if cmd == "enable" and len(args) == 1:
        with db.session() as c:
            if c.execute("UPDATE users SET status='active' WHERE username=?", (args[0],)).rowcount == 0:
                print(f"No user {args[0]}.")
                return 1
        print(f"{args[0]} is active.")
        return 0

    if cmd == "list-users":
        with db.session() as c:
            rows = c.execute("SELECT username, role, status, callsign, can_config FROM users "
                             "ORDER BY username COLLATE NOCASE").fetchall()
        for r in rows:
            cfg = ", remote config" if r["can_config"] else ""
            print(f"{r['username']:<24} {r['role']:<11} {r['status']:<9} {r['callsign']}{cfg}")
        if not rows:
            print("No users. Create the first admin with: python -m app.manage set-password")
        return 0

    if cmd == "logout-all":
        with db.session() as c:
            c.execute("DELETE FROM sessions")
        print("All sessions cleared.")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
