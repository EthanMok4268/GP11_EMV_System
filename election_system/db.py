"""SQLite schema, connections, and queries."""
import sqlite3
from datetime import datetime

from flask import current_app, g


# -------------------------------------------------------------------------
# DATABASE LAYER & SCHEMA INITIALIZATION
# -------------------------------------------------------------------------
_BALLOT_COLUMNS = """
    ballot_id TEXT PRIMARY KEY,
    election_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
    FOREIGN KEY (election_id, candidate_id) REFERENCES candidates(election_id, id) ON DELETE CASCADE
"""

SCHEMA = f"""
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
    end_time TEXT NOT NULL,
    is_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(is_confirmed IN (0, 1)),
    closed_early_at TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    election_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    manifesto TEXT,
    FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS candidates_election_id_id ON candidates(election_id, id);

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
CREATE TABLE IF NOT EXISTS ballots ({_BALLOT_COLUMNS});

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
    with db:
        # executescript commits any earlier transaction, so begin inside the script.
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        migrate_ballot_candidate_fk(db)
        migrate_election_confirmation(db)
        if "closed_early_at" not in {row["name"] for row in db.execute("PRAGMA table_info(elections)")}:
            db.execute("ALTER TABLE elections ADD COLUMN closed_early_at TEXT")


def migrate_election_confirmation(db):
    """Upgrade legacy elections without reopening history inside the initialization transaction."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(elections)")}
    if "is_confirmed" in columns:
        return
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    confirmations = []
    blocked = []
    for election in db.execute("SELECT * FROM elections"):
        candidates = db.execute(
            "SELECT name, manifesto FROM candidates WHERE election_id = ?", (election["id"],)
        ).fetchall()
        complete = len(candidates) >= 2 and all(
            (candidate["name"] or "").strip() and (candidate["manifesto"] or "").strip()
            for candidate in candidates
        )
        closed = election["end_time"] <= now
        has_ballots = db.execute(
            "SELECT 1 FROM ballots WHERE election_id = ? LIMIT 1", (election["id"],)
        ).fetchone() is not None
        if not closed and not complete and has_ballots:
            blocked.append(str(election["id"]))
        confirmations.append((int(closed or complete), election["id"]))
    if blocked:
        raise RuntimeError(
            "Election upgrade blocked. Close these elections using the previous application "
            "before upgrading: " + ", ".join(blocked)
            + ". They have accepted ballots but do not meet the two-candidate readiness rules."
        )
    db.execute(
        "ALTER TABLE elections ADD COLUMN is_confirmed INTEGER NOT NULL DEFAULT 0 "
        "CHECK(is_confirmed IN (0, 1))"
    )
    db.executemany("UPDATE elections SET is_confirmed = ? WHERE id = ?", confirmations)


def migrate_ballot_candidate_fk(db):
    """Enforce candidate ownership without guessing which election a legacy ballot belongs to."""
    if any(
        row["table"] == "candidates" and row["seq"] == 1 and row["from"] == "candidate_id"
        for row in db.execute("PRAGMA foreign_key_list(ballots)")
    ):
        return

    invalid_elections = db.execute("""
        SELECT b.election_id, COUNT(*) AS ballot_count FROM ballots b
        LEFT JOIN candidates c ON c.election_id = b.election_id AND c.id = b.candidate_id
        LEFT JOIN elections e ON e.id = b.election_id
        WHERE c.id IS NULL OR e.id IS NULL
        GROUP BY b.election_id
        ORDER BY b.election_id
    """).fetchall()
    if invalid_elections:
        ids = ", ".join(str(row["election_id"]) for row in invalid_elections)
        count = sum(row["ballot_count"] for row in invalid_elections)
        raise RuntimeError(
            f"Election upgrade blocked. Invalid ballot references: {count}. Affected election IDs: {ids}. "
            "Review the affected records before upgrading."
        )

    db.execute(f"CREATE TABLE ballots_with_candidate_fk ({_BALLOT_COLUMNS})")
    db.execute("""
        INSERT INTO ballots_with_candidate_fk (ballot_id, election_id, candidate_id)
        SELECT ballot_id, election_id, candidate_id FROM ballots
    """)
    db.execute("DROP TABLE ballots")
    db.execute("ALTER TABLE ballots_with_candidate_fk RENAME TO ballots")


