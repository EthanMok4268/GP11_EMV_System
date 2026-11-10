import re
from datetime import datetime, timedelta

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers, MultiDict
from werkzeug.security import generate_password_hash

from election_system import create_app, db as database
from election_system.db import get_db, init_db

from .helpers import ADMIN_PASSWORD, VOTER_PASSWORD


class FormClient(FlaskClient):
    """Supply the browser's CSRF token for existing form tests."""

    def post(self, path, *, data=None, headers=None, **kwargs):
        page = self.get("/login", base_url=kwargs.get("base_url"))
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        assert token is not None, "Login page is missing its CSRF token"

        form = MultiDict(data)
        form.setdefault("csrf_token", token[1].decode())
        headers = Headers(headers)
        headers.setdefault("Referer", page.request.url)
        return super().post(path, data=form, headers=headers, **kwargs)


@pytest.fixture
def form_client_class():
    return FormClient


@pytest.fixture
def client(tmp_path, form_client_class):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    app.test_client_class = form_client_class
    with app.app_context():
        init_db()
        now = datetime.now()
        with get_db() as db:
            db.executemany(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                [
                    ("admin", generate_password_hash(ADMIN_PASSWORD), "ADMIN"),
                    ("voter1", generate_password_hash(VOTER_PASSWORD), "VOTER"),
                ],
            )
            election_id = db.execute(
                "INSERT INTO elections (title, description, start_time, end_time, is_confirmed) "
                "VALUES (?, ?, ?, ?, 1)",
                (
                    "Student Union Presidential Election", "Annual vote for the Student Union President.",
                    (now - timedelta(hours=1)).isoformat(timespec="minutes"),
                    (now + timedelta(days=1)).isoformat(timespec="minutes"),
                ),
            ).lastrowid
            database.add_candidate(election_id, "Alice Wong", "24/7 library access and lower campus cafe prices.")
            database.add_candidate(election_id, "Bob Chan", "More funding for sports clubs and student societies.")
    return app.test_client()
