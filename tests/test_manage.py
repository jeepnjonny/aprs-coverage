"""The command-line admin tool (python -m app.manage)."""
import pytest

from app import auth, db, manage


@pytest.fixture()
def cli(client, monkeypatch, capsys):  # client: a fresh database
    answers = []
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt="": answers.pop(0))

    def run(*argv, passwords=()):
        answers[:] = list(passwords)
        code = manage.main(["manage", *argv])
        return code, capsys.readouterr().out
    return run


def test_set_password_creates_admin_and_checks_rules(cli):
    assert cli("set-password", "boss", passwords=["short", "short"])[0] == 1
    assert cli("set-password", "boss", passwords=["long enough pw", "different pw!"])[0] == 1
    code, out = cli("set-password", "boss", passwords=["long enough pw"] * 2)
    assert code == 0 and "Created admin account boss" in out
    assert auth.authenticate("boss", "long enough pw")["role"] == "admin"
    assert cli("set-password", "bad name!", passwords=["long enough pw"] * 2)[0] == 1


def test_set_role_keeps_an_admin(cli):
    code, out = cli("set-role", "admin", "node_owner")  # the fixture's only admin
    assert code == 1 and "only active admin" in out
    cli("add-user", "second", "admin", passwords=["another password"] * 2)
    assert cli("set-role", "admin", "node_owner")[0] == 0
    with db.session() as c:
        assert c.execute("SELECT role FROM users WHERE username='admin'").fetchone()[0] == "node_owner"


def test_add_user_list_enable_logout(cli):
    assert cli("add-user", "owner1", "node_owner", "k7abc", passwords=["owner password"] * 2)[0] == 0
    assert cli("add-user", "owner1", passwords=["owner password"] * 2)[0] == 1  # exists
    assert cli("add-user", "x", "admin")[0] == 1  # bad username
    out = cli("list-users")[1]
    assert "owner1" in out and "K7ABC" in out
    assert cli("enable", "owner1")[0] == 0 and cli("enable", "nobody")[0] == 1
    assert cli("logout-all")[0] == 0
    assert cli("nonsense")[0] == 1
