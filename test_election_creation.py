"""Integrated draft creation, explicit empty-roster consent and atomic writes."""
import sqlite3
from html.parser import HTMLParser

import pytest
from werkzeug.datastructures import MultiDict

from election_system import db as database
from election_system.db import get_db
from test_app import ADMIN_PASSWORD, VOTER_PASSWORD, client, election_management_snapshot, login


CREATE_URL = "/admin/election/create"


def creation_data(**changes):
    fields = {
        "title": "  New student election  ", "description": "  Choose representatives  ",
        "start_time": "2100-01-01T09:00", "end_time": "2100-01-02T18:00",
    }
    fields.update(changes)
    return fields


class CreationFields(HTMLParser):
    """Read the actual form, excluding the inert candidate row template."""
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.textarea = None
        self.fields = MultiDict()
        self.feed(page.decode())

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.in_form = attrs.get("id") == "election-creation-form"
        if not self.in_form:
            return
        if tag == "input" and "name" in attrs:
            self.fields.add(attrs["name"], attrs.get("value", ""))
        if tag == "textarea":
            self.textarea = [attrs["name"], ""]

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False
        if tag == "textarea" and self.textarea is not None:
            self.fields.add(*self.textarea)
            self.textarea = None


def assert_preserved(page, original):
    rendered = CreationFields(page).fields
    for name in ("title", "description", "start_time", "end_time", "candidate_name", "candidate_manifesto"):
        if name in original:
            expected = original[name]
            assert rendered.getlist(name) == (expected if isinstance(expected, list) else [expected])
    # Consent is a submit button, never a persistent input carried into a retry.
    assert "confirm_without_candidates" not in rendered


