"""Candidate editing, explicit election readiness, migration and race regressions."""
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html.parser import HTMLParser
from threading import Event

import pytest

from election_system import create_app, db as database, services
from election_system.db import get_db, init_db
from test_app import client, election_management_snapshot, login


def draft(client, *, future=False, count=2):
    login(client, "admin", "admin123")
    now = datetime.now()
    response = client.post("/admin/election/create", data={
        "title": "Private draft election", "description": "Still preparing the roster",
        "start_time": (now + timedelta(hours=2) if future else now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end_time": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
    }, follow_redirects=True)
    assert b"Election created successfully" in response.data
    with client.application.app_context():
        election_id = get_db().execute("SELECT MAX(id) FROM elections").fetchone()[0]
    for index in range(count):
        response = client.post(f"/admin/election/{election_id}/candidate/add", data={
            "name": f"Candidate {index + 1}", "manifesto": f"Manifesto {index + 1}",
        }, follow_redirects=True)
        assert b"added." in response.data
    return election_id


def row(client, election_id):
    with client.application.app_context():
        return dict(database.find_election(election_id))


def candidates(client, election_id):
    with client.application.app_context():
        return [dict(item) for item in database.list_candidates(election_id)]


def form_token(client, election_id, field, action):
    page = client.get(f"/election/{election_id}").data
    form = re.search(rb'<form\b[^>]*action="' + action.encode() + rb'"[^>]*>(.*?)</form>', page, re.DOTALL)
    assert form is not None
    token = re.search(rb'name="' + field.encode() + rb'"\s+value="([^"]*)"', form[1])
    assert token is not None
    return token[1].decode()


def edit_data(client, election_id, candidate_id, **changes):
    candidate = next(item for item in candidates(client, election_id) if item["id"] == candidate_id)
    data = {field: candidate[field] for field in ("name", "manifesto")}
    data["edit_token"] = form_token(
        client, election_id, "edit_token", f"/admin/election/{election_id}/candidate/{candidate_id}/update"
    )
    data.update(changes)
    return data


def confirmation_data(client, election_id):
    return {"confirmation_token": form_token(
        client, election_id, "confirmation_token", f"/admin/election/{election_id}/confirm"
    )}


def confirm(client, election_id):
    return client.post(f"/admin/election/{election_id}/confirm", data=confirmation_data(client, election_id), follow_redirects=True)


def assert_state(client, election_id, expected):
    page = client.get(f"/election/{election_id}").data
    assert re.search(rb'>\s*' + expected.encode() + rb'\s*</span>', page)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_drafts_never_auto_start_and_require_two_candidates(client, count):
    election_id = draft(client, count=count)
    assert_state(client, election_id, "DRAFT")
    assert row(client, election_id)["is_confirmed"] == 0
    before = election_management_snapshot(client)
    response = confirm(client, election_id)
    if count < 2:
        assert b"At least two candidates" in response.data
        assert election_management_snapshot(client) == before
    else:
        assert b"Voting is now active" in response.data
        assert_state(client, election_id, "ACTIVE")


def test_confirmed_schedule_starts_automatically_and_repeat_confirmation_is_noop(client, monkeypatch):
    election_id = draft(client, future=True)
    data = confirmation_data(client, election_id)
    assert b"scheduled start time" in confirm(client, election_id).data
    assert_state(client, election_id, "UPCOMING")
    before = election_management_snapshot(client)
    response = client.post(f"/admin/election/{election_id}/confirm", data=data, follow_redirects=True)
    assert b"already confirmed" in response.data
    assert election_management_snapshot(client) == before
    start = datetime.strptime(row(client, election_id)["start_time"], "%Y-%m-%dT%H:%M")

    class Clock(datetime):
        @classmethod
        def now(cls):
            return start + timedelta(minutes=1)

    monkeypatch.setattr(services, "datetime", Clock)
    assert_state(client, election_id, "ACTIVE")
    assert b"ACTIVE" in client.get("/").data


def test_drafts_are_private_and_cannot_receive_participation_or_votes(client):
    election_id = draft(client)
    login(client, "voter1", "voter123")
    before = election_management_snapshot(client)
    assert b"Private draft election" not in client.get("/").data
    assert b"Private draft election" not in client.get(f"/election/{election_id}", follow_redirects=True).data
    assert b"Participation is not available" in client.post(f"/election/{election_id}/request", follow_redirects=True).data
    candidate_id = candidates(client, election_id)[0]["id"]
    assert b"not currently active" in client.post(f"/election/{election_id}/vote", data={"candidate_id": candidate_id}, follow_redirects=True).data
    assert election_management_snapshot(client) == before
    login(client, "admin", "admin123")
    assert b"Only ACTIVE elections" in client.post(f"/admin/election/{election_id}/close", follow_redirects=True).data
    assert_state(client, election_id, "DRAFT")


def test_expired_draft_can_be_rescheduled_but_not_confirmed_until_fixed(client):
    election_id = draft(client)
    fields = {field: row(client, election_id)[field] for field in ("title", "description", "start_time", "end_time")}
    fields.update(start_time="2000-01-01T00:00", end_time="2000-01-02T00:00")
    assert b"Election updated successfully" in client.post(f"/admin/election/{election_id}/update", data=fields, follow_redirects=True).data
    assert_state(client, election_id, "DRAFT")
    assert b"end time has passed" in confirm(client, election_id).data
    fields.update(start_time="2100-01-01T00:00", end_time="2100-01-02T00:00")
    assert b"Election updated successfully" in client.post(f"/admin/election/{election_id}/update", data=fields, follow_redirects=True).data
    assert b"scheduled start time" in confirm(client, election_id).data
    assert_state(client, election_id, "UPCOMING")


def test_candidate_edit_preserves_id_and_audits_before_after_with_escaping(client):
    election_id = draft(client)
    candidate = candidates(client, election_id)[0]
    response = client.post(f"/admin/election/{election_id}/candidate/{candidate['id']}/update", data=edit_data(
        client, election_id, candidate["id"], name='  Candidate <script>alert(1)</script>  ',
        manifesto='  </textarea><script>alert(2)</script>  ',
    ), follow_redirects=True)
    assert b"Candidate updated successfully" in response.data
    assert b"<script>alert" not in response.data
    assert b"&lt;/textarea&gt;" in response.data
    updated = candidates(client, election_id)[0]
    assert updated["id"] == candidate["id"]
    assert updated["name"] == "Candidate <script>alert(1)</script>"
    with client.application.app_context():
        audit = dict(get_db().execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone())
    assert audit["admin_id"] == 1
    assert candidate["name"] in audit["action"] and updated["name"] in audit["action"]
    assert candidate["manifesto"] in audit["action"] and updated["manifesto"] in audit["action"]


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("field", ["name", "manifesto"])
def test_name_and_manifesto_required_on_add_and_update_without_revoking_confirmation(client, operation, field):
    election_id = draft(client, future=True)
    candidate_id = candidates(client, election_id)[0]["id"]
    confirm(client, election_id)
    data = edit_data(client, election_id, candidate_id) if operation == "update" else {"name": "New", "manifesto": "New manifesto"}
    data[field] = "   "
    route = f"/admin/election/{election_id}/candidate/" + (f"{candidate_id}/update" if operation == "update" else "add")
    before = election_management_snapshot(client)
    assert b"both required" in client.post(route, data=data, follow_redirects=True).data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("operation", ["add", "update", "delete"])
def test_roster_changes_revoke_confirmation_and_preserve_participation(client, operation):
    election_id = draft(client, future=True)
    candidate_id = candidates(client, election_id)[0]["id"]
    confirm(client, election_id)
    login(client, "voter1", "voter123")
    client.post(f"/election/{election_id}/request")
    login(client, "admin", "admin123")
    data = {"name": "New candidate", "manifesto": "New manifesto"}
    if operation == "update":
        data = edit_data(client, election_id, candidate_id, name="Updated name")
    route = f"/admin/election/{election_id}/candidate/" + ("add" if operation == "add" else f"{candidate_id}/{operation}")
    assert b"returned to draft" in client.post(route, data=data, follow_redirects=True).data
    assert_state(client, election_id, "DRAFT")
    with client.application.app_context():
        assert database.find_participation(election_id, 2)["status"] == "PENDING"
        assert '"is_confirmed": {"before": 1, "after": 0}' in get_db().execute("SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()[0]


@pytest.mark.parametrize("confirmed", [False, True])
def test_candidate_noop_keeps_confirmation_and_does_not_audit(client, confirmed):
    election_id = draft(client, future=True)
    if confirmed:
        confirm(client, election_id)
    candidate_id = candidates(client, election_id)[0]["id"]
    data = edit_data(client, election_id, candidate_id)
    data.update(name="  " + data["name"] + " ", manifesto=" " + data["manifesto"] + "  ")
    before = election_management_snapshot(client)
    assert b"No candidate changes to save" in client.post(f"/admin/election/{election_id}/candidate/{candidate_id}/update", data=data, follow_redirects=True).data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("change", ["update", "delete"])
def test_old_candidate_form_cannot_overwrite_or_recreate_a_candidate(client, change):
    election_id = draft(client)
    candidate_id = candidates(client, election_id)[0]["id"]
    stale = edit_data(client, election_id, candidate_id, name="Stale name")
    fresh = edit_data(client, election_id, candidate_id, name="Latest name")
    client.post(f"/admin/election/{election_id}/candidate/{candidate_id}/{change}", data=fresh)
    before = election_management_snapshot(client)
    response = client.post(f"/admin/election/{election_id}/candidate/{candidate_id}/update", data=stale, follow_redirects=True)
    assert b"alert-danger" in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("invalid", ["missing", "tampered", "different_candidate", "different_election"])
def test_candidate_identity_and_signed_original_are_enforced(client, invalid):
    election_id = draft(client)
    roster = candidates(client, election_id)
    candidate_id = roster[0]["id"]
    data = edit_data(client, election_id, candidate_id, name="Forbidden change")
    target_election = election_id
    if invalid == "missing":
        data.pop("edit_token")
    elif invalid == "tampered":
        data["edit_token"] += "tampered"
    elif invalid == "different_candidate":
        candidate_id = roster[1]["id"]
    else:
        target_election = draft(client, count=0)
    before = election_management_snapshot(client)
    assert b"alert-danger" in client.post(f"/admin/election/{target_election}/candidate/{candidate_id}/update", data=data, follow_redirects=True).data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("change", ["add", "update", "delete", "schedule"])
def test_stale_confirmation_cannot_confirm_a_changed_roster_or_schedule(client, change):
    election_id = draft(client, future=True)
    token = confirmation_data(client, election_id)
    candidate_id = candidates(client, election_id)[0]["id"]
    if change == "schedule":
        data = {field: row(client, election_id)[field] for field in ("title", "description", "start_time", "end_time")}
        data["end_time"] = "2100-01-01T00:00"
        client.post(f"/admin/election/{election_id}/update", data=data)
    else:
        data = edit_data(client, election_id, candidate_id, name="Updated name") if change == "update" else {"name": "Added", "manifesto": "Complete"}
        route = "add" if change == "add" else f"{candidate_id}/{change}"
        client.post(f"/admin/election/{election_id}/candidate/{route}", data=data)
    before = election_management_snapshot(client)
    response = client.post(f"/admin/election/{election_id}/confirm", data=token, follow_redirects=True)
    assert b"out of date" in response.data
    assert election_management_snapshot(client) == before


def test_all_candidates_must_be_complete_and_same_names_are_allowed(client):
    election_id = draft(client)
    roster = candidates(client, election_id)
    client.post(f"/admin/election/{election_id}/candidate/{roster[1]['id']}/update", data=edit_data(
        client, election_id, roster[1]["id"], name=roster[0]["name"],
    ))
    with client.application.app_context():
        db = get_db()
        db.execute("UPDATE candidates SET manifesto = '   ' WHERE id = ?", (roster[1]["id"],))
        db.commit()
    before = election_management_snapshot(client)
    assert b"Every candidate must have" in confirm(client, election_id).data
    assert election_management_snapshot(client) == before
    client.post(f"/admin/election/{election_id}/candidate/{roster[1]['id']}/update", data=edit_data(
        client, election_id, roster[1]["id"], manifesto="Complete manifesto",
    ))
    assert b"Voting is now active" in confirm(client, election_id).data


@pytest.mark.parametrize("operation", ["add", "update", "delete", "confirm"])
def test_candidate_and_confirmation_audit_failures_roll_back_all_changes(client, monkeypatch, operation):
    election_id = draft(client, future=True)
    if operation != "confirm":
        confirm(client, election_id)
    candidate_id = candidates(client, election_id)[0]["id"]
    if operation == "confirm":
        route, data = f"/admin/election/{election_id}/confirm", confirmation_data(client, election_id)
    else:
        route = f"/admin/election/{election_id}/candidate/" + ("add" if operation == "add" else f"{candidate_id}/{operation}")
        data = edit_data(client, election_id, candidate_id, name="Must roll back") if operation == "update" else {"name": "New", "manifesto": "New manifesto"}
    before = election_management_snapshot(client)

    def fail_audit(*args):
        raise sqlite3.OperationalError("Private audit failure")

    monkeypatch.setattr(database, "log_audit", fail_audit)
    response = client.post(route, data=data, follow_redirects=True)
    assert b"rolled back" in response.data and b"Private audit failure" not in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("role", [None, "voter1"])
@pytest.mark.parametrize("action", ["candidate/1/update", "confirm"])
def test_new_routes_require_administrator_and_post(client, role, action):
    if role:
        login(client, role, "voter123")
    before = election_management_snapshot(client)
    route = "/admin/election/1/" + action
    assert client.get(route).status_code == 405
    assert client.post(route, data={}).headers["Location"] == ("/" if role else "/login")
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("state", ["ACTIVE", "CLOSED"])
def test_candidate_edits_lock_when_voting_starts_and_closed_elections_cannot_reopen(client, state):
    election_id = draft(client)
    candidate_id = candidates(client, election_id)[0]["id"]
    stale_edit = edit_data(client, election_id, candidate_id, name="Must not save")
    stale_confirm = confirmation_data(client, election_id)
    confirm(client, election_id)
    if state == "CLOSED":
        client.post(f"/admin/election/{election_id}/close")
    before = election_management_snapshot(client)
    assert b"alert-danger" in client.post(f"/admin/election/{election_id}/candidate/{candidate_id}/update", data=stale_edit, follow_redirects=True).data
    if state == "CLOSED":
        assert b"cannot be confirmed or reopened" in client.post(f"/admin/election/{election_id}/confirm", data=stale_confirm, follow_redirects=True).data
    assert b"Save Candidate" not in client.get(f"/election/{election_id}").data
    assert election_management_snapshot(client) == before


def test_candidate_edit_forms_are_independent_and_required(client):
    election_id = draft(client)

    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.form = None
            self.edits = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                assert self.form is None, "Forms must not be nested"
                self.form = attrs["action"]
                if "/candidate/" in self.form and self.form.endswith("/update"):
                    self.edits += 1
            if self.form and "/candidate/" in self.form and self.form.endswith("/update"):
                if attrs.get("name") in ("name", "manifesto"):
                    assert "required" in attrs

        def handle_endtag(self, tag):
            if tag == "form":
                assert self.form is not None
                self.form = None

    parser = FormParser()
    parser.feed(client.get(f"/election/{election_id}").data.decode())
    assert parser.edits == 2 and parser.form is None


def wait_for_locked_request(app, monkeypatch, route, data, mutate, role="ADMIN"):
    """Synchronize an actual SQLite lock wait without timing-based sleeps."""
    blocker = sqlite3.connect(app.config["DATABASE"])
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
                session.update(user_id=1 if role == "ADMIN" else 2, role=role)
            return requester.post(route, data=data, follow_redirects=True)

    try:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(submit)
            try:
                assert waiting.wait(timeout=3)
                assert not future.done()
                mutate(blocker)
                blocker.commit()
            finally:
                blocker.rollback()
            return future.result(timeout=5)
    finally:
        blocker.close()


@pytest.mark.parametrize("newer_action", ["update", "delete", "start"])
def test_waiting_candidate_edit_rechecks_candidate_and_election(client, monkeypatch, newer_action):
    election_id = draft(client, future=True)
    candidate_id = candidates(client, election_id)[0]["id"]
    data = edit_data(client, election_id, candidate_id, name="Stale waiting edit")
    confirm(client, election_id)
    before_logs = None
    with client.application.app_context():
        before_logs = get_db().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]

    def mutate(db):
        if newer_action == "update":
            db.execute("UPDATE candidates SET name = 'Latest committed name' WHERE id = ?", (candidate_id,))
        elif newer_action == "delete":
            db.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        else:
            db.execute("UPDATE elections SET start_time = ? WHERE id = ?", (
                (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), election_id,
            ))

    response = wait_for_locked_request(client.application, monkeypatch,
        f"/admin/election/{election_id}/candidate/{candidate_id}/update", data, mutate)
    assert b"alert-danger" in response.data
    assert not any(item["name"] == "Stale waiting edit" for item in candidates(client, election_id))
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == before_logs


def test_confirmation_waiting_for_roster_deletion_does_not_start(client, monkeypatch):
    election_id = draft(client)
    candidate_id = candidates(client, election_id)[0]["id"]
    data = confirmation_data(client, election_id)
    response = wait_for_locked_request(client.application, monkeypatch,
        f"/admin/election/{election_id}/confirm", data,
        lambda db: db.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,)))
    assert b"out of date" in response.data
    assert row(client, election_id)["is_confirmed"] == 0


