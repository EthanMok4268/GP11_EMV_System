"""Concurrent requests from one voter must accept exactly one ballot."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest

from election_system import db as database

from .helpers import VOTER_PASSWORD


@pytest.mark.parametrize("second_candidate_id", [1, 2], ids=["same-choice", "different-choice"])
def test_simultaneous_votes_from_same_voter_accept_one_ballot(client, monkeypatch, second_candidate_id):
    app = client.application
    with app.app_context(), database.get_db():
        database.create_participation(1, 2)
        database.update_voter_status(1, 2, "APPROVED")

    real_connect = sqlite3.connect
    blocker = real_connect(app.config["DATABASE"])
    transaction_boundary = Barrier(3)
    vote_connections = [None, None]
    request_slot = local()

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        slot = getattr(request_slot, "index", None)
        if slot is not None:
            def trace(sql):
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    vote_connections[slot] = connection
                    # Authenticate both clients before either request starts
                    # waiting for SQLite's shorter busy timeout.
                    transaction_boundary.wait(timeout=15)

            connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def submit(slot, candidate_id):
        request_slot.index = slot
        # Each thread owns its client, login session, CSRF token, and connection.
        with app.test_client() as voter:
            response = voter.post(
                "/login", data={"username": "voter1", "password": VOTER_PASSWORD},
                base_url="https://localhost", follow_redirects=True,
            )
            assert b"Welcome back, voter1!" in response.data
            return voter.post(
                "/election/1/vote", data={"candidate_id": candidate_id},
                base_url="https://localhost", follow_redirects=True,
            )

    try:
        # Hold a real SQLite write lock until both vote requests reach their
        # transaction boundary. Neither can record a ballot before release.
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit, slot, candidate_id)
                       for slot, candidate_id in enumerate((1, second_candidate_id))]
            synchronized = False
            try:
                transaction_boundary.wait(timeout=15)
                synchronized = True
                assert vote_connections[0] is not vote_connections[1]
                assert all(not future.done() for future in futures)
                assert blocker.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 0
                blocker.commit()
            finally:
                # Also release the lock after an assertion failure so workers
                # can finish and executor cleanup cannot wait on this lock.
                if not synchronized:
                    transaction_boundary.abort()
                blocker.rollback()
            responses = [future.result(timeout=5) for future in futures]
    finally:
        blocker.close()

    assert all(response.status_code == 200 for response in responses)
    successes = [b"Vote successfully recorded!" in response.data for response in responses]
    duplicates = [b"Duplicate voting prevented" in response.data for response in responses]
    assert sum(successes) == 1
    assert sum(duplicates) == 1
    assert all(success != duplicate for success, duplicate in zip(successes, duplicates))
    accepted_candidate_id = (1, second_candidate_id)[successes.index(True)]

    with app.app_context():
        ballots = database.get_db().execute("SELECT election_id, candidate_id FROM ballots").fetchall()
        assert [tuple(ballot) for ballot in ballots] == [(1, accepted_candidate_id)]
        participation = database.find_participation(1, 2)
        assert participation["status"] == "APPROVED"
        assert participation["has_voted"] == 1
        results = {row["name"]: row["votes"] for row in database.list_results(1)}
        assert results == {
            "Alice Wong": int(accepted_candidate_id == 1),
            "Bob Chan": int(accepted_candidate_id == 2),
        }
