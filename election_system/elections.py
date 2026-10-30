"""Election dashboard and detail routes."""
from flask import Blueprint, flash, redirect, render_template, session, url_for

from . import db as database
from .auth import login_required
from .services import get_election_status

bp = Blueprint("elections", __name__)


# -------------------------------------------------------------------------
# ROUTES: DASHBOARD & ELECTION VIEWING
# -------------------------------------------------------------------------
@bp.route("/")
@login_required
def dashboard():
    elections = []
    for row in database.list_elections():
        elections.append({
            "row": row,
            "status": get_election_status(row["start_time"], row["end_time"]),
            "part": database.find_participation(row["id"], session["user_id"]),
        })
    audit_logs = database.list_recent_audit_logs() if session.get("role") == "ADMIN" else []
    return render_template("dashboard.html", elections=elections, audit_logs=audit_logs)


# -------------------------------------------------------------------------
# ROUTES: ELECTION DETAILS & RESULTS
# -------------------------------------------------------------------------
@bp.route("/election/<int:election_id>")
@login_required
def view_election(election_id):
    election = database.find_election(election_id)
    if not election:
        flash("Election not found.", "danger")
        return redirect(url_for("elections.dashboard"))

    status = get_election_status(election["start_time"], election["end_time"])
    candidates = database.list_candidates(election_id)
    participation = database.find_participation(election_id, session["user_id"])
    # Calculate results ONLY if election is CLOSED
    results = database.list_results(election_id) if status == "CLOSED" else []
    total_votes = sum(result["votes"] for result in results)
    # Admin view: load voter eligibility requests
    voter_requests = database.list_voter_requests(election_id) if session.get("role") == "ADMIN" else []
    return render_template(
        "election_detail.html", election=election, status=status, candidates=candidates,
        participation=participation, results=results, total_votes=total_votes, voter_requests=voter_requests,
    )