@pytest.mark.parametrize("future", [False, True])
def test_deletion_waiting_for_confirmation_rechecks_effective_state(client, monkeypatch, future):
    election_id = draft(client, future=future)
    candidate_id = candidates(client, election_id)[0]["id"]
    response = wait_for_locked_request(client.application, monkeypatch,
        f"/admin/election/{election_id}/candidate/{candidate_id}/delete", {},
        lambda db: db.execute("UPDATE elections SET is_confirmed = 1 WHERE id = ?", (election_id,)))
    if future:
        assert b"returned to draft" in response.data
        assert row(client, election_id)["is_confirmed"] == 0
        assert len(candidates(client, election_id)) == 1
    else:
        assert b"cannot be removed" in response.data
        assert row(client, election_id)["is_confirmed"] == 1
        assert len(candidates(client, election_id)) == 2


@pytest.mark.parametrize("newer_action", ["close", "reject"])
def test_waiting_vote_rechecks_latest_election_and_eligibility(client, monkeypatch, newer_action):
    with client.application.app_context():
        db = get_db()
        db.execute("INSERT INTO voter_participation VALUES (1, 2, 'APPROVED', 0)")
        db.commit()

    def mutate(db):
        if newer_action == "close":
            db.execute("UPDATE elections SET end_time = ? WHERE id = 1", (
                (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
            ))
        else:
            db.execute("UPDATE voter_participation SET status = 'REJECTED' WHERE election_id = 1 AND voter_id = 2")

    response = wait_for_locked_request(client.application, monkeypatch,
        "/election/1/vote", {"candidate_id": 1}, mutate, role="VOTER")
    assert b"not currently active" in response.data if newer_action == "close" else b"not approved" in response.data
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 0
        assert database.find_participation(1, 2)["has_voted"] == 0


@pytest.mark.parametrize("invalid", ["missing", "tampered", "different_election"])
def test_confirmation_requires_an_authentic_snapshot_for_that_election(client, invalid):
    election_id = draft(client)
    data = confirmation_data(client, election_id)
    if invalid == "missing":
        data = {}
    elif invalid == "tampered":
        data["confirmation_token"] += "tampered"
    else:
        election_id = draft(client)
    before = election_management_snapshot(client)
    assert b"alert-danger" in client.post(f"/admin/election/{election_id}/confirm", data=data, follow_redirects=True).data
    assert election_management_snapshot(client) == before


def legacy_app(tmp_path, *, blocked=False):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    schema = database.SCHEMA.replace(
        ",\n    is_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(is_confirmed IN (0, 1))", ""
    )
    db.executescript(schema)
    db.execute("INSERT INTO users VALUES (1, 'legacy_admin', 'hash', 'ADMIN')")
    db.execute("INSERT INTO users VALUES (2, 'legacy_voter', 'hash', 'VOTER')")
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    for election_id, start, end in [
        (1, "2000-01-01T00:00", "2000-01-02T00:00"),
        (2, future, later), (3, "2000-01-01T00:00", later), (4, future, later),
        (5, "2000-01-01T00:00", "2000-01-02T00:00"),
    ]:
        db.execute("INSERT INTO elections VALUES (?, ?, '', ?, ?)", (election_id, f"Legacy {election_id}", start, end))
    for candidate_id, election_id, manifesto in [(1, 1, "Old"), (2, 2, "A"), (3, 2, "B"), (4, 3, "Only one"), (5, 4, "A"), (6, 4, " ")]:
        db.execute("INSERT INTO candidates VALUES (?, ?, ?, ?)", (candidate_id, election_id, f"Candidate {candidate_id}", manifesto))
    db.execute("INSERT INTO voter_participation VALUES (1, 2, 'APPROVED', 1)")
    db.execute("INSERT INTO ballots VALUES ('historical-ballot', 1, 1)")
    db.execute("INSERT INTO audit_logs VALUES (1, 1, 'Historical action', '2000-01-01 00:00:00')")
    if blocked:
        db.execute("INSERT INTO voter_participation VALUES (3, 2, 'APPROVED', 1)")
        db.execute("INSERT INTO ballots VALUES ('active-ballot', 3, 4)")
    db.commit()
    db.close()
    return create_app({"TESTING": True, "DATABASE": str(path)})


def test_legacy_upgrade_preserves_history_and_only_backfills_once(tmp_path):
    app = legacy_app(tmp_path)
    with app.app_context():
        db = get_db()
        preserved = {table: [tuple(item) for item in db.execute(f"SELECT * FROM {table}")]
                     for table in ("users", "candidates", "voter_participation", "ballots", "audit_logs")}
        init_db()
        assert [item[0] for item in db.execute("SELECT is_confirmed FROM elections ORDER BY id")] == [1, 1, 0, 0, 1]
        assert services.get_election_status(database.find_election(1)) == "CLOSED"
        assert services.get_election_status(database.find_election(3)) == "DRAFT"
        for table, values in preserved.items():
            assert [tuple(item) for item in db.execute(f"SELECT * FROM {table}")] == values
        db.execute("UPDATE elections SET is_confirmed = 0 WHERE id = 2")
        db.commit()
        before = list(db.iterdump())
        init_db()
        assert list(db.iterdump()) == before


def test_legacy_upgrade_preflight_blocks_live_ballots_without_any_schema_or_data_changes(tmp_path):
    app = legacy_app(tmp_path, blocked=True)
    with app.app_context():
        db = get_db()
        before = list(db.iterdump())
        with pytest.raises(RuntimeError, match="Close these elections.*3"):
            init_db()
        assert list(db.iterdump()) == before
        assert "is_confirmed" not in {item["name"] for item in db.execute("PRAGMA table_info(elections)")}


def test_legacy_upgrade_rolls_back_schema_if_backfill_fails(tmp_path):
    app = legacy_app(tmp_path)
    with app.app_context():
        db = get_db()
        db.execute("CREATE TRIGGER fail_migration BEFORE UPDATE ON elections BEGIN SELECT RAISE(ABORT, 'Migration failed'); END")
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="Migration failed"):
            init_db()
        assert list(db.iterdump()) == before
