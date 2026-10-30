"""Administrator election, candidate, and eligibility routes."""
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, request, session, url_for

from . import db as database
from .auth import admin_required, login_required
from .db import get_db
from .services import (
    change_candidate, confirm_election as confirm_election_service, get_election_status,
    update_election_details, validate_election_parameters,
)

bp = Blueprint("admin", __name__)


def _admin_action(election_id, action, **values):
    try:
        message, category = action(election_id, session["user_id"], **values)
    except LookupError as error:
        flash(str(error), "danger")
        return redirect(url_for("elections.dashboard"))
    except ValueError as error:
        message, category = str(error), "danger"
    except sqlite3.Error:
        message = "Unable to save changes. Changes were rolled back. Please try again."
        category = "danger"
    flash(message, category)
    return redirect(url_for("elections.view_election", election_id=election_id))


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
    flash("Election created successfully. Prepare the candidates, then confirm the election.", "success")
    return redirect(url_for("elections.dashboard"))


# -------------------------------------------------------------------------
# ROUTES: EDIT ELECTION DETAILS
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/update", methods=["POST"])
@login_required
@admin_required
def update_election(election_id):
    return _admin_action(
        election_id, update_election_details,
        title=request.form.get("title", ""), description=request.form.get("description", ""),
        start_time=request.form.get("start_time", ""), end_time=request.form.get("end_time", ""),
    )


# -------------------------------------------------------------------------
# ROUTES: CONFIRM THE ROSTER AND SCHEDULE OR START VOTING
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/confirm", methods=["POST"])
@login_required
@admin_required
def confirm_election(election_id):
    return _admin_action(
        election_id, confirm_election_service, confirmation_token=request.form.get("confirmation_token", "")
    )


# -------------------------------------------------------------------------
# ROUTES: ADD A CANDIDATE
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/candidate/add", methods=["POST"])
@login_required
@admin_required
def add_candidate(election_id):
    return _admin_action(
        election_id, change_candidate, operation="add",
        name=request.form.get("name", ""), manifesto=request.form.get("manifesto", ""),
    )


# -------------------------------------------------------------------------
# ROUTES: EDIT A CANDIDATE
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/candidate/<int:cand_id>/update", methods=["POST"])
@login_required
@admin_required
def update_candidate(election_id, cand_id):
    return _admin_action(
        election_id, change_candidate, operation="update", candidate_id=cand_id,
        name=request.form.get("name", ""), manifesto=request.form.get("manifesto", ""),
        edit_token=request.form.get("edit_token", ""),
    )


# -------------------------------------------------------------------------
# ROUTES: REMOVE A CANDIDATE
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/candidate/<int:cand_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_candidate(election_id, cand_id):
    return _admin_action(election_id, change_candidate, operation="delete", candidate_id=cand_id)


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

    status = get_election_status(election)
    if status != "ACTIVE":
        flash("Only ACTIVE elections can be closed early.", "warning")
        return redirect(url_for("elections.view_election", election_id=election_id))

    early_end_time = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    with get_db():
        database.update_election_end_time(election_id, early_end_time)
        database.log_audit(session["user_id"], f"Closed Election ID {election_id} ('{election['title']}') early")

    flash("Election has been closed early. Official results are now published.", "info")
    return redirect(url_for("elections.view_election", election_id=election_id))
