"""Election status and voting business rules."""
import json
import secrets
import uuid
from datetime import datetime

from flask import current_app, session
from itsdangerous import BadData, URLSafeSerializer

from . import db as database
from .db import get_db


# -------------------------------------------------------------------------
# DETERMINE ELECTION STATUS
# -------------------------------------------------------------------------
def get_election_status(election, now=None):
    if not election["is_confirmed"]:
        return "DRAFT"
    if now is None:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if now > election["end_time"]:
        return "CLOSED"
    if now < election["start_time"]:
        return "UPCOMING"
    return "ACTIVE"


# -------------------------------------------------------------------------
# VALIDATE ELECTION DETAILS & UPDATE WITH AN ADMINISTRATIVE AUDIT
# -------------------------------------------------------------------------
def validate_election_parameters(title, description, start_time, end_time):
    """Validate form values and normalize dates to the database's minute format."""
    title = title.strip()
    if not title:
        raise ValueError("Election title is required.")
    try:
        start_time, end_time = (
            datetime.strptime(value, "%Y-%m-%dT%H:%M").isoformat(timespec="minutes")
            for value in (start_time, end_time)
        )
    except ValueError as error:
        raise ValueError("Invalid election dates. Enter valid dates and times in minute format.") from error
    if start_time >= end_time:
        raise ValueError("Invalid election parameters. Ensure start time is before end time.")
    return {
        "title": title,
        "description": description.strip(),
        "start_time": start_time,
        "end_time": end_time,
    }


def update_election_details(election_id, admin_id, title, description, start_time, end_time):
    """Lock, recheck the latest state, and atomically update details and their audit."""
    with get_db() as db:
        # Acquire the write lock before reading state, including after a lock wait.
        db.execute("BEGIN IMMEDIATE")
        election = database.find_election(election_id)
        if election is None:
            raise LookupError("Election not found.")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        status = get_election_status(election, now=now)
        if status == "CLOSED":
            raise ValueError("Closed elections cannot be edited. Reload the election page.")

        values = validate_election_parameters(title, description, start_time, end_time)
        if status == "UPCOMING" and values["start_time"] <= now:
            raise ValueError("An upcoming election's start time must remain in the future. Reload the election page.")
        if status == "ACTIVE":
            if values["start_time"] != election["start_time"]:
                raise ValueError("An active election's start time cannot be changed. Reload the election page.")
            if values["end_time"] < election["end_time"]:
                raise ValueError("An active election's end time can only be extended. Reload the election page.")

        changes = {
            field: {"before": election[field], "after": value}
            for field, value in values.items() if value != election[field]
        }
        if not changes:
            return "No election changes to save.", "info"

        database.update_election(election_id, **values)
        database.log_audit(
            admin_id, f"Updated Election ID {election_id}: {json.dumps(changes, ensure_ascii=False)}"
        )
    return "Election updated successfully.", "success"


