"""Election deletion, confirmation, cascade, rollback and lock regressions."""
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from werkzeug.security import generate_password_hash

from election_system import db as database
from election_system.db import get_db, init_db

from .helpers import (
    ADMIN_PASSWORD,
    VOTER_PASSWORD,
    election_management_snapshot,
    login,
    set_management_election_state,
)


def set_state(client, state):
    scheduled_state = {"DRAFT": "UPCOMING", "EXPIRED_DRAFT": "CLOSED"}.get(state, state)
    set_management_election_state(client, scheduled_state)
    with client.application.app_context():
        with get_db() as db:
            db.execute("UPDATE elections SET is_confirmed = ? WHERE id = 1",
                       (int(state not in ("DRAFT", "EXPIRED_DRAFT")),))


@pytest.fixture
def closed_client(client):
    set_state(client, "CLOSED")
    login(client, "admin", ADMIN_PASSWORD)
    return client


@pytest.fixture(params=["DRAFT", "EXPIRED_DRAFT", "UPCOMING", "CLOSED"])
def deletable_client(client, request):
    set_state(client, request.param)
    login(client, "admin", ADMIN_PASSWORD)
    return client


def deletion_data(client, election_id=1):
    page = client.get(f"/election/{election_id}").data
    token = re.search(rb'name="deletion_token" value="([^"]+)"', page)
    assert token is not None
    return {"deletion_token": token[1].decode(), "confirmed": "yes"}


@pytest.mark.parametrize("state", ["DRAFT", "EXPIRED_DRAFT", "UPCOMING", "ACTIVE", "CLOSED"])
@pytest.mark.parametrize("role", ["ADMIN", "VOTER"])
def test_deletion_controls_only_appear_for_admin_on_deletable_detail(client, state, role):
    set_state(client, state)
    login(client, "admin" if role == "ADMIN" else "voter1", ADMIN_PASSWORD if role == "ADMIN" else VOTER_PASSWORD)
    page = client.get("/election/1", follow_redirects=True).data
    visible = role == "ADMIN" and state in ("DRAFT", "EXPIRED_DRAFT", "UPCOMING", "CLOSED")
    assert (b'id="open-election-deletion"' in page) is visible
    assert (b'<dialog id="election-deletion-dialog"' in page) is visible
    assert (b'name="deletion_token"' in page) is visible
    assert (b'election-deletion.js' in page) is visible
    assert (b'election-deletion.css' in page) is visible
    assert b'id="open-election-deletion"' not in client.get("/").data
    if visible:
        assert re.search(rb'<button[^>]*id="open-election-deletion"[^>]*disabled', page)
        assert re.search(rb'<button[^>]*id="confirm-election-deletion"[^>]*disabled', page)
        assert b'data-cancel-deletion autofocus' in page
        assert b'This cannot be undone.' in page
        assert b'Administrative audit logs will be retained' in page


def test_dialog_escapes_election_title_and_loading_the_page_does_not_delete(closed_client):
    client = closed_client
    title = '</strong><script>alert("delete")</script>'
    with client.application.app_context():
        with get_db() as db:
            db.execute("UPDATE elections SET title = ? WHERE id = 1", (title,))
    before = election_management_snapshot(client)
    page = client.get("/election/1").data
    assert title.encode() not in page
    assert b'&lt;/strong&gt;&lt;script&gt;' in page
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("role", ["ANONYMOUS", "VOTER"])
def test_deletion_requires_logged_in_administrator(deletable_client, role):
    client = deletable_client
    data = deletion_data(client)
    client.post("/logout")
    if role == "VOTER":
        login(client, "voter1", VOTER_PASSWORD)
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/delete", data=data, follow_redirects=True)
    assert (b"Please log in" if role == "ANONYMOUS" else b"Administrator privileges required") in response.data
    assert election_management_snapshot(client) == before


def test_deletion_rejects_get(closed_client):
    before = election_management_snapshot(closed_client)
    assert closed_client.get("/admin/election/1/delete").status_code == 405
    assert election_management_snapshot(closed_client) == before


@pytest.mark.parametrize("confirmation", [None, "", "no", "true"])
def test_deletion_requires_explicit_confirmation(deletable_client, confirmation):
    client = deletable_client
    data = deletion_data(client)
    data.pop("confirmed")
    if confirmation is not None:
        data["confirmed"] = confirmation
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/delete", data=data, follow_redirects=True)
    assert b"Confirm deletion" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("invalid", ["missing", "tampered", "other_election", "other_session", "other_admin", "missing_nonce"])
