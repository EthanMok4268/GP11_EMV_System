import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, request, session, redirect, url_for,
    render_template_string, flash, g
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
DATABASE = "election_system.db"


# -------------------------------------------------------------------------
# DATABASE LAYER & SCHEMA INITIALIZATION
# -------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
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
        """)

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


def log_audit(admin_id: int, action: str):
    db = get_db()
    db.execute(
        "INSERT INTO audit_logs (admin_id, action, timestamp) VALUES (?, ?, ?)",
        (admin_id, action, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )


def get_election_status(start_str: str, end_str: str) -> str:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if now < start_str:
        return "UPCOMING"
    elif start_str <= now <= end_str:
        return "ACTIVE"
    return "CLOSED"


# -------------------------------------------------------------------------
# ACCESS CONTROL MIDDLEWARE (RBAC)
# -------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "ADMIN":
            flash("Access Denied: Administrator privileges required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)

    return decorated


# -------------------------------------------------------------------------
# ROUTES: AUTHENTICATION
# -------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_page(LOGIN_HTML)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(password) < 6:
            flash("Username required and password must be at least 6 characters.", "danger")
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'VOTER')",
                (username, generate_password_hash(password))
            )
            db.commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already registered. Duplicate registrations are prevented.", "danger")
    return render_page(REGISTER_HTML)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# -------------------------------------------------------------------------
# ROUTES: DASHBOARD & ELECTION VIEWING
# -------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    rows = db.execute("SELECT * FROM elections ORDER BY start_time DESC").fetchall()
    elections = []
    for r in rows:
        status = get_election_status(r["start_time"], r["end_time"])
        part = db.execute(
            "SELECT * FROM voter_participation WHERE election_id = ? AND voter_id = ?",
            (r["id"], session["user_id"])
        ).fetchone()
        elections.append({"row": r, "status": status, "part": part})

    audit_logs = []
    if session.get("role") == "ADMIN":
        audit_logs = db.execute("""
            SELECT a.*, u.username FROM audit_logs a 
            JOIN users u ON a.admin_id = u.id 
            ORDER BY a.id DESC LIMIT 15
        """).fetchall()

    return render_page(DASHBOARD_HTML, elections=elections, audit_logs=audit_logs)


@app.route("/election/<int:election_id>")
@login_required
def view_election(election_id):
    db = get_db()
    election = db.execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()
    if not election:
        flash("Election not found.", "danger")
        return redirect(url_for("dashboard"))

    status = get_election_status(election["start_time"], election["end_time"])
    candidates = db.execute("SELECT * FROM candidates WHERE election_id = ?", (election_id,)).fetchall()
    participation = db.execute(
        "SELECT * FROM voter_participation WHERE election_id = ? AND voter_id = ?",
        (election_id, session["user_id"])
    ).fetchone()

    # Calculate results ONLY if election is CLOSED
    results = []
    total_votes = 0
    if status == "CLOSED":
        results = db.execute("""
            SELECT c.name, c.manifesto, COUNT(b.ballot_id) as votes
            FROM candidates c
            LEFT JOIN ballots b ON c.id = b.candidate_id
            WHERE c.election_id = ?
            GROUP BY c.id
            ORDER BY votes DESC
        """, (election_id,)).fetchall()
        total_votes = sum(r["votes"] for r in results)

    # Admin view: load voter eligibility requests
    voter_requests = []
    if session.get("role") == "ADMIN":
        voter_requests = db.execute("""
            SELECT vp.*, u.username FROM voter_participation vp
            JOIN users u ON vp.voter_id = u.id
            WHERE vp.election_id = ?
        """, (election_id,)).fetchall()

    return render_page(
        ELECTION_DETAIL_HTML,
        election=election,
        status=status,
        candidates=candidates,
        participation=participation,
        results=results,
        total_votes=total_votes,
        voter_requests=voter_requests
    )


# -------------------------------------------------------------------------
# ROUTES: VOTER ACTIONS (REQUEST ELIGIBILITY & ATOMIC VOTING)
# -------------------------------------------------------------------------
@app.route("/election/<int:election_id>/request", methods=["POST"])
@login_required
def request_participation(election_id):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO voter_participation (election_id, voter_id, status, has_voted) VALUES (?, ?, 'PENDING', 0)",
            (election_id, session["user_id"])
        )
        db.commit()
        flash("Participation request submitted. Waiting for administrator approval.", "success")
    except sqlite3.IntegrityError:
        flash("You have already submitted a request for this election.", "warning")
    return redirect(url_for("view_election", election_id=election_id))


@app.route("/election/<int:election_id>/vote", methods=["POST"])
@login_required
def cast_vote(election_id):
    if session.get("role") != "VOTER":
        flash("Only voters can cast ballots.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    candidate_id = request.form.get("candidate_id")
    db = get_db()
    election = db.execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()

    # Step 2 Check: Election is ACTIVE
    if not election or get_election_status(election["start_time"], election["end_time"]) != "ACTIVE":
        flash("Voting rejected: This election is not currently active.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    # Step 3 & 4 Checks: Voter is APPROVED and has NOT voted
    part = db.execute(
        "SELECT * FROM voter_participation WHERE election_id = ? AND voter_id = ?",
        (election_id, session["user_id"])
    ).fetchone()

    if not part or part["status"] != "APPROVED":
        flash("Voting rejected: You are not approved to vote in this election.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    if part["has_voted"] == 1:
        flash("Duplicate voting prevented: You have already cast a ballot in this election.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    # Verify candidate belongs to this election
    cand = db.execute(
        "SELECT id FROM candidates WHERE id = ? AND election_id = ?",
        (candidate_id, election_id)
    ).fetchone()
    if not cand:
        flash("Invalid candidate selection.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    # Step 6: Atomic Database Transaction (Ballot + Participation Status Update)
    try:
        with db:
            cursor = db.execute(
                "UPDATE voter_participation SET has_voted = 1 WHERE election_id = ? AND voter_id = ? AND has_voted = 0",
                (election_id, session["user_id"])
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Concurrent duplicate vote attempt blocked.")

            anonymous_ballot_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO ballots (ballot_id, election_id, candidate_id) VALUES (?, ?, ?)",
                (anonymous_ballot_id, election_id, candidate_id)
            )
        # Step 7: Confirmation Message
        flash(f"Vote successfully recorded! Your anonymous receipt ID is: {anonymous_ballot_id[:8]}...", "success")
    except Exception:
        flash("An unexpected error occurred. Transaction rolled back to preserve data integrity.", "danger")

    return redirect(url_for("view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: ADMINISTRATOR ACTIONS
# -------------------------------------------------------------------------
@app.route("/admin/election/create", methods=["POST"])
@login_required
@admin_required
def create_election():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")

    if not title or not start_time or not end_time or start_time >= end_time:
        flash("Invalid election parameters. Ensure start time is before end time.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO elections (title, description, start_time, end_time) VALUES (?, ?, ?, ?)",
            (title, description, start_time, end_time)
        )
        log_audit(session["user_id"], f"Created election '{title}' (ID: {cur.lastrowid})")
    flash("Election created successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/election/<int:election_id>/candidate/add", methods=["POST"])
@login_required
@admin_required
def add_candidate(election_id):
    db = get_db()
    election = db.execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()
    if get_election_status(election["start_time"], election["end_time"]) != "UPCOMING":
        flash("Candidate Constraint: Candidates can only be added before an election begins (UPCOMING status).",
              "danger")
        return redirect(url_for("view_election", election_id=election_id))

    name = request.form.get("name", "").strip()
    manifesto = request.form.get("manifesto", "").strip()
    if name:
        with db:
            db.execute("INSERT INTO candidates (election_id, name, manifesto) VALUES (?, ?, ?)",
                       (election_id, name, manifesto))
            log_audit(session["user_id"], f"Added candidate '{name}' to Election ID {election_id}")
        flash(f"Candidate '{name}' added.", "success")
    return redirect(url_for("view_election", election_id=election_id))


@app.route("/admin/election/<int:election_id>/candidate/<int:cand_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_candidate(election_id, cand_id):
    db = get_db()
    election = db.execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()
    if get_election_status(election["start_time"], election["end_time"]) != "UPCOMING":
        flash("Candidate Constraint: Candidates cannot be removed after an election starts.", "danger")
        return redirect(url_for("view_election", election_id=election_id))

    with db:
        db.execute("DELETE FROM candidates WHERE id = ? AND election_id = ?", (cand_id, election_id))
        log_audit(session["user_id"], f"Removed candidate ID {cand_id} from Election ID {election_id}")
    flash("Candidate removed.", "info")
    return redirect(url_for("view_election", election_id=election_id))


@app.route("/admin/election/<int:election_id>/voter/<int:voter_id>/status", methods=["POST"])
@login_required
@admin_required
def update_voter_status(election_id, voter_id):
    new_status = request.form.get("status")
    if new_status in ("APPROVED", "REJECTED"):
        db = get_db()
        with db:
            db.execute(
                "UPDATE voter_participation SET status = ? WHERE election_id = ? AND voter_id = ?",
                (new_status, election_id, voter_id)
            )
            log_audit(session["user_id"],
                      f"Set Voter ID {voter_id} eligibility to {new_status} for Election ID {election_id}")
        flash(f"Voter eligibility updated to {new_status}.", "success")
    return redirect(url_for("view_election", election_id=election_id))


# -------------------------------------------------------------------------
# FRONTEND TEMPLATES (BOOTSTRAP 5 UI)
# -------------------------------------------------------------------------
BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Election Management & Voting System</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
    <div class="container">
        <a class="navbar-brand fw-bold" href="{{ url_for('dashboard') }}">🗳️ E-Vote System</a>
        {% if session.get('user_id') %}
        <div class="d-flex align-items-center text-white">
            <span class="me-3">User: <strong>{{ session['username'] }}</strong> <span class="badge bg-secondary">{{ session['role'] }}</span></span>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm">Logout</a>
        </div>
        {% endif %}
    </div>
</nav>
<div class="container pb-5">
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }} shadow-sm">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    {{ content | safe }}
</div>
</body>
</html>
"""

