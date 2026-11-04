"""Administrator election, candidate, and eligibility routes."""
import json
import sqlite3
from datetime import datetime, timedelta
from itertools import zip_longest

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from . import db as database
from .auth import admin_required, login_required
from .db import get_db
from .services import (
    change_candidate, confirm_election as confirm_election_service, get_election_status,
    delete_election as delete_election_service, update_election_details, update_voter_eligibility,
    validate_election_parameters, validate_candidate_parameters,
)

bp = Blueprint("admin", __name__)
_AUDIT_LOG_BATCH_SIZE = 20


def _audit_log_batch(before_id=None):
    rows = database.list_audit_logs(before_id=before_id, limit=_AUDIT_LOG_BATCH_SIZE + 1)
    audit_logs = rows[:_AUDIT_LOG_BATCH_SIZE]
    next_url = None
    if len(rows) > _AUDIT_LOG_BATCH_SIZE:
        next_url = url_for("admin.audit_log_entries", before=audit_logs[-1]["id"])
    return audit_logs, next_url


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
@bp.route("/admin/audit-logs")
@login_required
@admin_required
def audit_logs():
    entries, next_url = _audit_log_batch()
    return render_template("audit_logs.html", audit_logs=entries, next_url=next_url)


@bp.route("/admin/audit-logs/entries")
@login_required
@admin_required
def audit_log_entries():
    before = request.args.get("before", "")
    normalized_before = before.lstrip("0")
    if (
        not before.isascii() or not before.isdecimal() or not normalized_before
        or len(normalized_before) > 19
        or (len(normalized_before) == 19 and normalized_before > "9223372036854775807")
    ):
        return jsonify(error="Invalid audit log cursor."), 400
    entries, next_url = _audit_log_batch(int(normalized_before))
    return jsonify(
        entries=[
            {field: entry[field] for field in ("username", "action", "timestamp")}
            for entry in entries
        ],
        next_url=next_url,
    )


@bp.route("/admin/election/create", methods=["GET", "POST"])
@login_required
@admin_required
def create_election():
    form_data = {field: request.form.get(field, "") for field in (
        "title", "description", "start_time", "end_time",
    )}
    names = request.form.getlist("candidate_name")
    manifestos = request.form.getlist("candidate_manifesto")
    candidate_rows = [
        {"name": name, "manifesto": manifesto}
        for name, manifesto in zip_longest(names, manifestos, fillvalue="")
    ]
    candidate_errors = {}

    def show_form(needs_confirmation=False):
        return render_template(
            "create_election.html", form_data=form_data,
            candidate_rows=candidate_rows or [{"name": "", "manifesto": ""}],
            candidate_errors=candidate_errors, needs_confirmation=needs_confirmation,
        )

    if request.method == "GET" or request.form.get("cancel_without_candidates") == "yes":
        return show_form()

    try:
        values = validate_election_parameters(
            **form_data,
        )
    except ValueError as error:
        flash(str(error), "danger")
        return show_form()

    if len(names) != len(manifestos):
        flash("Candidate fields do not match. Review each candidate before creating the election.", "danger")
        return show_form()

    candidates = []
    for index, candidate in enumerate(candidate_rows):
        if not candidate["name"].strip() and not candidate["manifesto"].strip():
            continue
        try:
            candidates.append(validate_candidate_parameters(**candidate))
        except ValueError as error:
            candidate_errors[index] = str(error)
    if candidate_errors:
        flash("Please correct the highlighted candidate details.", "danger")
        return show_form()

    if not candidates and request.form.get("confirm_without_candidates") != "yes":
        return show_form(needs_confirmation=True)

    try:
        with get_db():
            election_id = database.create_election(**values)
            database.log_audit(session["user_id"], f"Created election '{values['title']}' (ID: {election_id})")
            for candidate in candidates:
                candidate_id = database.add_candidate(election_id, **candidate)
                changes = {"candidate": {"before": None, "after": candidate}}
                database.log_audit(
                    session["user_id"],
                    f"Added candidate '{candidate['name']}' (ID: {candidate_id}) in Election ID {election_id}: "
                    + json.dumps(changes, ensure_ascii=False),
                )
    except sqlite3.Error:
        flash("Unable to create the election. Changes were rolled back. Please try again.", "danger")
        return show_form()
    flash("Election created successfully. Prepare the candidates, then confirm the election.", "success")
    return redirect(url_for("elections.view_election", election_id=election_id))


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
# ROUTES: DELETE AN ELECTION
# -------------------------------------------------------------------------
@bp.route("/admin/election/<int:election_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_election(election_id):
    destination = url_for("elections.dashboard")
    try:
        message, category = delete_election_service(
            election_id, session["user_id"], request.form.get("deletion_token", ""),
            request.form.get("confirmed", ""),
        )
    except LookupError as error:
        message, category = str(error), "danger"
    except ValueError as error:
        message, category = str(error), "danger"
        destination = url_for("elections.view_election", election_id=election_id)
    except sqlite3.Error:
        message = "Unable to delete the election. Changes were rolled back. Please try again."
        category = "danger"
        destination = url_for("elections.view_election", election_id=election_id)
    flash(message, category)
    return redirect(destination)


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
        return _admin_action(
            election_id, update_voter_eligibility, voter_id=voter_id, status=new_status,
        )
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