def test_deletion_token_is_bound_to_election_admin_and_session(deletable_client, invalid):
    client = deletable_client
    data = deletion_data(client)
    if invalid == "missing":
        data.pop("deletion_token")
    elif invalid == "tampered":
        data["deletion_token"] += "tampered"
    elif invalid == "other_election":
        with client.application.app_context():
            with get_db() as db:
                db.execute("INSERT INTO elections (title, start_time, end_time, is_confirmed) "
                           "VALUES ('Other closed election', '2000-01-01T00:00', '2000-01-02T00:00', 1)")
        data["deletion_token"] = deletion_data(client, 2)["deletion_token"]
    elif invalid == "other_session":
        client.post("/logout")
        login(client, "admin", ADMIN_PASSWORD)
        assert deletion_data(client)["deletion_token"] != data["deletion_token"]
    elif invalid == "other_admin":
        with client.application.app_context():
            with get_db() as db:
                db.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES ('admin2', ?, 'ADMIN')",
                    (generate_password_hash(ADMIN_PASSWORD),),
                )
        with client.session_transaction() as session:
            nonce = session["election_deletion_nonce"]
        login(client, "admin2", ADMIN_PASSWORD)
        with client.session_transaction() as session:
            session["election_deletion_nonce"] = nonce
    else:
        with client.session_transaction() as session:
            session.pop("election_deletion_nonce")
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/delete", data=data, follow_redirects=True)
    assert b"Invalid deletion confirmation" in response.data
    assert election_management_snapshot(client) == before


def test_backend_rejects_active_election_even_with_valid_confirmation(deletable_client):
    client = deletable_client
    data = deletion_data(client)
    set_state(client, "ACTIVE")
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/delete", data=data, follow_redirects=True)
    assert b"Only DRAFT, UPCOMING, or CLOSED elections can be deleted" in response.data
    assert election_management_snapshot(client) == before


def populate_related_data(client):
    with client.application.app_context():
        with get_db() as db:
            for status in ("PENDING", "APPROVED", "REJECTED"):
                voter_id = db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, 'unused', 'VOTER')",
                                      (status.lower(),)).lastrowid
                db.execute("INSERT INTO voter_participation VALUES (1, ?, ?, ?)",
                           (voter_id, status, int(status == "APPROVED")))
            db.execute("INSERT INTO ballots VALUES ('target-ballot', 1, 1)")
            db.execute("INSERT INTO elections (title, start_time, end_time, is_confirmed) "
                       "VALUES ('Other election', '2000-01-01T00:00', '2000-01-02T00:00', 1)")
            candidate_id = database.add_candidate(2, "Other candidate", "Other manifesto")
            db.execute("INSERT INTO voter_participation VALUES (2, 2, 'APPROVED', 1)")
            db.execute("INSERT INTO ballots VALUES ('other-ballot', 2, ?)", (candidate_id,))
            database.log_audit(1, "Historical action for Election ID 1")
            database.log_audit(1, "Historical action for Election ID 2")


@pytest.mark.parametrize("state", ["DRAFT", "EXPIRED_DRAFT", "UPCOMING", "CLOSED", "CLOSED_EARLY"])
def test_confirmed_deletion_cascades_and_preserves_other_elections_users_and_audits(client, state):
    set_state(client, "ACTIVE" if state == "CLOSED_EARLY" else state)
    login(client, "admin", ADMIN_PASSWORD)
    if state == "CLOSED_EARLY":
        response = client.post("/admin/election/1/close", follow_redirects=True)
        assert b"Election has been closed early" in response.data
    populate_related_data(client)
    with client.application.app_context():
        db = get_db()
        title = database.find_election(1)["title"]
        users = [tuple(row) for row in db.execute("SELECT * FROM users ORDER BY id")]
        audit_logs = [tuple(row) for row in db.execute("SELECT * FROM audit_logs ORDER BY id")]
        other = {table: [tuple(row) for row in db.execute(
            f"SELECT * FROM {table} WHERE {'id' if table == 'elections' else 'election_id'} = 2"
        )] for table in ("elections", "candidates", "ballots", "voter_participation")}
    response = client.post("/admin/election/1/delete", data=deletion_data(client))
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert b"deleted permanently" in client.get("/").data
    with client.application.app_context():
        db = get_db()
        for table in ("elections", "candidates", "ballots", "voter_participation"):
            key = "id" if table == "elections" else "election_id"
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE {key} = 1").fetchone()[0] == 0
            assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE {key} = 2")] == other[table]
        assert database.list_results(1) == []
        assert [tuple(row) for row in db.execute("SELECT * FROM users ORDER BY id")] == users
        logs = [tuple(row) for row in db.execute("SELECT * FROM audit_logs ORDER BY id")]
        assert logs[:-1] == audit_logs
        assert logs[-1][1] == 1
        assert f"Deleted Election ID 1 ('{title}') permanently" == logs[-1][2]
        assert logs[-1][3]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        init_db()
        assert database.find_election(1) is None
    login(client, "voter1", VOTER_PASSWORD)
    response = client.get("/election/1", follow_redirects=True)
    assert b"Election not found" in response.data