# -------------------------------------------------------------------------
# UPDATE VOTER ELIGIBILITY & ITS AUDIT BEFORE ELECTION CLOSURE
# -------------------------------------------------------------------------
def update_voter_eligibility(election_id, admin_id, voter_id, status):
    """Recheck closure after obtaining the write lock and commit the decision with its audit."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        election = database.find_election(election_id)
        if election is None:
            raise LookupError("Election not found.")
        if get_election_status(election) == "CLOSED":
            raise ValueError("Voter eligibility cannot be changed after this election has closed.")
        database.update_voter_status(election_id, voter_id, status)
        database.log_audit(
            admin_id,
            f"Set Voter ID {voter_id} eligibility to {status} for Election ID {election_id}",
        )
    return f"Voter eligibility updated to {status}.", "success"


# -------------------------------------------------------------------------
# SIGN FORM SNAPSHOTS & CONFIRM A COMPLETE CANDIDATE ROSTER
# -------------------------------------------------------------------------
def _form_serializer(purpose):
    return URLSafeSerializer(current_app.secret_key, salt=purpose)


def candidate_snapshot(candidate):
    return {field: candidate[field] for field in ("id", "election_id", "name", "manifesto")}


def candidate_edit_token(candidate):
    return _form_serializer("candidate-edit").dumps(candidate_snapshot(candidate))


def confirmation_snapshot(election, candidates):
    return {
        "election_id": election["id"],
        "start_time": election["start_time"],
        "end_time": election["end_time"],
        "candidates": [candidate_snapshot(candidate) for candidate in sorted(candidates, key=lambda row: row["id"])],
    }


def election_confirmation_token(election, candidates):
    return _form_serializer("election-confirmation").dumps(confirmation_snapshot(election, candidates))


def _check_form_token(token, purpose, expected):
    try:
        original = _form_serializer(purpose).loads(token)
    except BadData as error:
        raise ValueError("Invalid or expired form. Reload the election page before saving.") from error
    if original != expected:
        raise ValueError("This form is out of date. Reload the election page before saving.")


# -------------------------------------------------------------------------
# CONFIRM & PERMANENTLY DELETE A CLOSED ELECTION
# -------------------------------------------------------------------------
def _election_deletion_context(election_id, admin_id):
    return {
        "election_id": election_id,
        "admin_id": admin_id,
        "session_nonce": session.get("election_deletion_nonce"),
    }


def election_deletion_token(election_id, admin_id):
    if not session.get("election_deletion_nonce"):
        session["election_deletion_nonce"] = secrets.token_urlsafe(32)
    return _form_serializer("election-deletion").dumps(_election_deletion_context(election_id, admin_id))


def delete_closed_election(election_id, admin_id, deletion_token, confirmed):
    """Delete the election, its dependent records and its deletion audit atomically."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        election = database.find_election(election_id)
        if election is None:
            raise LookupError("Election not found.")
        if get_election_status(election) != "CLOSED":
            raise ValueError("Only CLOSED elections can be deleted. Reload the election page.")
        if confirmed != "yes":
            raise ValueError("Confirm deletion in the election's deletion dialog before deleting.")
        if not session.get("election_deletion_nonce"):
            raise ValueError("Invalid deletion confirmation. Reload the election page before deleting.")
        try:
            _check_form_token(
                deletion_token, "election-deletion", _election_deletion_context(election_id, admin_id)
            )
        except ValueError as error:
            raise ValueError("Invalid deletion confirmation. Reload the election page before deleting.") from error
        database.delete_election(election_id)
        database.log_audit(admin_id, f"Deleted Election ID {election_id} ('{election['title']}') permanently")
    return (
        "Election and all associated voting data deleted permanently. Administrative audit logs were retained.",
        "success",
    )


# -------------------------------------------------------------------------
# VALIDATE READINESS & CONFIRM THE ELECTION
# -------------------------------------------------------------------------
def election_readiness_issues(election, candidates, now=None):
    if now is None:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    issues = []
    if len(candidates) < 2:
        issues.append("At least two candidates are required before confirming the election.")
    if any(not (row["name"] or "").strip() or not (row["manifesto"] or "").strip() for row in candidates):
        issues.append("Every candidate must have a name and manifesto.")
    if election["end_time"] < now:
        issues.append("The end time has passed. Update the schedule before confirming.")
    return issues


def confirm_election(election_id, admin_id, confirmation_token):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        election = database.find_election(election_id)
        if election is None:
            raise LookupError("Election not found.")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        if get_election_status(election, now=now) == "CLOSED":
            raise ValueError("Closed elections cannot be confirmed or reopened.")
        candidates = database.list_candidates(election_id)
        _check_form_token(
            confirmation_token, "election-confirmation", confirmation_snapshot(election, candidates)
        )
        if election["is_confirmed"]:
            return "This election is already confirmed.", "info"
        issues = election_readiness_issues(election, candidates, now=now)
        if issues:
            raise ValueError(" ".join(issues))
        database.set_election_confirmation(election_id, True)
        database.log_audit(
            admin_id, f"Confirmed Election ID {election_id}: "
            + json.dumps({"is_confirmed": {"before": 0, "after": 1},
                          "roster": confirmation_snapshot(election, candidates)}, ensure_ascii=False),
        )
    if now < election["start_time"]:
        return "Election confirmed. Voting will begin at its scheduled start time.", "success"
    return "Election confirmed. Voting is now active.", "success"


