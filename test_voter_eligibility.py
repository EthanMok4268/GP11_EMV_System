"""Eligibility closure, stale forms, atomic decisions and lock-wait regressions."""
import sqlite3
from datetime import datetime, timedelta

import pytest

from election_system import db as database, services
from election_system.db import get_db
from test_app import client, election_management_snapshot, login, set_management_election_state
from test_candidate_management import wait_for_locked_request


def eligibility_action(action, election_id=1):
    if action == "request":
        return f"/election/{election_id}/request", {}, "VOTER"
    return f"/admin/election/{election_id}/voter/2/status", {"status": action}, "ADMIN"


def seed_participation(client, status="PENDING"):
    with client.application.app_context():
        with get_db() as db:
            db.execute("INSERT INTO voter_participation VALUES (1, 2, ?, 0)", (status,))


def seed_history(client):
    with client.application.app_context():
        with get_db() as db:
            voter_id = db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES ('previous_voter', 'unused', 'VOTER')"
            ).lastrowid
            db.execute("INSERT INTO voter_participation VALUES (1, ?, 'APPROVED', 1)", (voter_id,))
            db.execute("INSERT INTO ballots VALUES ('existing-ballot', 1, 1)")
            database.log_audit(1, "Existing administrative action")


def close_election(client, closure):
    if closure == "scheduled":
        set_management_election_state(client, "CLOSED")
    else:
        with client.application.test_client() as administrator:
            with administrator.session_transaction() as session:
                session.update(user_id=1, role="ADMIN")
            response = administrator.post("/admin/election/1/close", follow_redirects=True)
            assert b"Election has been closed early" in response.data


@pytest.mark.parametrize("action", ["request", "APPROVED", "REJECTED"])
@pytest.mark.parametrize("closure", ["scheduled", "early"])
def test_closed_election_blocks_requests_and_decisions_from_old_forms(client, closure, action):
    seed_history(client)
    if action != "request":
        seed_participation(client)
    route, data, role = eligibility_action(action)
    login(client, "voter1" if role == "VOTER" else "admin", "voter123" if role == "VOTER" else "admin123")
    assert f'action="{route}"'.encode() in client.get("/election/1").data

    close_election(client, closure)
    before = election_management_snapshot(client)
    response = client.post(route, data=data)
    assert response.status_code == 302
    assert response.headers["Location"] == "/election/1"
    page = client.get(response.headers["Location"]).data
    assert b"alert-danger" in page
    assert b"closed" in page
    assert b"Participation request submitted" not in page
    assert b"Voter eligibility updated" not in page
    assert f'action="{route}"'.encode() not in page
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("status", ["PENDING", "APPROVED", "REJECTED"])
@pytest.mark.parametrize("role", ["VOTER", "ADMIN"])
def test_closed_eligibility_records_remain_visible_without_action_forms(client, role, status):
    seed_participation(client, status)
    set_management_election_state(client, "CLOSED")
    login(client, "voter1" if role == "VOTER" else "admin", "voter123" if role == "VOTER" else "admin123")
    before = election_management_snapshot(client)
    page = client.get("/election/1").data
    assert status.encode() in page
    assert b'action="/election/1/request"' not in page
    assert b'action="/admin/election/1/voter/2/status"' not in page
    assert b">Request Participation</button>" not in page
    assert b">Approve</button>" not in page
    assert b">Reject</button>" not in page
    if role == "VOTER":
        assert b"Your Eligibility Status:" in page
    else:
        assert b"voter1" in page
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("state", ["UPCOMING", "ACTIVE"])
@pytest.mark.parametrize("status", ["APPROVED", "REJECTED"])
def test_open_election_still_accepts_requests_and_decisions(client, state, status):
    set_management_election_state(client, state)
    login(client, "voter1", "voter123")
    assert b'action="/election/1/request"' in client.get("/election/1").data
    response = client.post("/election/1/request", follow_redirects=True)
    assert b"Participation request submitted" in response.data
    before_duplicate = election_management_snapshot(client)
    response = client.post("/election/1/request", follow_redirects=True)
    assert b"already submitted a request" in response.data
    assert election_management_snapshot(client) == before_duplicate

    login(client, "admin", "admin123")
    assert b'action="/admin/election/1/voter/2/status"' in client.get("/election/1").data
    response = client.post("/admin/election/1/voter/2/status", data={"status": status}, follow_redirects=True)
    assert f"Voter eligibility updated to {status}.".encode() in response.data
    with client.application.app_context():
        participation = database.find_participation(1, 2)
        assert participation["status"] == status
        assert participation["has_voted"] == 0
        logs = get_db().execute("SELECT * FROM audit_logs").fetchall()
        assert len(logs) == 1
        assert logs[0]["admin_id"] == 1
        assert f"Set Voter ID 2 eligibility to {status} for Election ID 1" == logs[0]["action"]

    if state == "ACTIVE" and status == "APPROVED":
        login(client, "voter1", "voter123")
        response = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
        assert b"Vote successfully recorded!" in response.data


@pytest.mark.parametrize("action", ["request", "APPROVED", "REJECTED"])
def test_missing_election_eligibility_actions_return_to_dashboard(client, action):
    route, data, role = eligibility_action(action, election_id=999)
    login(client, "voter1" if role == "VOTER" else "admin", "voter123" if role == "VOTER" else "admin123")
    before = election_management_snapshot(client)
    response = client.post(route, data=data)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert b"Election not found." in client.get("/").data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED"])
def test_eligibility_audit_failure_rolls_back_decision_and_audit(client, monkeypatch, status):
    seed_participation(client)
    login(client, "admin", "admin123")
    before = election_management_snapshot(client)
    real_log_audit = database.log_audit

    def fail_audit(*args):
        real_log_audit(*args)
        raise sqlite3.OperationalError("Private audit failure")

    monkeypatch.setattr(database, "log_audit", fail_audit)
    response = client.post("/admin/election/1/voter/2/status", data={"status": status}, follow_redirects=True)
    assert b"Changes were rolled back" in response.data
    assert b"Private audit failure" not in response.data
    assert b"Voter eligibility updated" not in response.data
    assert election_management_snapshot(client) == before


@pytest.mark.parametrize("action", ["request", "APPROVED", "REJECTED"])
@pytest.mark.parametrize("closure", ["early", "scheduled"])
def test_waiting_eligibility_actions_recheck_closed_state(client, monkeypatch, action, closure):
    seed_history(client)
    if action != "request":
        seed_participation(client)
    route, data, role = eligibility_action(action)
    with client.application.app_context():
        end_time = datetime.strptime(database.find_election(1)["end_time"], "%Y-%m-%dT%H:%M")

    class Clock(datetime):
        @classmethod
        def now(cls):
            return end_time + timedelta(minutes=1)

    before = None

    def close_while_waiting(db):
        nonlocal before
        if closure == "early":
            db.execute("UPDATE elections SET end_time = ? WHERE id = 1", (
                (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
            ))
        else:
            monkeypatch.setattr(services, "datetime", Clock)
        before = list(db.iterdump())

    response = wait_for_locked_request(
        client.application, monkeypatch, route, data, close_while_waiting, role=role,
    )
    assert b"alert-danger" in response.data
    assert b"closed" in response.data
    assert b"Participation request submitted" not in response.data
    assert b"Voter eligibility updated" not in response.data
    assert election_management_snapshot(client) == before
