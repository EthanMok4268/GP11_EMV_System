import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

from election_system.db import get_db

ADMIN_PASSWORD = "AdminFixture8"
VOTER_PASSWORD = "VoterFixture8"


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def election_management_snapshot(client):
    with client.application.app_context():
        return list(get_db().iterdump())


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
            if role == "ADMIN":
                login(requester, "admin", ADMIN_PASSWORD)
            else:
                login(requester, "voter1", VOTER_PASSWORD)
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
