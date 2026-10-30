"""Election dashboard and detail routes."""
from flask import Blueprint, flash, redirect, render_template, session, url_for

from . import db as database
from .auth import login_required
from .services import (
    candidate_edit_token, election_confirmation_token, election_deletion_token,
    election_readiness_issues, get_election_status,
)

bp = Blueprint("elections", __name__)


# -------------------------------------------------------------------------
# ROUTES: DASHBOARD & ELECTION VIEWING
# -------------------------------------------------------------------------
@bp.route("/")
@login_required
def dashboard():
    elections = []
    for row in database.list_elections():
        status = get_election_status(row)
        if status == "DRAFT" and session.get("role") != "ADMIN":
            continue
        elections.append({
            "row": row,
            "status": status,
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
    if not election or (not election["is_confirmed"] and session.get("role") != "ADMIN"):
        flash("Election not found.", "danger")
        return redirect(url_for("elections.dashboard"))

    status = get_election_status(election)
    candidates = database.list_candidates(election_id)
    participation = database.find_participation(election_id, session["user_id"])
    # Calculate results ONLY if election is CLOSED
    results = database.list_results(election_id) if status == "CLOSED" else []
    total_votes = sum(result["votes"] for result in results)
    # Admin view: load voter eligibility requests
    voter_requests = database.list_voter_requests(election_id) if session.get("role") == "ADMIN" else []
    candidate_tokens = {}
    confirmation_token = None
    deletion_token = None
    readiness_issues = []
    if session.get("role") == "ADMIN" and status == "CLOSED":
        deletion_token = election_deletion_token(election_id, session["user_id"])
    if session.get("role") == "ADMIN" and status in ("DRAFT", "UPCOMING"):
        candidate_tokens = {candidate["id"]: candidate_edit_token(candidate) for candidate in candidates}
        if status == "DRAFT":
            confirmation_token = election_confirmation_token(election, candidates)
            readiness_issues = election_readiness_issues(election, candidates)
    return render_template(
        "election_detail.html", election=election, status=status, candidates=candidates,
        participation=participation, results=results, total_votes=total_votes, voter_requests=voter_requests,
        candidate_tokens=candidate_tokens, confirmation_token=confirmation_token, readiness_issues=readiness_issues,
        deletion_token=deletion_token,
    )
