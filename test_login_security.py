import re
import secrets
from html.parser import HTMLParser

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

from election_system import create_app
from election_system.db import get_db, init_db


LEGACY_SECRET = "dev-secret-key-change-in-production"
ADMIN_PASSWORD = "Private-test-password-2026"


@pytest.fixture
def secure_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    app = create_app({"DATABASE": str(tmp_path / "secure.db")})
    with app.app_context():
        init_db()
    return app


def create_user(app, username, password, role):
    with app.app_context(), get_db() as db:
        return db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        ).lastrowid


def login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, base_url="https://localhost"
    )


@pytest.mark.parametrize("secret", [None, "   ", "too-short", LEGACY_SECRET, f" {LEGACY_SECRET} "])
def test_normal_startup_requires_a_new_signing_key(monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SECRET_KEY", secret)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


@pytest.mark.parametrize(("username", "password"), [("admin", "admin123"), ("voter1", "voter123")])
def test_signed_cookie_uses_secure_attributes_and_admin_login_works(secure_app, username, password):
    create_user(secure_app, username, password, "ADMIN")
    client = secure_app.test_client()
    response = login(client, username, password)
    assert response.headers["Location"] == "/"
    cookie = response.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ("; Secure", "; HttpOnly", "; SameSite=Lax"))
    assert client.get("/admin/audit-logs", base_url="https://localhost").status_code == 200


def test_old_key_and_old_session_format_cannot_authenticate(secure_app):
    admin_id = create_user(secure_app, "admin", ADMIN_PASSWORD, "ADMIN")
    client = secure_app.test_client()
    assert login(client, "admin", ADMIN_PASSWORD).headers["Location"] == "/"
    with client.session_transaction(base_url="https://localhost") as session:
        authenticated_claims = dict(session)

    legacy_app = Flask(__name__)
    legacy_app.secret_key = LEGACY_SECRET
    signer = legacy_app.session_interface.get_signing_serializer(legacy_app)
    client.set_cookie(secure_app.config["SESSION_COOKIE_NAME"], signer.dumps(authenticated_claims))
    assert client.get("/admin/audit-logs", base_url="https://localhost").headers["Location"] == "/login"

    with client.session_transaction(base_url="https://localhost") as session:
        session.update(user_id=admin_id, username="admin", role="ADMIN")
    assert client.get("/admin/audit-logs", base_url="https://localhost").headers["Location"] == "/login"


def test_published_demo_voter_credentials_cannot_register_or_sign_in(secure_app):
    username, password = "voter1", "voter123"
    client = secure_app.test_client()
    login_page = client.get("/login", base_url="https://localhost").data
    assert b"admin123" not in login_page and b"voter123" not in login_page
    response = client.post(
        "/register", data={"username": username, "password": password}, base_url="https://localhost"
    )
    assert response.headers["Location"] == "/register"
    with secure_app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    create_user(secure_app, username, password, "VOTER")
    assert b"Invalid username or password" in login(client, username, password).data
    assert client.get("/", base_url="https://localhost").headers["Location"] == "/login"


def test_voter_registration_form_displays_and_enforces_password_requirements(secure_app):
    response = secure_app.test_client().get("/register", base_url="https://localhost")
    assert response.status_code == 200
    fields = {}

    class RegistrationFields(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag == "input" and "name" in attrs:
                fields[attrs["name"]] = attrs

    RegistrationFields().feed(response.text)
    username, password = fields["username"], fields["password"]
    assert username["type"] == "text" and "required" in username
    assert password["type"] == "password" and "required" in password
    assert f'<label for="{password["id"]}"'.encode() in response.data
    assert password["minlength"] == "8"
    assert re.fullmatch(password["pattern"], "abcd1234")
    assert not re.fullmatch(password["pattern"], "😀😀😀a1")
    assert not re.fullmatch(password["pattern"], "abcdefgh")
    assert not re.fullmatch(password["pattern"], "12345678")
    assert b"at least 8 characters, including an English letter and a digit" in response.data


@pytest.mark.parametrize("password", ["Abc1234", "abcdefgh", "12345678", "αβγδ1234", "abcdefg１"])
def test_voter_registration_rejects_invalid_passwords_in_direct_posts(secure_app, password):
    response = secure_app.test_client().post(
        "/register", data={"username": "new_voter", "password": password}, base_url="https://localhost"
    )
    assert response.headers["Location"] == "/register"
    with secure_app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_voter_registration_accepts_exactly_eight_characters_with_a_letter_and_digit(secure_app):
    client = secure_app.test_client()
    username, password = "new_voter", "abcd1234"
    response = client.post(
        "/register", data={"username": username, "password": password}, base_url="https://localhost"
    )
    assert response.headers["Location"] == "/login"
    with secure_app.app_context():
        assert get_db().execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()[0] == "VOTER"
    assert login(client, username, password).headers["Location"] == "/"


def test_voter_with_an_existing_short_password_can_still_sign_in(secure_app):
    username, password = "existing_voter", "legacy"
    create_user(secure_app, username, password, "VOTER")
    assert login(secure_app.test_client(), username, password).headers["Location"] == "/"


def test_protected_routes_use_latest_database_role_and_username(secure_app):
    admin_id = create_user(secure_app, "admin", ADMIN_PASSWORD, "ADMIN")
    client = secure_app.test_client()
    assert login(client, "admin", ADMIN_PASSWORD).headers["Location"] == "/"

    with secure_app.app_context(), get_db() as db:
        db.execute("UPDATE users SET username = 'renamed', role = 'VOTER' WHERE id = ?", (admin_id,))

    response = client.get("/admin/audit-logs", base_url="https://localhost", follow_redirects=True)
    assert b"Administrator privileges required" in response.data
    assert b"renamed" in response.data
    assert b'href="/admin/audit-logs"' not in response.data


@pytest.mark.parametrize("change", ["password", "delete"])
def test_existing_session_is_invalidated_when_account_changes(secure_app, change):
    admin_id = create_user(secure_app, "admin", ADMIN_PASSWORD, "ADMIN")
    client = secure_app.test_client()
    assert login(client, "admin", ADMIN_PASSWORD).headers["Location"] == "/"

    new_password = "Private-test-password-rotated"
    with secure_app.app_context(), get_db() as db:
        if change == "password":
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), admin_id),
            )
        else:
            db.execute("DELETE FROM users WHERE id = ?", (admin_id,))

    assert client.get("/admin/audit-logs", base_url="https://localhost").headers["Location"] == "/login"
    if change == "password":
        assert login(client, "admin", new_password).headers["Location"] == "/"
