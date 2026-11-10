"""Database constraints and legacy upgrades for anonymous ballots."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from election_system import create_app, db as database


def legacy_ballot_app(tmp_path, *, confirmed=True):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "legacy.db")})
    schema = database.SCHEMA.replace(",\n    closed_early_at TEXT", "")
    if not confirmed:
        schema = schema.replace(",\n    is_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(is_confirmed IN (0, 1))", "")
    with app.app_context():
        db = database.get_db()
        db.executescript(schema)
        db.executescript("""
            DROP TABLE ballots;
            DROP INDEX candidates_election_id_id;
            CREATE TABLE ballots (
                ballot_id TEXT PRIMARY KEY,
                election_id INTEGER NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE
            );
            INSERT INTO users VALUES (1, 'legacy_admin', 'hash', 'ADMIN');
            INSERT INTO elections (id, title, start_time, end_time) VALUES
                (1, 'Historical election', '2000-01-01T00:00', '2000-01-02T00:00'),
                (2, 'Other election', '2000-01-01T00:00', '2999-01-02T00:00');
            INSERT INTO candidates VALUES (1, 1, 'First candidate', 'A'), (2, 2, 'Other candidate', 'B');
            INSERT INTO ballots VALUES ('historical-ballot', 1, 1);
        """)
    return app


def test_ballot_and_candidate_changes_enforce_election_membership(client):
    with client.application.app_context():
        db = database.get_db()
        with db:
            election_id = db.execute("""
                INSERT INTO elections (title, start_time, end_time)
                VALUES ('Other election', '2000-01-01T00:00', '2000-01-02T00:00')
            """).lastrowid
            candidate_id = database.add_candidate(election_id, "Other candidate", "B")
            database.insert_ballot("valid-ballot", 1, 1)

            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                database.insert_ballot("mismatched-ballot", 1, candidate_id)
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute("UPDATE ballots SET candidate_id = ? WHERE ballot_id = 'valid-ballot'", (candidate_id,))
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute("UPDATE ballots SET election_id = ? WHERE ballot_id = 'valid-ballot'", (election_id,))
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                db.execute("UPDATE candidates SET election_id = ? WHERE id = 1", (election_id,))

        assert [row["votes"] for row in database.list_results(1)] == [1, 0]
        assert [row["votes"] for row in database.list_results(election_id)] == [0]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ballot_upgrade_preserves_records_and_only_runs_once(tmp_path):
    app = legacy_ballot_app(tmp_path)
    with app.app_context():
        db = database.get_db()
        database.init_db()

        assert [tuple(row) for row in db.execute("SELECT * FROM ballots")] == [("historical-ballot", 1, 1)]
        election = database.find_election(1)
        assert election["start_time"] == "2000-01-01T00:00"
        assert election["end_time"] == "2000-01-02T00:00"
        assert election["closed_early_at"] is None
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db:
            database.insert_ballot("mismatched-ballot", 1, 2)

        before = list(db.iterdump())
        database.init_db()
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ballot_upgrade_reports_corruption_without_changing_schema_or_records(tmp_path):
    app = legacy_ballot_app(tmp_path, confirmed=False)
    with app.app_context():
        db = database.get_db()
        db.execute("PRAGMA foreign_keys = OFF")
        with db:
            db.execute("""
                INSERT INTO ballots VALUES
                    ('wrong-candidate-election', 2, 1), ('missing-candidate', 2, 99), ('missing-election', 99, 1)
            """)
        db.execute("PRAGMA foreign_keys = ON")

        # The tally itself only counts ballots whose election matches the candidate's election.
        assert [row["votes"] for row in database.list_results(1)] == [1]
        assert [row["votes"] for row in database.list_results(2)] == [0]

        before = list(db.iterdump())
        with pytest.raises(RuntimeError, match=r"Invalid ballot references: 3\. Affected election IDs: 2, 99\."):
            database.init_db()
        assert list(db.iterdump()) == before


def test_concurrent_first_startup_initializes_without_demo_data(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "new.db")})
    ready = Barrier(2)

    def start(_):
        with app.app_context():
            ready.wait(timeout=5)
            database.init_db()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(start, range(2)))
    with app.app_context():
        db = database.get_db()
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM elections").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
