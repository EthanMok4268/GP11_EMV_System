import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

import pytest
from werkzeug.security import generate_password_hash

from election_system import create_app, services
from election_system import db as database
from election_system.db import get_db, init_db

ADMIN_PASSWORD = "AdminFixture8"
VOTER_PASSWORD = "VoterFixture8"


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


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)

def test_rbac_blocks_voter_from_admin_routes(client):
    """TC-02: Verify voters cannot create elections."""
    login(client, "voter1", VOTER_PASSWORD)
    res = client.post("/admin/election/create", data={
        "title": "Unauthorized Election",
        "start_time": "2026-01-01T00:00",
        "end_time": "2026-12-31T00:00"
    }, follow_redirects=True)
    assert b"Access Denied: Administrator privileges required" in res.data

def test_voting_flow_and_duplicate_prevention(client):
    """TC-04, TC-05, TC-06: Verify eligibility check, anonymous ballot recording, and duplicate block."""
    # 1. Voter requests participation in Election 1
    login(client, "voter1", VOTER_PASSWORD)
    client.post("/election/1/request", follow_redirects=True)

    # 2. Attempt to vote while still PENDING (TC-04)
    res_pending = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
    assert b"You are not approved to vote" in res_pending.data

    # 3. Admin approves voter1
    login(client, "admin", ADMIN_PASSWORD)
    client.post("/admin/election/1/voter/2/status", data={"status": "APPROVED"}, follow_redirects=True)

    # 4. Approved voter casts valid vote (TC-05)
    login(client, "voter1", VOTER_PASSWORD)
    res_vote = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
    assert b"Vote successfully recorded!" in res_vote.data

    # 5. Attempt to cast a second vote in the same election (TC-06)
    res_dup = client.post("/election/1/vote", data={"candidate_id": 2}, follow_redirects=True)
    assert b"Duplicate voting prevented" in res_dup.data

    # Verify only 1 ballot exists in database
    with client.application.app_context():
        db = get_db()
        count = db.execute("SELECT COUNT(*) as c FROM ballots WHERE election_id = 1").fetchone()["c"]
        assert count == 1


def test_registration_login_and_logout(client):
    assert client.get("/").headers["Location"] == "/login"
    response = client.post("/register", data={"username": "new_voter", "password": "secret123"}, follow_redirects=True)
    assert b"Registration successful" in response.data
    assert b"Invalid username or password" in login(client, "new_voter", "wrong-password").data
    assert b"Welcome back, new_voter" in login(client, "new_voter", "secret123").data
    response = client.post("/register", data={"username": "new_voter", "password": "secret123"})
    assert b"Username already registered" in response.data
    client.post("/logout")
    assert client.get("/").headers["Location"] == "/login"


def test_admin_election_candidate_actions_and_template_escaping(client):
    from datetime import datetime, timedelta

    login(client, "admin", ADMIN_PASSWORD)
    start = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    end = (datetime.now() + timedelta(days=11)).strftime("%Y-%m-%dT%H:%M")
    response = client.post("/admin/election/create", data={
        "title": "<script>alert(1)</script>", "description": "Example", "start_time": start, "end_time": end,
        "confirm_without_candidates": "yes",
    }, follow_redirects=True)
    assert b"Election created successfully" in response.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
    assert b"<script>alert(1)</script>" not in response.data
    assert response.request.path == "/election/2"
    dashboard = client.get("/").data
    assert b'href="/admin/election/create"' in dashboard
    assert b'href="/election/2"' in dashboard
    assert b'action="/admin/election/create"' in client.get("/admin/election/create").data

    response = client.post("/admin/election/2/candidate/add", data={
        "name": "Candidate <One>", "manifesto": "Example",
    }, follow_redirects=True)
    assert b"Candidate &lt;One&gt;" in response.data
    assert b'action="/admin/election/2/candidate/add"' in response.data
    with client.application.app_context():
        candidate_id = get_db().execute("SELECT id FROM candidates WHERE election_id = 2").fetchone()["id"]
    assert f'formaction="/admin/election/2/candidate/{candidate_id}/delete"'.encode() in response.data
    response = client.post(f"/admin/election/2/candidate/{candidate_id}/delete", follow_redirects=True)
    assert b"Candidate removed" in response.data
    with client.application.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM candidates WHERE election_id = 2").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 3