@pytest.mark.parametrize("state", ["DRAFT", "EXPIRED_DRAFT"])
def test_draft_without_candidates_can_be_deleted(client, state):
    set_state(client, state)
    with client.application.app_context():
        with get_db() as db:
            db.execute("DELETE FROM candidates WHERE election_id = 1")
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post("/admin/election/1/delete", data=deletion_data(client), follow_redirects=True)
    assert b"deleted permanently" in response.data
    with client.application.app_context():
        assert database.find_election(1) is None
        assert len(database.list_recent_audit_logs()) == 1


def test_missing_election_and_duplicate_deletion_return_to_dashboard_without_extra_audit(closed_client):
    client = closed_client
    data = deletion_data(client)
    client.post("/admin/election/1/delete", data=data)
    before = election_management_snapshot(client)
    for election_id in (1, 999):
        response = client.post(f"/admin/election/{election_id}/delete", data=data)
        assert response.headers["Location"] == "/"
        assert b"Election not found" in client.get("/").data
        assert election_management_snapshot(client) == before


@pytest.mark.parametrize("failure", ["cascade", "audit"])
def test_failed_deletion_rolls_back_all_records_and_hides_database_error(deletable_client, monkeypatch, failure):
    client = deletable_client
    populate_related_data(client)
    data = deletion_data(client)
    if failure == "cascade":
        with client.application.app_context():
            with get_db() as db:
                db.execute("CREATE TRIGGER fail_ballot_delete BEFORE DELETE ON ballots "
                           "BEGIN SELECT RAISE(ABORT, 'Private deletion failure'); END")
    else:
        def fail_audit(*args):
            raise sqlite3.OperationalError("Private deletion failure")
        monkeypatch.setattr(database, "log_audit", fail_audit)
    before = election_management_snapshot(client)
    response = client.post("/admin/election/1/delete", data=data, follow_redirects=True)
    assert b"Unable to delete the election. Changes were rolled back" in response.data
    assert b"Private deletion failure" not in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("newer_action", ["activate", "delete"])
def test_waiting_deletion_reloads_state_after_acquiring_write_lock(deletable_client, monkeypatch, newer_action):
    client = deletable_client
    data = deletion_data(client)
    app = client.application
    with client.session_transaction() as session:
        admin_session = dict(session)
    blocker = sqlite3.connect(app.config["DATABASE"])
    blocker.execute("PRAGMA foreign_keys = ON")
    waiting = Event()
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: waiting.set() if sql.strip().upper().startswith("BEGIN IMMEDIATE") else None)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def submit():
        with app.test_client() as requester:
            with requester.session_transaction() as session:
                session.update(admin_session)
            return requester.post("/admin/election/1/delete", data=data, follow_redirects=True)

    try:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(submit)
            try:
                assert waiting.wait(timeout=3)
                assert not future.done()
                if newer_action == "activate":
                    blocker.execute("UPDATE elections SET is_confirmed = 1, start_time = '2000-01-01T00:00', "
                                    "end_time = '2100-01-01T00:00' WHERE id = 1")
                else:
                    blocker.execute("DELETE FROM elections WHERE id = 1")
                blocker.commit()
            finally:
                blocker.rollback()
            response = future.result(timeout=5)
    finally:
        blocker.close()
    assert (b"Only DRAFT, UPCOMING, or CLOSED elections" if newer_action == "activate" else b"Election not found") in response.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 0
        election = database.find_election(1)
        assert (election is not None) is (newer_action == "activate")
