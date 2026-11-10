"""Database initialization and administrator setup through the command line."""
import secrets
import sqlite3
import warnings
from concurrent.futures import ThreadPoolExecutor
from getpass import GetPassWarning
from threading import Barrier

import pytest
from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

from election_system import create_app, db as database


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRET_KEY", secrets.token_urlsafe(32))
    monkeypatch.delenv("DATABASE", raising=False)

    import app

    def unexpected_server(*args, **kwargs):
        pytest.fail("A setup command started the HTTP server")

    monkeypatch.setattr(Flask, "run", unexpected_server)
    return app, tmp_path / "election_system.db"


def prompt_answers(monkeypatch, inputs, passwords):
    answers = iter(inputs)
    hidden_answers = iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("app.getpass", lambda prompt: next(hidden_answers))


def test_local_server_initializes_without_demo_data(cli, monkeypatch):
    command, path = cli

    def capture_server(app, **options):
        assert options == {"host": "127.0.0.1", "port": 5000, "debug": False}
        assert app.config["SESSION_COOKIE_SECURE"] is False

    monkeypatch.setattr(Flask, "run", capture_server)
    command.main([])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM elections").fetchone()[0] == 0


@pytest.mark.parametrize("database_name", [None, "", "configured.db"])
def test_cli_initializes_database_without_prompting_and_preserves_records(cli, monkeypatch, database_name):
    command, default_path = cli
    path = default_path.with_name(database_name) if database_name else default_path
    if database_name is not None:
        monkeypatch.setenv("DATABASE", str(path) if database_name else "")
    prompt_answers(monkeypatch, [], [])

    command.main(["--init-db"])
    assert path.is_file()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('existing', 'test-hash', 'VOTER')")

    command.main(["--init-db"])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT username FROM users").fetchall() == [("existing",)]
    if database_name:
        assert not default_path.exists()


def test_cli_rejects_combining_setup_modes(cli):
    command, path = cli
    with pytest.raises(SystemExit) as error:
        command.main(["--init-db", "--admin"])
    assert error.value.code == 2
    assert not path.exists()


def test_cli_creates_first_admin_and_lists_existing_admin_by_default(cli, monkeypatch, capsys):
    command, path = cli
    password = "admin123"
    prompt_answers(monkeypatch, ["admin"], [password, password])
    command.main(["--admin"])
    created = capsys.readouterr().out
    assert "Administrator account 'admin' created." in created

    with sqlite3.connect(path) as db:
        users = db.execute("SELECT id, username, role, password_hash FROM users").fetchall()
    assert len(users) == 1
    _, username, role, password_hash = users[0]
    assert (username, role) == ("admin", "ADMIN")
    assert check_password_hash(password_hash, password)

    prompt_answers(monkeypatch, [""], [])
    command.main(["--admin"])
    output = capsys.readouterr().out
    assert "Existing administrators:" in output and "'admin'" in output
    assert password not in created + output and password_hash not in created + output
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id, username, role, password_hash FROM users").fetchall() == users


@pytest.mark.parametrize(("new_username", "password"), [("", "ReplacedPass1"), ("admin", "admin123")])
def test_cli_updates_only_selected_admin_credentials(cli, monkeypatch, capsys, new_username, password):
    command, path = cli
    prompt_answers(monkeypatch, ["owner"], ["LocalPass1", "LocalPass1"])
    command.main(["--admin"])
    other_hash = generate_password_hash("OtherPass1")
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT id, password_hash FROM users WHERE username = 'owner'").fetchone()
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('other', ?, 'ADMIN')", (other_hash,))
        db.execute("INSERT INTO audit_logs (admin_id, action, timestamp) VALUES (?, 'Existing history', '')", (original[0],))

    prompt_answers(monkeypatch, ["yes", "owner", new_username], [password, password])
    command.main(["--admin"])
    with sqlite3.connect(path) as db:
        updated = db.execute("SELECT username, role, password_hash FROM users WHERE id = ?", (original[0],)).fetchone()
        assert db.execute("SELECT password_hash FROM users WHERE username = 'other'").fetchone()[0] == other_hash
        assert db.execute("SELECT admin_id, action FROM audit_logs").fetchall() == [(original[0], "Existing history")]
    assert updated[:2] == (new_username or "owner", "ADMIN")
    assert updated[2] != original[1] and check_password_hash(updated[2], password)
    assert password not in capsys.readouterr().out


