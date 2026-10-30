"""Administrator election, candidate, and eligibility routes."""
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, request, session, url_for

from . import db as database
from .auth import admin_required, login_required
from .db import get_db
from .services import get_election_status, update_election_details, validate_election_parameters

bp = Blueprint("admin", __name__)


# -------------------------------------------------------------------------
# ROUTES: ADMINISTRATOR ACTIONS
# -------------------------------------------------------------------------
@bp.route("/admin/election/create", methods=["POST"])
@login_required
@admin_required
def create_election():
    try:
        values = validate_election_parameters(
            request.form.get("title", ""), request.form.get("description", ""),
            request.form.get("start_time", ""), request.form.get("end_time", ""),
        )
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("elections.dashboard"))

    try:
        with get_db():
            election_id = database.create_election(**values)
            database.log_audit(session["user_id"], f"Created election '{values['title']}' (ID: {election_id})")
    except sqlite3.Error:
        flash("Unable to create the election. Changes were rolled back. Please try again.", "danger")
        return redirect(url_for("elections.dashboard"))
    flash("Election created successfully.", "success")
    return redirect(url_for("elections.dashboard"))


# -------------------------------------------------------------------------
# ROUTES: EDIT ELECTION DETAILS
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/update", methods=["POST"])
@login_required
@admin_required
def update_election(election_id):
    try:
        message, category = update_election_details(
            election_id, session["user_id"],
            request.form.get("title", ""), request.form.get("description", ""),
            request.form.get("start_time", ""), request.form.get("end_time", ""),
        )
    except LookupError as error:
        flash(str(error), "danger")
        return redirect(url_for("elections.dashboard"))
    except ValueError as error:
        message, category = str(error), "danger"
    except sqlite3.Error:
        message = "Unable to update the election. Changes were rolled back. Please try again."
        category = "danger"
    flash(message, category)
    return redirect(url_for("elections.view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: ADD A CANDIDATE
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/candidate/add", methods=["POST"])
@login_required
@admin_required
def add_candidate(election_id):
    election = database.find_election(election_id)
    if get_election_status(election["start_time"], election["end_time"]) != "UPCOMING":
        flash("Candidate Constraint: Candidates can only be added before an election begins (UPCOMING status).", "danger")
        return redirect(url_for("elections.view_election", election_id=election_id))

    name = request.form.get("name", "").strip()
    manifesto = request.form.get("manifesto", "").strip()
    if name:
        with get_db():
            database.add_candidate(election_id, name, manifesto)
            database.log_audit(session["user_id"], f"Added candidate '{name}' to Election ID {election_id}")
        flash(f"Candidate '{name}' added.", "success")
    return redirect(url_for("elections.view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: REMOVE A CANDIDATE
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/candidate/<int:cand_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_candidate(election_id, cand_id):
    election = database.find_election(election_id)
    if get_election_status(election["start_time"], election["end_time"]) != "UPCOMING":
        flash("Candidate Constraint: Candidates cannot be removed after an election starts.", "danger")
        return redirect(url_for("elections.view_election", election_id=election_id))
    with get_db():
        database.delete_candidate(election_id, cand_id)
        database.log_audit(session["user_id"], f"Removed candidate ID {cand_id} from Election ID {election_id}")
    flash("Candidate removed.", "info")
    return redirect(url_for("elections.view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: UPDATE VOTER ELIGIBILITY
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/voter/<int:voter_id>/status", methods=["POST"])
@login_required
@admin_required
def update_voter_status(election_id, voter_id):
    new_status = request.form.get("status")
    if new_status in ("APPROVED", "REJECTED"):
        with get_db():
            database.update_voter_status(election_id, voter_id, new_status)
            database.log_audit(
                session["user_id"],
                f"Set Voter ID {voter_id} eligibility to {new_status} for Election ID {election_id}",
            )
        flash(f"Voter eligibility updated to {new_status}.", "success")
    return redirect(url_for("elections.view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: CLOSE AN ELECTION EARLY
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/close", methods=["POST"])
@login_required
@admin_required
def close_election_early(election_id):
    election = database.find_election(election_id)
    if not election:
        flash("Election not found.", "danger")
        return redirect(url_for("elections.dashboard"))

    status = get_election_status(election["start_time"], election["end_time"])
    if status != "ACTIVE":
        flash("Only ACTIVE elections can be closed early.", "warning")
        return redirect(url_for("elections.view_election", election_id=election_id))

    early_end_time = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    with get_db():
        database.update_election_end_time(election_id, early_end_time)
        database.log_audit(session["user_id"], f"Closed Election ID {election_id} ('{election['title']}') early")

    flash("Election has been closed early. Official results are now published.", "info")
    return redirect(url_for("elections.view_election", election_id=election_id))