LOGIN_HTML = """
<div class="row justify-content-center mt-5">
    <div class="col-md-5">
        <div class="card shadow-sm">
            <div class="card-body p-4">
                <h4 class="card-title mb-3">Sign In</h4>
                <form method="POST">
                    <div class="mb-3"><label class="form-label">Username</label><input type="text" name="username" class="form-control" required></div>
                    <div class="mb-3"><label class="form-label">Password</label><input type="password" name="password" class="form-control" required></div>
                    <button class="btn btn-primary w-100">Login</button>
                </form>
                <hr>
                <p class="mb-1 text-muted small"><strong>Demo Accounts:</strong><br>Admin: <code>admin</code> / <code>admin123</code><br>Voter: <code>voter1</code> / <code>voter123</code></p>
                <a href="{{ url_for('register') }}" class="d-block text-center mt-3">Register a new voter account</a>
            </div>
        </div>
    </div>
</div>
"""

REGISTER_HTML = """
<div class="row justify-content-center mt-5">
    <div class="col-md-5">
        <div class="card shadow-sm">
            <div class="card-body p-4">
                <h4 class="card-title mb-3">Voter Registration</h4>
                <form method="POST">
                    <div class="mb-3"><label class="form-label">Username</label><input type="text" name="username" class="form-control" required></div>
                    <div class="mb-3"><label class="form-label">Password (min 6 chars)</label><input type="password" name="password" class="form-control" required></div>
                    <button class="btn btn-success w-100">Register Voter Account</button>
                </form>
                <a href="{{ url_for('login') }}" class="d-block text-center mt-3">Back to Login</a>
            </div>
        </div>
    </div>
</div>
"""