def test_cli_rejects_renaming_an_admin_to_an_existing_username(cli, monkeypatch, capsys):
    command, path = cli
    prompt_answers(monkeypatch, ["owner"], ["LocalPass1", "LocalPass1"])
    command.main(["--admin"])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('existing', 'voter-hash', 'VOTER')")
        before = db.execute("SELECT * FROM users ORDER BY id").fetchall()

    prompt_answers(monkeypatch, ["yes", "owner", "existing"], ["ReplacedPass1", "ReplacedPass1"])
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    assert "username is already registered" in capsys.readouterr().err
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM users ORDER BY id").fetchall() == before


@pytest.mark.parametrize("change", ["username = 'newer-owner'", "role = 'VOTER'"])
def test_cli_rejects_admin_changed_during_credential_prompts(cli, monkeypatch, capsys, change):
    command, path = cli
    prompt_answers(monkeypatch, ["owner"], ["LocalPass1", "LocalPass1"])
    command.main(["--admin"])
    with sqlite3.connect(path) as db:
        password_hash = db.execute("SELECT password_hash FROM users").fetchone()[0]

    prompt_answers(monkeypatch, ["yes", "owner", "another-owner"], [])

    def change_account_then_get_password(prompt):
        with sqlite3.connect(path) as db:
            db.execute(f"UPDATE users SET {change} WHERE id = 1")
        return "ReplacedPass1"

    monkeypatch.setattr(command, "getpass", change_account_then_get_password)
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    assert "administrator account changed" in capsys.readouterr().err
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT password_hash FROM users").fetchone()[0] == password_hash


@pytest.mark.parametrize(("password", "confirmation", "error"), [
    ("Abc123", "Abc123", "at least 8 characters"),
    ("αβγδεζη1", "αβγδεζη1", "ASCII letter and digit"),
    ("Abcdefg１", "Abcdefg１", "ASCII letter and digit"),
    ("LocalPass1", "DifferentPass1", "do not match"),
])
def test_cli_rejects_bad_password_before_creating_account(cli, monkeypatch, capsys, password, confirmation, error):
    command, path = cli
    prompt_answers(monkeypatch, ["admin"], [password, confirmation])
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    assert error in capsys.readouterr().err
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_cli_fails_safely_without_hidden_input(cli, monkeypatch, capsys):
    command, path = cli
    monkeypatch.setattr("builtins.input", lambda prompt: "owner")

    def warning_before_echo(prompt):
        warnings.warn("Cannot disable echo", GetPassWarning)
        pytest.fail("Password read continued after the terminal rejected hidden input")

    monkeypatch.setattr(command, "getpass", warning_before_echo)
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    assert "terminal that can hide input" in capsys.readouterr().err
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_cli_rejects_a_voters_username_when_creating_first_admin(cli, monkeypatch, capsys):
    command, path = cli
    prompt_answers(monkeypatch, [""], [])
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('existing', 'legacy-hash', 'VOTER')")

    prompt_answers(monkeypatch, ["existing"], ["LocalPass1", "LocalPass1"])
    with pytest.raises(SystemExit, match="1"):
        command.main(["--admin"])
    assert "username is already registered" in capsys.readouterr().err
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT username, role, password_hash FROM users").fetchall() == [
            ("existing", "VOTER", "legacy-hash"),
        ]


def test_concurrent_first_admin_setup_creates_one_account(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    with app.app_context():
        database.init_db()
    ready = Barrier(2)

    def create(username):
        with app.app_context():
            ready.wait(timeout=5)
            try:
                database.create_first_administrator(username, "unused-test-hash")
            except ValueError as error:
                assert "administrator already exists" in str(error)
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(create, ("one", "two"))) == [False, True]
    with app.app_context():
        assert len(database.list_administrators()) == 1
