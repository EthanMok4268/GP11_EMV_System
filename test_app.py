import os
import tempfile
import pytest
from app import app, init_db, get_db

@pytest.fixture
def client():
    db_fd, db_path = tempfile.mkstemp()
    import app as election_app
    election_app.DATABASE = db_path
    app.config["TESTING"] = True

    with app.test_client() as client:
        init_db()
        yield client

    os.close(db_fd)
    os.unlink(db_path)

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
    with app.app_context():
        db = get_db()
        count = db.execute("SELECT COUNT(*) as c FROM ballots WHERE election_id = 1").fetchone()["c"]
        assert count == 1