@pytest.mark.parametrize("username", [None, "voter1"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_creation_requires_admin(client, username, method):
    if username:
        login(client, username, VOTER_PASSWORD)
    before = election_management_snapshot(client)
    response = getattr(client, method)(CREATE_URL, data=creation_data(confirm_without_candidates="yes"))
    assert response.status_code == 302
    assert response.headers["Location"] == ("/" if username else "/login")
    assert election_management_snapshot(client) == before
    assert b"Create New Election" not in client.get("/", follow_redirects=True).data


def test_dashboard_links_to_dedicated_form(client):
    login(client, "admin", ADMIN_PASSWORD)
    dashboard = client.get("/").data
    assert b'href="/admin/election/create"' in dashboard
    assert b'action="/admin/election/create"' not in dashboard
    response = client.get(CREATE_URL)
    assert response.status_code == 200
    fields = CreationFields(response.data).fields
    assert fields.getlist("candidate_name") == [""]
    assert fields.getlist("candidate_manifesto") == [""]
    assert b"Election Details" in response.data
    assert b"Add Candidate" in response.data


@pytest.mark.parametrize("count", [1, 2, 4])
def test_creates_whole_roster_as_private_draft_and_audits(client, count):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(
        candidate_name=["  Same candidate  "] * count,
        candidate_manifesto=[f"  Manifesto {index}  " for index in range(count)],
    )
    response = client.post(CREATE_URL, data=fields)
    assert response.status_code == 302
    assert response.headers["Location"] == "/election/2"
    with client.application.app_context():
        election = database.find_election(2)
        assert election["title"] == "New student election"
        assert election["description"] == "Choose representatives"
        assert election["is_confirmed"] == 0
        candidates = database.list_candidates(2)
        assert [(item["name"], item["manifesto"]) for item in candidates] == [
            ("Same candidate", f"Manifesto {index}") for index in range(count)
        ]
        logs = get_db().execute("SELECT * FROM audit_logs ORDER BY id").fetchall()
        assert len(logs) == count + 1
        assert all(log["admin_id"] == 1 for log in logs)
        assert "Created election" in logs[0]["action"]
        for index, log in enumerate(logs[1:]):
            assert "Election ID 2" in log["action"]
            assert f"Manifesto {index}" in log["action"]
    login(client, "voter1", VOTER_PASSWORD)
    assert b"New student election" not in client.get("/").data
    assert client.get("/election/2").headers["Location"] == "/"


@pytest.mark.parametrize("acknowledgement", [None, "", "no", "true"])
@pytest.mark.parametrize("blank_rows", [False, True])
def test_empty_roster_requires_explicit_confirmation_without_writing(client, acknowledgement, blank_rows):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data()
    if acknowledgement is not None:
        fields["confirm_without_candidates"] = acknowledgement
    if blank_rows:
        fields.update(candidate_name=["", "  \t"], candidate_manifesto=["  ", ""])
    before = election_management_snapshot(client)
    response = client.post(CREATE_URL, data=fields)
    assert response.status_code == 200
    assert b'id="empty-roster-confirmation"' in response.data
    assert b'aria-labelledby="empty-roster-title" hidden' not in response.data
    assert b"This election has no candidates." in response.data
    assert_preserved(response.data, fields)
    assert election_management_snapshot(client) == before


def test_explicit_empty_roster_confirmation_creates_draft(client):
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post(CREATE_URL, data=creation_data(
        candidate_name=["  "], candidate_manifesto=["\t"], confirm_without_candidates="yes",
    ))
    assert response.headers["Location"] == "/election/2"
    with client.application.app_context():
        assert database.find_election(2)["is_confirmed"] == 0
        assert database.list_candidates(2) == []
        assert get_db().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1


def test_no_script_warning_cancel_and_retry_preserve_form(client):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(candidate_name=[""], candidate_manifesto=[""])
    before = election_management_snapshot(client)
    warning = client.post(CREATE_URL, data=fields)
    assert_preserved(warning.data, fields)
    cancelled = client.post(CREATE_URL, data={**fields, "cancel_without_candidates": "yes"})
    assert_preserved(cancelled.data, fields)
    assert b'aria-labelledby="empty-roster-title" hidden' in cancelled.data
    assert election_management_snapshot(client) == before
    # A fresh submission still needs consent; cancellation cannot acknowledge creation.
    retry = client.post(CREATE_URL, data=fields)
    assert b'aria-labelledby="empty-roster-title" hidden' not in retry.data
    assert election_management_snapshot(client) == before
    created = client.post(CREATE_URL, data={**fields, "confirm_without_candidates": "yes"})
    assert created.headers["Location"] == "/election/2"


@pytest.mark.parametrize("names,manifestos", [
    (["Alice"], [""]), ([""], ["My manifesto"]), (["   "], ["Manifesto"]),
    (["123"], ["Manifesto"]), (["１２３"], ["Manifesto"]),
    (["Alice", "Bob"], ["Manifesto"]), (["Alice"], ["First", "Second"]),
])
def test_invalid_candidates_do_not_write_and_keep_every_input(client, names, manifestos):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(candidate_name=names, candidate_manifesto=manifestos, confirm_without_candidates="yes")
    before = election_management_snapshot(client)
    response = client.post(CREATE_URL, data=fields)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before
    # Unmatched fields are retained as incomplete rows rather than silently truncated.
    expected = {**fields}
    count = max(len(names), len(manifestos))
    expected["candidate_name"] = names + [""] * (count - len(names))
    expected["candidate_manifesto"] = manifestos + [""] * (count - len(manifestos))
    assert_preserved(response.data, expected)
    if len(names) == len(manifestos):
        assert b'aria-invalid="true"' in response.data


def test_blank_rows_between_candidates_are_ignored(client):
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post(CREATE_URL, data=creation_data(
        candidate_name=["", "Alice", "  ", "Bob"], candidate_manifesto=["", "First", "\t", "Second"],
    ))
    assert response.status_code == 302
    with client.application.app_context():
        assert [candidate["name"] for candidate in database.list_candidates(2)] == ["Alice", "Bob"]


@pytest.mark.parametrize("invalid", [
    {"title": "   "}, {"start_time": "bad date"}, {"end_time": "2100-01-01T08:00"},
])
def test_invalid_election_is_reported_before_empty_roster_warning(client, invalid):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(**invalid)
    before = election_management_snapshot(client)
    response = client.post(CREATE_URL, data=fields)
    assert b"alert-danger" in response.data
    assert b'aria-labelledby="empty-roster-title" hidden' in response.data
    assert_preserved(response.data, fields)
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("failure", ["second_candidate", "candidate_audit"])
def test_late_write_failure_rolls_back_entire_creation_and_allows_retry(client, monkeypatch, failure):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(candidate_name=["Alice", "Bob"], candidate_manifesto=["First", "Second"])
    before = election_management_snapshot(client)
    original_add = database.add_candidate
    original_audit = database.log_audit

    def add(election_id, name, manifesto):
        if failure == "second_candidate" and name == "Bob":
            raise sqlite3.OperationalError("Private insertion error")
        return original_add(election_id, name, manifesto)

    def audit(admin_id, action):
        if failure == "candidate_audit" and action.startswith("Added candidate 'Bob'"):
            raise sqlite3.OperationalError("Private audit error")
        return original_audit(admin_id, action)

    with monkeypatch.context() as patch:
        patch.setattr(database, "add_candidate", add)
        patch.setattr(database, "log_audit", audit)
        response = client.post(CREATE_URL, data=fields)
    assert b"Changes were rolled back" in response.data
    assert b"Private insertion error" not in response.data
    assert b"Private audit error" not in response.data
    assert_preserved(response.data, fields)
    assert election_management_snapshot(client) == before
    assert client.post(CREATE_URL, data=fields).headers["Location"] == "/election/2"


def test_redisplayed_and_saved_values_are_escaped(client):
    login(client, "admin", ADMIN_PASSWORD)
    fields = creation_data(
        title='"><script>alert(1)</script>', description="</textarea><script>alert(2)</script>",
        candidate_name=['"><script>alert(3)</script>'], candidate_manifesto=[""],
    )
    response = client.post(CREATE_URL, data=fields)
    assert_preserved(response.data, fields)
    assert b"<script>alert(" not in response.data
    fields["candidate_manifesto"] = ["</textarea><script>alert(4)</script>"]
    created = client.post(CREATE_URL, data=fields, follow_redirects=True)
    assert created.request.path == "/election/2"
    assert b"<script>alert(" not in created.data
    assert b"&lt;script&gt;alert(4)&lt;/script&gt;" in created.data