def init_app(app):
    app.teardown_appcontext(close_db)


# -------------------------------------------------------------------------
# USER ACCOUNT QUERIES
# -------------------------------------------------------------------------
def find_user(username):
    return get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def find_user_by_id(user_id):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_voter(username, password_hash):
    get_db().execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'VOTER')",
        (username, password_hash),
    )


def list_administrators():
    return get_db().execute("SELECT id, username FROM users WHERE role = 'ADMIN' ORDER BY username").fetchall()


def create_first_administrator(username, password_hash):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users WHERE role = 'ADMIN' LIMIT 1").fetchone():
            raise ValueError("An administrator already exists. Run the command again to view existing accounts.")
        if find_user(username) is not None:
            raise ValueError("That username is already registered. Choose a different username.")
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'ADMIN')",
            (username, password_hash),
        )


def update_administrator_credentials(admin_id, previous_username, username, password_hash):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        admin = find_user_by_id(admin_id)
        if admin is None or admin["role"] != "ADMIN" or admin["username"] != previous_username:
            raise ValueError("That administrator account changed. Run the command again.")
        if username != previous_username and find_user(username) is not None:
            raise ValueError("That username is already registered. Choose a different username.")
        db.execute("UPDATE users SET username = ?, password_hash = ? WHERE id = ?", (username, password_hash, admin_id))


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
        "SELECT * FROM candidates WHERE id = ? AND election_id = ?", (candidate_id, election_id)
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
        LEFT JOIN ballots b ON c.election_id = b.election_id AND c.id = b.candidate_id
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
    return list_audit_logs(limit=15)


def list_audit_logs(*, before_id=None, limit=21):
    query = """
        SELECT a.*, u.username FROM audit_logs a
        JOIN users u ON a.admin_id = u.id
    """
    parameters = []
    if before_id is not None:
        query += " WHERE a.id < ?"
        parameters.append(before_id)
    query += " ORDER BY a.id DESC LIMIT ?"
    parameters.append(limit)
    return get_db().execute(query, parameters).fetchall()


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


def delete_election(election_id):
    get_db().execute("DELETE FROM elections WHERE id = ?", (election_id,))


def add_candidate(election_id, name, manifesto):
    cursor = get_db().execute(
        "INSERT INTO candidates (election_id, name, manifesto) VALUES (?, ?, ?)", (election_id, name, manifesto)
    )
    return cursor.lastrowid


def update_candidate(election_id, candidate_id, name, manifesto):
    get_db().execute(
        "UPDATE candidates SET name = ?, manifesto = ? WHERE election_id = ? AND id = ?",
        (name, manifesto, election_id, candidate_id),
    )


def delete_candidate(election_id, candidate_id):
    get_db().execute("DELETE FROM candidates WHERE id = ? AND election_id = ?", (candidate_id, election_id))


def set_election_confirmation(election_id, is_confirmed):
    get_db().execute(
        "UPDATE elections SET is_confirmed = ? WHERE id = ?", (int(is_confirmed), election_id)
    )


# -------------------------------------------------------------------------
# UPDATE VOTER ELIGIBILITY STATUS
# -------------------------------------------------------------------------
def update_voter_status(election_id, voter_id, status):
    return get_db().execute(
        """
        UPDATE voter_participation SET status = ?
        WHERE election_id = ? AND voter_id = ? AND status <> ? AND has_voted = 0
        """,
        (status, election_id, voter_id, status),
    ).rowcount


# -------------------------------------------------------------------------
# RECORD ACTUAL EARLY ELECTION CLOSURE
# -------------------------------------------------------------------------
def mark_election_closed_early(election_id, closed_early_at):
    get_db().execute("UPDATE elections SET closed_early_at = ? WHERE id = ?", (closed_early_at, election_id))
