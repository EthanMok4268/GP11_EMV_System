"""SQLite schema, connections, demo data, and queries."""
import sqlite3
from datetime import datetime, timedelta

from flask import current_app, g
from werkzeug.security import generate_password_hash


# -------------------------------------------------------------------------
# DATABASE LAYER & SCHEMA INITIALIZATION
# -------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('ADMIN', 'VOTER'))
);

CREATE TABLE IF NOT EXISTS elections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    election_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    manifesto TEXT,
    FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE
);

-- Stores eligibility and participation status (NO candidate choice stored here)
CREATE TABLE IF NOT EXISTS voter_participation (
    election_id INTEGER NOT NULL,
    voter_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED')),
    has_voted INTEGER NOT NULL DEFAULT 0 CHECK(has_voted IN (0, 1)),
    PRIMARY KEY (election_id, voter_id),
    FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
    FOREIGN KEY (voter_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Stores anonymous ballots (NO voter_id or timestamp stored here to protect privacy)
CREATE TABLE IF NOT EXISTS ballots (
    ballot_id TEXT PRIMARY KEY,
    election_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (admin_id) REFERENCES users(id)
);
"""


# -------------------------------------------------------------------------
# DATABASE CONNECTION & CLEANUP
# -------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# -------------------------------------------------------------------------
# INITIALIZE DATABASE & REGISTER CLEANUP
# -------------------------------------------------------------------------
def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    seed_demo_data(db)


def init_app(app):
    app.teardown_appcontext(close_db)


# -------------------------------------------------------------------------
# SEED DEFAULT USERS & SAMPLE ELECTION
# -------------------------------------------------------------------------
def seed_demo_data(db):
    # Seed default Admin, Voter, and Sample Election if database is empty
    cur = db.execute("SELECT COUNT(*) as cnt FROM users")
    if cur.fetchone()["cnt"] == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "ADMIN")
        )
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("voter1", generate_password_hash("voter123"), "VOTER")
        )
        now = datetime.now()
        start_active = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        end_active = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        db.execute(
            "INSERT INTO elections (title, description, start_time, end_time) VALUES (?, ?, ?, ?)",
            ("Student Union Presidential Election", "Annual vote for the Student Union President.", start_active,
             end_active)
        )
        db.execute(
            "INSERT INTO candidates (election_id, name, manifesto) VALUES (1, 'Alice Wong', '24/7 library access and lower campus cafe prices.')")
        db.execute(
            "INSERT INTO candidates (election_id, name, manifesto) VALUES (1, 'Bob Chan', 'More funding for sports clubs and student societies.')")
        db.commit()


# -------------------------------------------------------------------------
# USER ACCOUNT QUERIES
# -------------------------------------------------------------------------
def find_user(username):
    return get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_voter(username, password_hash):
    get_db().execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'VOTER')",
        (username, password_hash),
    )


# -------------------------------------------------------------------------
# ELECTION & CANDIDATE QUERIES
# -------------------------------------------------------------------------
def list_elections():
    return get_db().execute("SELECT * FROM elections ORDER BY start_time DESC").fetchall()


def find_election(election_id):
    return get_db().execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()


def list_candidates(election_id):
    return get_db().execute("SELECT * FROM candidates WHERE election_id = ?", (election_id,)).fetchall()


def find_candidate(election_id, candidate_id):
    return get_db().execute(
        "SELECT id FROM candidates WHERE id = ? AND election_id = ?", (candidate_id, election_id)
    ).fetchone()


# -------------------------------------------------------------------------
# VOTER PARTICIPATION & BALLOT QUERIES
# -------------------------------------------------------------------------
def find_participation(election_id, voter_id):
    return get_db().execute(
        "SELECT * FROM voter_participation WHERE election_id = ? AND voter_id = ?", (election_id, voter_id)
    ).fetchone()


def create_participation(election_id, voter_id):
    get_db().execute(
        "INSERT INTO voter_participation (election_id, voter_id, status, has_voted) VALUES (?, ?, 'PENDING', 0)",
        (election_id, voter_id),
    )


def mark_voted(election_id, voter_id):
    return get_db().execute(
        "UPDATE voter_participation SET has_voted = 1 WHERE election_id = ? AND voter_id = ? AND has_voted = 0",
        (election_id, voter_id),
    ).rowcount


def insert_ballot(ballot_id, election_id, candidate_id):
    get_db().execute(
        "INSERT INTO ballots (ballot_id, election_id, candidate_id) VALUES (?, ?, ?)",
        (ballot_id, election_id, candidate_id),
    )


# -------------------------------------------------------------------------
# ELECTION RESULTS & VOTER REQUESTS
# -------------------------------------------------------------------------
def list_results(election_id):
    return get_db().execute("""
        SELECT c.name, c.manifesto, COUNT(b.ballot_id) as votes
        FROM candidates c
        LEFT JOIN ballots b ON c.id = b.candidate_id
        WHERE c.election_id = ?
        GROUP BY c.id
        ORDER BY votes DESC
    """, (election_id,)).fetchall()


def list_voter_requests(election_id):
    return get_db().execute("""
        SELECT vp.*, u.username FROM voter_participation vp
        JOIN users u ON vp.voter_id = u.id
        WHERE vp.election_id = ?
    """, (election_id,)).fetchall()


# -------------------------------------------------------------------------
# ADMINISTRATIVE AUDIT LOG QUERIES
# -------------------------------------------------------------------------
def list_recent_audit_logs():
    return get_db().execute("""
        SELECT a.*, u.username FROM audit_logs a
        JOIN users u ON a.admin_id = u.id
        ORDER BY a.id DESC LIMIT 15
    """).fetchall()


def log_audit(admin_id, action):
    get_db().execute(
        "INSERT INTO audit_logs (admin_id, action, timestamp) VALUES (?, ?, ?)",
        (admin_id, action, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


# -------------------------------------------------------------------------
# ELECTION & CANDIDATE MANAGEMENT QUERIES
# -------------------------------------------------------------------------
def create_election(title, description, start_time, end_time):
    cursor = get_db().execute(
        "INSERT INTO elections (title, description, start_time, end_time) VALUES (?, ?, ?, ?)",
        (title, description, start_time, end_time),
    )
    return cursor.lastrowid


def update_election(election_id, title, description, start_time, end_time):
    get_db().execute(
        "UPDATE elections SET title = ?, description = ?, start_time = ?, end_time = ? WHERE id = ?",
        (title, description, start_time, end_time, election_id),
    )


def add_candidate(election_id, name, manifesto):
    get_db().execute(
        "INSERT INTO candidates (election_id, name, manifesto) VALUES (?, ?, ?)", (election_id, name, manifesto)
    )


def delete_candidate(election_id, candidate_id):
    get_db().execute("DELETE FROM candidates WHERE id = ? AND election_id = ?", (candidate_id, election_id))


# -------------------------------------------------------------------------
# UPDATE VOTER ELIGIBILITY STATUS
# -------------------------------------------------------------------------
def update_voter_status(election_id, voter_id, status):
    get_db().execute(
        "UPDATE voter_participation SET status = ? WHERE election_id = ? AND voter_id = ?",
        (status, election_id, voter_id),
    )


# -------------------------------------------------------------------------
# UPDATE ELECTION CLOSING TIME
# -------------------------------------------------------------------------
def update_election_end_time(election_id, end_time):
    get_db().execute("UPDATE elections SET end_time = ? WHERE id = ?", (end_time, election_id))
