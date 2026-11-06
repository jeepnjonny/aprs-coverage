"""Admin CLI.

    python -m app.manage set-password      # set or reset the admin password
    python -m app.manage logout-all        # invalidate every admin session
"""
import getpass
import sys

from . import auth, db


def main(argv: list[str]) -> int:
    db.init()
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "set-password":
        pw = getpass.getpass("New admin password: ")
        if len(pw) < 10:
            print("Password must be at least 10 characters.")
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match.")
            return 1
        db.set_setting("admin_password", auth.hash_password(pw))
        with db.session() as c:
            c.execute("DELETE FROM sessions")
        print("Admin password set.")
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