DASHBOARD_HTML = """
<div class="row">
    <div class="col-lg-8">
        <h3 class="mb-3">Elections</h3>
        {% for item in elections %}
        <div class="card mb-3 shadow-sm">
            <div class="card-body d-flex justify-content-between align-items-center">
                <div>
                    <h5 class="mb-1">{{ item.row.title }}</h5>
                    <p class="text-muted small mb-1">{{ item.row.description }}</p>
                    <small class="text-secondary">🕒 {{ item.row.start_time }} to {{ item.row.end_time }}</small>
                    {% if session['role'] == 'VOTER' and item.part %}
                        <div class="mt-1">
                            <span class="badge bg-info text-dark">Eligibility: {{ item.part.status }}</span>
                            {% if item.part.has_voted %}<span class="badge bg-success">Voted ✔</span>{% endif %}
                        </div>
                    {% endif %}
                </div>
                <div class="text-end">
                    <span class="badge {% if item.status == 'ACTIVE' %}bg-success{% elif item.status == 'UPCOMING' %}bg-warning text-dark{% else %}bg-secondary{% endif %} d-block mb-2">
                        {{ item.status }}
                    </span>
                    <a href="{{ url_for('view_election', election_id=item.row.id) }}" class="btn btn-sm btn-outline-primary">Open Election</a>
                </div>
            </div>
        </div>
        {% endfor %}
    </div>

    {% if session['role'] == 'ADMIN' %}
    <div class="col-lg-4">
        <div class="card shadow-sm mb-4">
            <div class="card-header bg-primary text-white fw-bold">Create New Election</div>
            <div class="card-body">
                <form method="POST" action="{{ url_for('create_election') }}">
                    <div class="mb-2"><label class="form-label small">Title</label><input type="text" name="title" class="form-control form-control-sm" required></div>
                    <div class="mb-2"><label class="form-label small">Description</label><textarea name="description" class="form-control form-control-sm"></textarea></div>
                    <div class="mb-2"><label class="form-label small">Start Time</label><input type="datetime-local" name="start_time" class="form-control form-control-sm" required></div>
                    <div class="mb-3"><label class="form-label small">End Time</label><input type="datetime-local" name="end_time" class="form-control form-control-sm" required></div>
                    <button class="btn btn-primary btn-sm w-100">Create Election</button>
                </form>
            </div>
        </div>

        <div class="card shadow-sm">
            <div class="card-header bg-dark text-white fw-bold">Administrative Audit Log</div>
            <ul class="list-group list-group-flush small">
                {% for log in audit_logs %}
                <li class="list-group-item">
                    <strong>{{ log.username }}</strong>: {{ log.action }}
                    <div class="text-muted" style="font-size:0.75rem;">{{ log.timestamp }}</div>
                </li>
                {% else %}
                <li class="list-group-item text-muted">No admin actions recorded yet.</li>
                {% endfor %}
            </ul>
        </div>
    </div>
    {% endif %}
</div>
"""

