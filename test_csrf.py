"""Exercise the rendered CSRF forms and bypass the test client's automatic token."""
from html.parser import HTMLParser

import pytest
from itsdangerous import TimestampSigner

from election_system.db import get_db
from test_app import ADMIN_PASSWORD, VOTER_PASSWORD, client, election_management_snapshot, login


class PostForms(HTMLParser):
    def __init__(self, response):
        super().__init__()
        assert response.status_code == 200
        assert response.cache_control.no_store
        self.path = response.request.path
        self.forms = {}
        self.fields = None
        self.feed(response.text)
        assert self.forms
        assert all(fields.get("csrf_token") for fields in self.forms.values())

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form" and attrs.get("method", "get").lower() == "post":
            assert self.fields is None
            self.fields = {}
            self.forms[attrs.get("action", self.path)] = self.fields
        if tag == "input" and self.fields is not None and attrs.get("type") == "hidden":
            assert attrs["name"] not in self.fields
            self.fields[attrs["name"]] = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.fields = None


def submit(client, page, path, **data):
    fields = PostForms(page).forms[path]
    return client.open(
        path, method="POST", base_url=page.request.url_root,
        data={**fields, **data}, headers={"Referer": page.request.url},
    )


def test_rendered_post_forms_include_csrf_tokens(client):
    assert PostForms(client.get("/login")).forms.keys() == {"/login"}
    assert PostForms(client.get("/register")).forms.keys() == {"/register"}

    login(client, "voter1", VOTER_PASSWORD)
    assert PostForms(client.get("/election/1")).forms.keys() == {
        "/logout", "/election/1/request", "/election/1/vote",
    }
    assert client.post("/election/1/request").status_code == 302

    login(client, "admin", ADMIN_PASSWORD)
    assert PostForms(client.get("/admin/election/create")).forms.keys() == {
        "/logout", "/admin/election/create",
    }
    assert PostForms(client.get("/election/1")).forms.keys() == {
        "/logout", "/admin/election/1/update", "/admin/election/1/close",
        "/admin/election/1/voter/2/status",
    }
    with client.application.app_context(), get_db() as db:
        db.execute("UPDATE elections SET is_confirmed = 0 WHERE id = 1")
    assert PostForms(client.get("/election/1")).forms.keys() == {
        "/logout", "/admin/election/1/update", "/admin/election/1/delete",
        "/admin/election/1/confirm", "/admin/election/1/candidate/add",
        "/admin/election/1/candidate/1/update", "/admin/election/1/candidate/1/delete",
        "/admin/election/1/candidate/2/update", "/admin/election/1/candidate/2/delete",
        "/admin/election/1/voter/2/status",
    }


@pytest.mark.parametrize("failure", ["missing", "tampered", "other_session", "expired", "cross_origin"])
def test_rejected_participation_preserves_database_and_login(client, monkeypatch, failure):
    base_url = "https://localhost"
    path = "/election/1/request"
    login_page = client.get("/login", base_url=base_url)
    assert submit(client, login_page, "/login", username="voter1", password=VOTER_PASSWORD).status_code == 302
    page = client.get("/election/1", base_url=base_url)
    token = PostForms(page).forms[path]["csrf_token"]
    data = {"csrf_token": token}
    headers = {"Referer": page.request.url}

    if failure in {"missing", "cross_origin"}:
        headers = {"Origin": "https://attacker.example", "Referer": "https://attacker.example/"}
    if failure == "missing":
        data.clear()
    elif failure == "tampered":
        data["csrf_token"] = "!" + token
    elif failure == "other_session":
        other = client.application.test_client()
        other_page = other.get("/login", base_url=base_url)
        data["csrf_token"] = PostForms(other_page).forms["/login"]["csrf_token"]
    elif failure == "expired":
        get_timestamp = TimestampSigner.get_timestamp
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda signer: get_timestamp(signer) + 3601)

    before = election_management_snapshot(client)
    with client.session_transaction(base_url=base_url) as session:
        before_session = dict(session)
    response = client.open(path, method="POST", base_url=base_url, data=data, headers=headers)
    assert client.application.config["SESSION_COOKIE_NAME"] in response.request.cookies
    assert response.status_code == 400
    assert response.cache_control.no_store
    assert b"refresh it, and submit the form again" in response.data
    assert election_management_snapshot(client) == before
    with client.session_transaction(base_url=base_url) as session:
        assert dict(session) == before_session
        assert session["username"] == "voter1"


@pytest.mark.parametrize("path,data", [
    ("/login", {"username": "voter1", "password": VOTER_PASSWORD}),
    ("/register", {"username": "new_voter", "password": "PrivateTest8"}),
])
def test_anonymous_forms_require_csrf(client, path, data):
    client.get(path)
    before = election_management_snapshot(client)
    assert client.open(path, method="POST", data=data).status_code == 400
    assert election_management_snapshot(client) == before
    assert client.get("/").headers["Location"] == "/login"


@pytest.mark.parametrize("base_url", ["http://localhost", "https://localhost"])
def test_browser_forms_work_with_csrf(client, base_url, monkeypatch):
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", base_url.startswith("https://"))

    def get(path):
        return client.get(path, base_url=base_url)

    def sign_in(username, password):
        response = submit(client, get("/login"), "/login", username=username, password=password)
        assert response.headers["Location"] == "/"

    registration = submit(client, get("/register"), "/register", username="new_voter", password="PrivateTest8")
    assert registration.headers["Location"] == "/login"

    sign_in("voter1", VOTER_PASSWORD)
    detail = get("/election/1")
    assert submit(client, detail, "/election/1/request").headers["Location"] == "/election/1"

    sign_in("admin", ADMIN_PASSWORD)
    detail = get("/election/1")
    decision = submit(client, detail, "/admin/election/1/voter/2/status", status="APPROVED")
    assert decision.headers["Location"] == "/election/1"

    sign_in("voter1", VOTER_PASSWORD)
    detail = get("/election/1")
    assert submit(client, detail, "/election/1/vote", candidate_id=1).headers["Location"] == "/election/1"
    assert b"Vote successfully recorded!" in get("/election/1").data

    assert get("/logout").status_code == 405
    assert client.open("/logout", method="POST", base_url=base_url).status_code == 400
    assert get("/").status_code == 200
    assert submit(client, detail, "/logout").headers["Location"] == "/login"
    assert get("/").headers["Location"] == "/login"


def test_login_and_logout_invalidate_previous_form_tokens(client):
    login_page = client.get("/login")
    assert submit(client, login_page, "/login", username="voter1", password=VOTER_PASSWORD).status_code == 302
    dashboard = client.get("/")
    assert submit(client, login_page, "/login", username="admin", password=ADMIN_PASSWORD).status_code == 400
    assert submit(client, dashboard, "/logout").status_code == 302

    login_page = client.get("/login")
    assert submit(client, login_page, "/login", username="voter1", password=VOTER_PASSWORD).status_code == 302
    assert client.get("/").status_code == 200
    assert submit(client, dashboard, "/logout").status_code == 400
    assert client.get("/").status_code == 200