def test_active_election_locks_candidate_management(client):
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/1/candidate/add", data={
        "name": "Late candidate", "manifesto": "Example",
    }, follow_redirects=True)
    assert b"Candidates can only be added before an election begins" in response.data
    response = client.post("/admin/election/1/candidate/1/delete", follow_redirects=True)
    assert b"Candidates cannot be removed after an election starts" in response.data
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM candidates WHERE election_id = 1").fetchone()[0] == 2


@pytest.mark.parametrize("seconds_after_cutoff", [0, 30])
def test_voting_closes_and_results_are_published_at_cutoff(client, monkeypatch, seconds_after_cutoff):
    cutoff = datetime(2026, 11, 9, 18)
    now = cutoff - timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls):
            return now

    monkeypatch.setattr(services, "datetime", Clock)
    client.post("/register", data={"username": "voter2", "password": VOTER_PASSWORD})
    with client.application.app_context():
        db = get_db()
        db.execute("UPDATE elections SET start_time = ?, end_time = ? WHERE id = 1", (
            "2026-11-09T17:00", cutoff.isoformat(timespec="minutes"),
        ))
        late_voter_id = database.find_user("voter2")["id"]
        db.executemany(
            "INSERT INTO voter_participation (election_id, voter_id, status) VALUES (1, ?, 'APPROVED')",
            [(2,), (late_voter_id,)],
        )
        db.commit()

    login(client, "voter1", VOTER_PASSWORD)
    response = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
    assert b"Vote successfully recorded!" in response.data
    assert b"Official Published Results" not in response.data

    login(client, "voter2", VOTER_PASSWORD)
    before_cutoff = client.get("/election/1").data
    assert b"ACTIVE" in before_cutoff
    assert b'name="candidate_id"' in before_cutoff
    assert b"Official Published Results" not in before_cutoff

    now = cutoff + timedelta(seconds=seconds_after_cutoff)
    assert b"CLOSED" in client.get("/").data
    after_cutoff = client.get("/election/1").data
    assert b"CLOSED" in after_cutoff
    assert b'name="candidate_id"' not in after_cutoff
    assert b"Official Published Results (Total Votes: 1)" in after_cutoff
    assert b"100.0%" in after_cutoff

    response = client.post("/election/1/vote", data={"candidate_id": 2}, follow_redirects=True)
    assert b"This election is not currently active" in response.data
    with client.application.app_context():
        ballots = get_db().execute("SELECT candidate_id FROM ballots WHERE election_id = 1").fetchall()
        assert [ballot["candidate_id"] for ballot in ballots] == [1]
        assert database.find_participation(1, late_voter_id)["has_voted"] == 0


def test_ballot_failure_rolls_back_voter_participation(client):
    with client.application.app_context():
        db = get_db()
        db.execute("INSERT INTO voter_participation (election_id, voter_id, status) VALUES (1, 2, 'APPROVED')")
        db.execute("""
            CREATE TRIGGER fail_ballot_insert BEFORE INSERT ON ballots
            BEGIN SELECT RAISE(ABORT, 'Simulated ballot write failure'); END
        """)
        db.commit()
    login(client, "voter1", VOTER_PASSWORD)
    response = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
    assert b"Transaction rolled back" in response.data
    with client.application.app_context():
        db = get_db()
        assert db.execute("SELECT has_voted FROM voter_participation WHERE election_id = 1 AND voter_id = 2").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 0


def test_reinitialization_preserves_existing_data(client):
    with client.application.app_context():
        db = get_db()
        db.execute("UPDATE elections SET title = 'Existing election' WHERE id = 1")
        db.execute("INSERT INTO voter_participation (election_id, voter_id, status, has_voted) VALUES (1, 2, 'APPROVED', 1)")
        db.execute("INSERT INTO ballots (ballot_id, election_id, candidate_id) VALUES ('existing-ballot', 1, 1)")
        db.commit()
        before = list(db.iterdump())
        init_db()
        assert list(db.iterdump()) == before


