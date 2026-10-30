"""Voter participation and ballot routes."""
import sqlite3

from flask import Blueprint, flash, redirect, request, session, url_for

from . import db as database
from .auth import login_required
from .db import get_db
from .services import get_election_status, record_vote

bp = Blueprint("voting", __name__)


# -------------------------------------------------------------------------
# ROUTES: VOTER ACTIONS (REQUEST ELIGIBILITY & ATOMIC VOTING)
# -------------------------------------------------------------------------
@bp.route("/election/<int:election_id>/request", methods=["POST"])
@login_required
def request_participation(election_id):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            election = database.find_election(election_id)
            if session.get("role") != "VOTER" or not election or get_election_status(election) == "DRAFT":
                flash("Participation is not available for this election.", "danger")
                return redirect(url_for("elections.dashboard"))
            database.create_participation(election_id, session["user_id"])
        flash("Participation request submitted. Waiting for administrator approval.", "success")
    except sqlite3.IntegrityError:
        flash("You have already submitted a request for this election.", "warning")
    return redirect(url_for("elections.view_election", election_id=election_id))


# -------------------------------------------------------------------------
# ROUTES: CAST A BALLOT
# -------------------------------------------------------------------------
@bp.route("/election/<int:election_id>/vote", methods=["POST"])
@login_required
def cast_vote(election_id):
    message, category = record_vote(
        election_id, session["user_id"], request.form.get("candidate_id"), session.get("role")
    )
    flash(message, category)
    return redirect(url_for("elections.view_election", election_id=election_id))
