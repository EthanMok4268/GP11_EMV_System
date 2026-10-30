import pytest
from election_system import create_app
from election_system.db import get_db, init_db

@pytest.fixture
def client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    with app.app_context():
        init_db()
    with app.test_client() as client:
        yield client


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)

def test_rbac_blocks_voter_from_admin_routes(client):
    """TC-02: Verify voters cannot create elections."""
    login(client, "voter1", "voter123")
    res = client.post("/admin/election/create", data={
        "title": "Unauthorized Election",
        "start_time": "2026-01-01T00:00",
        "end_time": "2026-12-31T00:00"
    }, follow_redirects=True)
    assert b"Access Denied: Administrator privileges required" in res.data

def test_voting_flow_and_duplicate_prevention(client):
    """TC-04, TC-05, TC-06: Verify eligibility check, anonymous ballot recording, and duplicate block."""
    # 1. Voter requests participation in Election 1
    login(client, "voter1", "voter123")
    client.post("/election/1/request", follow_redirects=True)

    # 2. Attempt to vote while still PENDING (TC-04)
    res_pending = client.post("/election/1/vote", data={"candidate_id": 1}, follow_redirects=True)
    assert b"You are not approved to vote" in res_pending.data

    # 3. Admin approves voter1
    login(client, "admin", "admin123")
    client.post("/admin/election/1/voter/2/status", data={"status": "APPROVED"}, follow_redirects=True)

    # 4. Approved voter casts valid vote (TC-05)
    login(client, "voter1", "voter123")
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
    client.get("/logout")
    assert client.get("/").headers["Location"] == "/login"


def test_admin_election_candidate_actions_and_template_escaping(client):
    from datetime import datetime, timedelta

    login(client, "admin", "admin123")
    start = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    end = (datetime.now() + timedelta(days=11)).strftime("%Y-%m-%dT%H:%M")
    response = client.post("/admin/election/create", data={
        "title": "<script>alert(1)</script>", "description": "Example", "start_time": start, "end_time": end,
    }, follow_redirects=True)
    assert b"Election created successfully" in response.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
    assert b"<script>alert(1)</script>" not in response.data
    assert b'action="/admin/election/create"' in response.data
    assert b'href="/election/2"' in response.data

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
    login(client, "admin", "admin123")
    response = client.post("/admin/election/1/candidate/add", data={
        "name": "Late candidate", "manifesto": "Example",
    }, follow_redirects=True)
    assert b"Candidates can only be added before an election begins" in response.data
    response = client.post("/admin/election/1/candidate/1/delete", follow_redirects=True)
    assert b"Candidates cannot be removed after an election starts" in response.data
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM candidates WHERE election_id = 1").fetchone()[0] == 2


def test_results_are_only_published_after_election_closes(client):
    login(client, "voter1", "voter123")
    assert b"Official Published Results" not in client.get("/election/1").data
    with client.application.app_context():
        db = get_db()
        db.execute("UPDATE elections SET start_time = ?, end_time = ? WHERE id = 1", ("2000-01-01T00:00", "2000-01-02T00:00"))
        db.execute("INSERT INTO ballots (ballot_id, election_id, candidate_id) VALUES ('test-ballot', 1, 1)")
        db.commit()
    response = client.get("/election/1")
    assert b"Official Published Results (Total Votes: 1)" in response.data
    assert b"100.0%" in response.data


def test_ballot_failure_rolls_back_voter_participation(client):
    with client.application.app_context():
        db = get_db()
        db.execute("INSERT INTO voter_participation (election_id, voter_id, status) VALUES (1, 2, 'APPROVED')")
        db.execute("""
            CREATE TRIGGER fail_ballot_insert BEFORE INSERT ON ballots
            BEGIN SELECT RAISE(ABORT, 'Simulated ballot write failure'); END
        """)
        db.commit()
    login(client, "voter1", "voter123")
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


def test_admin_can_close_election_early_and_publish_results(client):
    login(client, "voter1", "voter123")
    response = client.get("/election/1")
    assert b'action="/election/1/request"' in response.data
    assert b"End Election Early" not in response.data
    client.post("/election/1/request")
    login(client, "admin", "admin123")
    response = client.get("/election/1")
    assert b'action="/admin/election/1/close"' in response.data
    assert b'action="/admin/election/1/voter/2/status"' in response.data
    client.post("/admin/election/1/voter/2/status", data={"status": "APPROVED"})
    login(client, "voter1", "voter123")
    assert b'action="/election/1/vote"' in client.get("/election/1").data
    client.post("/election/1/vote", data={"candidate_id": 1})
    login(client, "admin", "admin123")
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Election has been closed early" in response.data
    assert b"Official Published Results (Total Votes: 1)" in response.data
    assert b"End Election Early" not in response.data
    with client.application.app_context():
        log = get_db().execute("SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "Closed Election ID 1" in log
    login(client, "voter1", "voter123")
    response = client.post("/election/1/vote", data={"candidate_id": 2}, follow_redirects=True)
    assert b"This election is not currently active" in response.data


def test_voter_cannot_close_election(client):
    login(client, "voter1", "voter123")
    response = client.post("/admin/election/1/close", follow_redirects=True)
    assert b"Access Denied: Administrator privileges required" in response.data
    assert b"Official Published Results" not in client.get("/election/1").data


@pytest.mark.parametrize("election_id,start_time,end_time,expected", [
    (1, "2100-01-01T00:00", "2100-01-02T00:00", b"Only ACTIVE elections can be closed early"),
    (1, "2000-01-01T00:00", "2000-01-02T00:00", b"Only ACTIVE elections can be closed early"),
    (999, None, None, b"Election not found"),
])
def test_close_election_rejects_inactive_or_missing_election(client, election_id, start_time, end_time, expected):
    login(client, "admin", "admin123")
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