def test_admin_can_close_in_opening_minute_and_publish_results(client, monkeypatch):
    start = datetime(2100, 1, 1, 9)
    scheduled_end = (start + timedelta(hours=1)).isoformat(timespec="minutes")
    closed_at = start + timedelta(seconds=30)

    class Clock(datetime):
        @classmethod
        def now(cls):
            return closed_at

    monkeypatch.setattr(services, "datetime", Clock)
    with client.application.app_context():
        with get_db() as db:
            db.execute("UPDATE elections SET start_time = ?, end_time = ? WHERE id = 1", (
                start.isoformat(timespec="minutes"), scheduled_end,
            ))
    login(client, "voter1", VOTER_PASSWORD)
    response = client.get("/election/1")
    assert b'action="/election/1/request"' in response.data
    assert b"End Election Early" not in response.data
    client.post("/election/1/request")
    login(client, "admin", ADMIN_PASSWORD)
    response = client.get("/election/1")
    assert b'action="/admin/election/1/close"' in response.data
    assert b'action="/admin/election/1/voter/2/status"' in response.data
    client.post("/admin/election/1/voter/2/status", data={"status": "APPROVED"})
    login(client, "voter1", VOTER_PASSWORD)
    assert b'action="/election/1/vote"' in client.get("/election/1").data
    client.post("/election/1/vote", data={"candidate_id": 1})
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Election has been closed early" in response.data
    assert b"Official Published Results (Total Votes: 1)" in response.data
    assert b"End Election Early" not in response.data
    actual_end = closed_at.isoformat(timespec="seconds")
    dashboard = client.get("/").data
    for page in (response.data, dashboard):
        assert actual_end.encode() in page
        assert f"closed early, scheduled end: {scheduled_end}".encode() in page
    with client.application.app_context():
        election = database.find_election(1)
        assert election["start_time"] == start.isoformat(timespec="minutes")
        assert election["end_time"] == scheduled_end
        assert election["closed_early_at"] == actual_end
        log = get_db().execute("SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "Closed Election ID 1" in log
    before_retry = election_management_snapshot(client)
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Only ACTIVE elections can be closed early" in response.data
    assert election_management_snapshot(client) == before_retry
    login(client, "voter1", VOTER_PASSWORD)
    response = client.post("/election/1/vote", data={"candidate_id": 2}, follow_redirects=True)
    assert b"This election is not currently active" in response.data


def test_voter_cannot_close_election(client):
    login(client, "voter1", VOTER_PASSWORD)
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Access Denied: Administrator privileges required" in response.data
    assert b"Official Published Results" not in client.get("/election/1").data


@pytest.mark.parametrize("election_id,start_time,end_time,expected", [
    (1, "2100-01-01T00:00", "2100-01-02T00:00", b"Only ACTIVE elections can be closed early"),
    (1, "2000-01-01T00:00", "2000-01-02T00:00", b"Only ACTIVE elections can be closed early"),
    (999, None, None, b"Election not found"),
])
def test_close_election_rejects_inactive_or_missing_election(client, election_id, start_time, end_time, expected):
    login(client, "admin", ADMIN_PASSWORD)
    with client.application.app_context():
        db = get_db()
        if start_time is not None:
            db.execute("UPDATE elections SET start_time = ?, end_time = ? WHERE id = 1", (start_time, end_time))
            db.commit()
        before = list(db.iterdump())
    response = client.post(f"/admin/election/{election_id}/close", follow_redirects=True)
    assert expected in response.data
    with client.application.app_context():
        assert list(get_db().iterdump()) == before


@pytest.mark.parametrize("newer_action", ["close", "deadline"])
def test_waiting_early_closure_rechecks_state_after_acquiring_write_lock(client, monkeypatch, newer_action):
    from test_candidate_management import wait_for_locked_request

    with client.application.app_context():
        deadline = datetime.fromisoformat(database.find_election(1)["end_time"])

    class DeadlineClock(datetime):
        @classmethod
        def now(cls):
            return deadline

    expected = None

    def close_while_waiting(db):
        nonlocal expected
        if newer_action == "close":
            closed_at = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE elections SET closed_early_at = ? WHERE id = 1", (closed_at,))
            db.execute("INSERT INTO audit_logs (admin_id, action, timestamp) VALUES (?, ?, ?)", (
                1, "Closed Election ID 1 early", closed_at,
            ))
        else:
            monkeypatch.setattr(services, "datetime", DeadlineClock)
        expected = list(db.iterdump())

    response = wait_for_locked_request(
        client.application, monkeypatch, "/admin/election/1/close", {}, close_while_waiting,
    )
    assert b"Only ACTIVE elections can be closed early" in response.data
    assert election_management_snapshot(client) == expected


def election_management_snapshot(client):
    with client.application.app_context():
        return list(get_db().iterdump())


def election_update_fields(client, **changes):
    with client.application.app_context():
        row = get_db().execute("SELECT * FROM elections WHERE id = 1").fetchone()
        fields = {name: row[name] for name in ("title", "description", "start_time", "end_time")}
    fields.update(changes)
    return fields


def set_management_election_state(client, state):
    now = datetime.now()
    start, end = {
        "UPCOMING": (now + timedelta(days=10), now + timedelta(days=11)),
        "ACTIVE": (now - timedelta(days=1), now + timedelta(days=1)),
        "CLOSED": (now - timedelta(days=11), now - timedelta(days=10)),
    }[state]
    with client.application.app_context():
        db = get_db()
        db.execute("UPDATE elections SET start_time = ?, end_time = ? WHERE id = 1", (
            start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"),
        ))
        db.commit()


@pytest.mark.parametrize("state,editable", [("UPCOMING", True), ("ACTIVE", True), ("CLOSED", False)])
def test_election_edit_form_matches_lifecycle_and_prefills_values(client, state, editable):
    set_management_election_state(client, state)
    login(client, "admin", ADMIN_PASSWORD)
    fields = election_update_fields(client)
    page = client.get("/election/1").data
    form = re.search(rb'<form\b[^>]*action="/admin/election/1/update"[^>]*>(.*?)</form>', page, re.DOTALL)
    assert bool(form) is editable
    if form:
        inputs = {}
        for name in ("title", "start_time", "end_time"):
            tag = re.search(rb'<input\b(?=[^>]*name="' + name.encode() + rb'")[^>]*>', form[1])
            assert tag is not None
            assert f'value="{fields[name]}"'.encode() in tag[0]
            inputs[name] = tag[0]
        assert re.search(rb'<textarea\b[^>]*name="description"[^>]*>(.*?)</textarea>', form[1], re.DOTALL)[1] == fields["description"].encode()
        if state == "ACTIVE":
            assert b"readonly" in inputs["start_time"]
            assert b"readonly" not in inputs["end_time"]
            assert f'min="{fields["end_time"]}"'.encode() in inputs["end_time"]
        else:
            assert b"readonly" not in form[1]


def test_upcoming_update_normalizes_values_and_audits_old_and_new_fields(client):
    set_management_election_state(client, "UPCOMING")
    old = election_update_fields(client)
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/1/update", data={
        "title": "  Updated election  ", "description": "  Revised description  ",
        "start_time": "2100-1-2T3:4", "end_time": "2100-1-3T5:6",
    }, follow_redirects=True)
    assert b"Election updated successfully." in response.data
    expected = {
        "title": "Updated election", "description": "Revised description",
        "start_time": "2100-01-02T03:04", "end_time": "2100-01-03T05:06",
    }
    assert election_update_fields(client) == expected
    assert re.search(rb'<h3[^>]*>\s*Updated election\s*</h3>', response.data)
    assert re.search(rb'<p[^>]*>\s*Revised description\s*</p>', response.data)
    dashboard = client.get("/").data
    assert re.search(rb'<h5[^>]*>\s*Updated election\s*</h5>', dashboard)
    assert re.search(rb'<p[^>]*>\s*Revised description\s*</p>', dashboard)
    with client.application.app_context():
        logs = get_db().execute("SELECT * FROM audit_logs").fetchall()
        assert len(logs) == 1
        assert logs[0]["admin_id"] == 1
        for name, value in expected.items():
            assert name in logs[0]["action"]
            assert old[name] in logs[0]["action"]
            assert value in logs[0]["action"]