# -------------------------------------------------------------------------
# MANAGE CANDIDATES BEFORE VOTING & INVALIDATE ROSTER CONFIRMATION
# -------------------------------------------------------------------------
def change_candidate(election_id, admin_id, operation, candidate_id=None, name="", manifesto="", edit_token=""):
    errors = {
        "add": "Candidates can only be added before an election begins (DRAFT or UPCOMING status).",
        "update": "Candidates cannot be edited after an election starts.",
        "delete": "Candidates cannot be removed after an election starts.",
    }
    if operation not in errors:
        raise ValueError("Invalid candidate operation.")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        election = database.find_election(election_id)
        if election is None:
            raise LookupError("Election not found.")
        if get_election_status(election) not in ("DRAFT", "UPCOMING"):
            raise ValueError("Candidate Constraint: " + errors[operation])
        candidate = None
        if operation != "add":
            candidate = database.find_candidate(election_id, candidate_id)
            if candidate is None:
                raise ValueError("Candidate not found in this election. Reload the election page.")
        if operation == "update":
            _check_form_token(edit_token, "candidate-edit", candidate_snapshot(candidate))
        if operation != "delete":
            name, manifesto = name.strip(), manifesto.strip()
            if not name or not manifesto:
                raise ValueError("Candidate name and manifesto are both required.")
        before = {field: candidate[field] for field in ("name", "manifesto")} if candidate else None
        after = {"name": name, "manifesto": manifesto} if operation != "delete" else None
        if operation == "update" and before == after:
            return "No candidate changes to save.", "info"

        if operation == "add":
            candidate_id = database.add_candidate(election_id, name, manifesto)
            message = f"Candidate '{name}' added."
            action = f"Added candidate '{name}' (ID: {candidate_id})"
        elif operation == "update":
            database.update_candidate(election_id, candidate_id, name, manifesto)
            message, action = "Candidate updated successfully.", f"Updated candidate ID {candidate_id}"
        else:
            database.delete_candidate(election_id, candidate_id)
            message, action = "Candidate removed.", f"Removed candidate ID {candidate_id}"
        changes = {"candidate": {"before": before, "after": after}}
        if election["is_confirmed"]:
            database.set_election_confirmation(election_id, False)
            changes["is_confirmed"] = {"before": 1, "after": 0}
            message += " Election returned to draft. Confirm it again when the roster is complete."
        database.log_audit(admin_id, f"{action} in Election ID {election_id}: {json.dumps(changes, ensure_ascii=False)}")
    return message, "success"


# -------------------------------------------------------------------------
# VALIDATE & RECORD AN ANONYMOUS BALLOT
# -------------------------------------------------------------------------
def record_vote(election_id, voter_id, candidate_id, role):
    """Return a message and category after validating and atomically recording a vote."""
    if role != "VOTER":
        return "Only voters can cast ballots.", "danger"

    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            election = database.find_election(election_id)
            if not election or get_election_status(election) != "ACTIVE":
                return "Voting rejected: This election is not currently active.", "danger"
            participation = database.find_participation(election_id, voter_id)
            if not participation or participation["status"] != "APPROVED":
                return "Voting rejected: You are not approved to vote in this election.", "danger"
            if participation["has_voted"] == 1:
                return "Duplicate voting prevented: You have already cast a ballot in this election.", "danger"
            if not database.find_candidate(election_id, candidate_id):
                return "Invalid candidate selection.", "danger"
            if database.mark_voted(election_id, voter_id) != 1:
                raise RuntimeError("Concurrent duplicate vote attempt blocked.")
            ballot_id = str(uuid.uuid4())
            database.insert_ballot(ballot_id, election_id, candidate_id)
        # Step 7: Confirmation Message
        return f"Vote successfully recorded! Your anonymous receipt ID is: {ballot_id[:8]}...", "success"
    except Exception:
        return "An unexpected error occurred. Transaction rolled back to preserve data integrity.", "danger"