ELECTION_DETAIL_HTML = """
<a href="{{ url_for('dashboard') }}" class="btn btn-sm btn-secondary mb-3">&larr; Back to Elections</a>
<div class="card shadow-sm mb-4">
    <div class="card-body">
        <div class="d-flex justify-content-between">
            <h3>{{ election.title }}</h3>
            <span class="badge {% if status == 'ACTIVE' %}bg-success{% elif status == 'UPCOMING' %}bg-warning text-dark{% else %}bg-secondary{% endif %} fs-6 align-self-start">{{ status }}</span>
        </div>
        <p class="text-muted mb-1">{{ election.description }}</p>
        <small class="text-secondary">Start: {{ election.start_time }} | End: {{ election.end_time }}</small>
    </div>
</div>

<div class="row">
    <div class="col-md-7">
        {% if status == 'CLOSED' %}
        <div class="card shadow-sm mb-4 border-success">
            <div class="card-header bg-success text-white fw-bold">Official Published Results (Total Votes: {{ total_votes }})</div>
            <div class="card-body">
                {% for r in results %}
                <div class="mb-3">
                    <div class="d-flex justify-content-between fw-bold">
                        <span>{{ r.name }}</span>
                        <span>{{ r.votes }} votes ({{ (r.votes / total_votes * 100)|round(1) if total_votes > 0 else 0 }}%)</span>
                    </div>
                    <div class="progress mt-1" style="height: 10px;">
                        <div class="progress-bar bg-success" style="width: {{ (r.votes / total_votes * 100) if total_votes > 0 else 0 }}%"></div>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <div class="card shadow-sm mb-4">
            <div class="card-header fw-bold">Candidates & Ballot</div>
            <div class="card-body">
                {% if session['role'] == 'VOTER' %}
                    {% if not participation %}
                        <div class="alert alert-info">You must request eligibility approval before voting in this election.</div>
                        <form method="POST" action="{{ url_for('request_participation', election_id=election.id) }}">
                            <button class="btn btn-primary">Request Participation</button>
                        </form>
                    {% else %}
                        <p>Your Eligibility Status: <strong>{{ participation.status }}</strong></p>
                    {% endif %}
                {% endif %}

                <form method="POST" action="{{ url_for('cast_vote', election_id=election.id) }}">
                    {% for c in candidates %}
                    <div class="border rounded p-3 mb-2 d-flex justify-content-between align-items-center">
                        <div class="form-check">
                            {% if session['role'] == 'VOTER' and status == 'ACTIVE' and participation and participation.status == 'APPROVED' and not participation.has_voted %}
                            <input class="form-check-input" type="radio" name="candidate_id" value="{{ c.id }}" id="c{{ c.id }}" required>
                            {% endif %}
                            <label class="form-check-label fw-bold" for="c{{ c.id }}">{{ c.name }}</label>
                            <div class="text-muted small">{{ c.manifesto }}</div>
                        </div>
                        {% if session['role'] == 'ADMIN' and status == 'UPCOMING' %}
                        <button type="submit" formaction="{{ url_for('delete_candidate', election_id=election.id, cand_id=c.id) }}" class="btn btn-sm btn-outline-danger" formnovalidate>Remove</button>
                        {% endif %}
                    </div>
                    {% else %}
                    <p class="text-muted">No candidates registered yet.</p>
                    {% endfor %}

                    {% if session['role'] == 'VOTER' and status == 'ACTIVE' and participation and participation.status == 'APPROVED' %}
                        {% if participation.has_voted %}
                            <div class="alert alert-success mt-3 mb-0">✔ You have already cast your ballot in this election.</div>
                        {% elif candidates %}
                            <button type="submit" class="btn btn-success w-100 mt-3">Confirm & Submit Anonymous Ballot</button>
                        {% endif %}
                    {% endif %}
                </form>
            </div>
        </div>
    </div>

    {% if session['role'] == 'ADMIN' %}
    <div class="col-md-5">
        {% if status == 'UPCOMING' %}
        <div class="card shadow-sm mb-4">
            <div class="card-header fw-bold">Add Candidate (Allowed while UPCOMING)</div>
            <div class="card-body">
                <form method="POST" action="{{ url_for('add_candidate', election_id=election.id) }}">
                    <div class="mb-2"><input type="text" name="name" placeholder="Candidate Name" class="form-control form-control-sm" required></div>
                    <div class="mb-2"><input type="text" name="manifesto" placeholder="Manifesto / Bio" class="form-control form-control-sm" required></div>
                    <button class="btn btn-sm btn-primary w-100">Add Candidate</button>
                </form>
            </div>
        </div>
        {% endif %}

        <div class="card shadow-sm">
            <div class="card-header fw-bold">Voter Eligibility Management</div>
            <ul class="list-group list-group-flush">
                {% for req in voter_requests %}
                <li class="list-group-item d-flex justify-content-between align-items-center">
                    <div>
                        <strong>{{ req.username }}</strong>
                        <span class="badge bg-secondary ms-1">{{ req.status }}</span>
                        {% if req.has_voted %}<span class="badge bg-success">Voted</span>{% endif %}
                    </div>
                    {% if not req.has_voted %}
                    <form method="POST" action="{{ url_for('update_voter_status', election_id=election.id, voter_id=req.voter_id) }}" class="d-inline">
                        <button name="status" value="APPROVED" class="btn btn-xs btn-success btn-sm">Approve</button>
                        <button name="status" value="REJECTED" class="btn btn-xs btn-danger btn-sm">Reject</button>
                    </form>
                    {% endif %}
                </li>
                {% else %}
                <li class="list-group-item text-muted small">No participation requests yet.</li>
                {% endfor %}
            </ul>
        </div>
    </div>
    {% endif %}
</div>
"""


def render_page(template_str, **context):
    inner = render_template_string(template_str, **context)
    return render_template_string(BASE_LAYOUT, content=inner)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)