def test_create_normalizes_dates_and_accepts_empty_description(client):
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/create", data={
        "title": "  Future election  ", "description": "   ",
        "start_time": "2100-1-2T3:4", "end_time": "2100-1-3T5:6",
        "confirm_without_candidates": "yes",
    }, follow_redirects=True)
    assert b"Election created successfully." in response.data
    with client.application.app_context():
        row = get_db().execute("SELECT * FROM elections WHERE id = 2").fetchone()
        assert (row["title"], row["description"], row["start_time"], row["end_time"]) == (
            "Future election", "", "2100-01-02T03:04", "2100-01-03T05:06",
        )


@pytest.mark.parametrize("route", ["create", "1/update"])
@pytest.mark.parametrize("invalid", [
    {"title": "   "},
    {"start_time": ""},
    {"start_time": "2100-02-29T12:00", "end_time": "2100-03-01T12:00"},
    {"start_time": "2100-01-02T12:00:00"},
    {"start_time": "2100-01-02T12:00+08:00"},
    {"end_time": "2100-01-02T12:00"},
    {"end_time": "2100-01-01T12:00"},
])
def test_create_and_update_reject_invalid_fields_without_writing(client, route, invalid):
    set_management_election_state(client, "UPCOMING")
    login(client, "admin", ADMIN_PASSWORD)
    fields = {
        "title": "Valid title", "description": "", "start_time": "2100-01-02T12:00",
        "end_time": "2100-01-03T12:00",
    }
    fields.update(invalid)
    before = election_management_snapshot(client)
    response = client.post(f"/admin/election/{route}", data=fields, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("extend_days", [0, 3])
def test_active_update_preserves_votes_and_results_with_same_or_later_end(client, extend_days):
    login(client, "voter1", VOTER_PASSWORD)
    client.post("/election/1/request")
    login(client, "admin", ADMIN_PASSWORD)
    client.post("/admin/election/1/voter/2/status", data={"status": "APPROVED"})
    login(client, "voter1", VOTER_PASSWORD)
    assert b"Vote successfully recorded!" in client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True).data
    with client.application.app_context():
        preserved = {
            table: [tuple(row) for row in get_db().execute(f"SELECT * FROM {table}")]
            for table in ("candidates", "voter_participation", "ballots")
        }
    fields = election_update_fields(client, title="Revised active election", description="")
    fields["end_time"] = (datetime.strptime(fields["end_time"], "%Y-%m-%dT%H:%M") + timedelta(days=extend_days)).strftime("%Y-%m-%dT%H:%M")
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/1/update", data=fields, follow_redirects=True)
    assert b"Election updated successfully." in response.data
    assert b"Official Published Results" not in response.data
    assert election_update_fields(client) == fields
    with client.application.app_context():
        for table, rows in preserved.items():
            assert [tuple(row) for row in get_db().execute(f"SELECT * FROM {table}")] == rows
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Official Published Results (Total Votes: 1)" in response.data
    assert b"100.0%" in response.data


@pytest.mark.parametrize("field", ["start_time", "end_time"])
def test_active_update_rejects_start_changes_and_shorter_end_atomically(client, field):
    set_management_election_state(client, "ACTIVE")
    login(client, "admin", ADMIN_PASSWORD)
    fields = election_update_fields(client, title="Must not be saved")
    fields[field] = (datetime.strptime(fields[field], "%Y-%m-%dT%H:%M") - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/update", data=fields, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("state", ["UPCOMING", "CLOSED"])
def test_update_rejects_starting_upcoming_election_now_and_changing_closed_election(client, state):
    set_management_election_state(client, state)
    login(client, "admin", ADMIN_PASSWORD)
    fields = election_update_fields(client, title="Must not be saved")
    if state == "UPCOMING":
        fields["start_time"] = datetime.now().strftime("%Y-%m-%dT%H:%M")
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/update", data=fields, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("username", [None, "voter1"])
def test_election_update_requires_admin_and_hides_edit_form(client, username):
    if username:
        login(client, username, VOTER_PASSWORD)
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/update", data=election_update_fields(client, title="Forbidden"))
    assert response.status_code == 302
    assert response.headers["Location"] == ("/" if username else "/login")
    assert b'action="/admin/election/1/update"' not in client.get("/election/1", follow_redirects=True).data
    assert election_management_snapshot(client) == before


def test_election_update_is_post_only_and_missing_election_returns_dashboard(client):
    login(client, "admin", ADMIN_PASSWORD)
    before = election_management_snapshot(client)
    assert client.get("/admin/election/1/update").status_code == 405
    response = client.post("/admin/election/999/update", data=election_update_fields(client))
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert b"Election not found." in client.get("/").data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("state", ["UPCOMING", "ACTIVE"])
def test_unchanged_update_has_no_writes_or_audit(client, state):
    set_management_election_state(client, state)
    login(client, "admin", ADMIN_PASSWORD)
    fields = election_update_fields(client)
    fields["title"] = "  " + fields["title"] + "  "
    fields["description"] = "  " + fields["description"] + "  "
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/update", data=fields, follow_redirects=True)
    assert b"No election changes to save." in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("route,state", [("create", "UPCOMING"), ("1/update", "UPCOMING"), ("1/close", "ACTIVE")])
def test_election_audit_failure_rolls_back_and_returns_generic_error(client, monkeypatch, route, state):
    set_management_election_state(client, state)
    login(client, "admin", ADMIN_PASSWORD)
    before = election_management_snapshot(client)
    real_log_audit = database.log_audit

    def fail_audit(*args):
        real_log_audit(*args)
        raise sqlite3.OperationalError("Private database failure details")

    monkeypatch.setattr(database, "log_audit", fail_audit)
    fields = {} if route == "1/close" else election_update_fields(client, title="Must roll back")
    if route == "create":
        fields["confirm_without_candidates"] = "yes"
    response = client.post(f"/admin/election/{route}", data=fields, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert b"Private database failure details" not in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("newer_action", ["extend", "close"])
def test_old_edit_form_cannot_overwrite_extension_or_closed_election(client, newer_action):
    set_management_election_state(client, "ACTIVE")
    login(client, "admin", ADMIN_PASSWORD)
    stale_fields = election_update_fields(client, title="Stale form title")
    if newer_action == "extend":
        latest = election_update_fields(client, title="Current title")
        latest["end_time"] = (datetime.strptime(latest["end_time"], "%Y-%m-%dT%H:%M") + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        response = client.post("/admin/election/1/update", data=latest, follow_redirects=True)
        assert b"Election updated successfully." in response.data
    else:
        response = client.post("/admin/election/1/close", follow_redirects=True)
        assert b"Election has been closed early" in response.data
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/update", data=stale_fields, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("newer_action", ["extend", "close"])
def test_waiting_update_reads_latest_election_after_acquiring_write_lock(client, monkeypatch, newer_action):
    set_management_election_state(client, "ACTIVE")
    stale_fields = election_update_fields(client, title="Stale waiting request")
    newer_end = stale_fields["end_time"]
    closed_early_at = None
    if newer_action == "extend":
        newer_end = (datetime.fromisoformat(newer_end) + timedelta(days=3)).isoformat(timespec="minutes")
    else:
        closed_early_at = datetime.now().isoformat(timespec="seconds")
    app = client.application
    blocker = sqlite3.connect(app.config["DATABASE"])
    waiting = Event()
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)

        def observe(statement):
            if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
                waiting.set()

        connection.set_trace_callback(observe)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def submit_update():
        with app.test_client() as requester:
            login(requester, "admin", ADMIN_PASSWORD)
            return requester.post("/admin/election/1/update", data=stale_fields, follow_redirects=True)

    try:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(submit_update)
            try:
                assert waiting.wait(timeout=3)
                assert not future.done()
                blocker.execute("UPDATE elections SET title = ?, end_time = ?, closed_early_at = ? WHERE id = 1", (
                    "Latest committed title", newer_end, closed_early_at,
                ))
                blocker.commit()
            finally:
                blocker.rollback()
            response = future.result(timeout=5)
    finally:
        blocker.close()
    assert b"alert-danger" in response.data
    fields = election_update_fields(client)
    assert fields["title"] == "Latest committed title"
    assert fields["end_time"] == newer_end
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 0
