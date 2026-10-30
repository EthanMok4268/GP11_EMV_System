"""Election status and voting business rules."""
import json
import uuid
from datetime import datetime

from . import db as database
from .db import get_db


# -------------------------------------------------------------------------
# DETERMINE ELECTION STATUS
# -------------------------------------------------------------------------
def get_election_status(start_time, end_time, now=None):
    if now is None:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if now < start_time:
        return "UPCOMING"
    if start_time <= now <= end_time:
        return "ACTIVE"
    return "CLOSED"


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
        status = get_election_status(election["start_time"], election["end_time"], now=now)
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
# VALIDATE & RECORD AN ANONYMOUS BALLOT
# -------------------------------------------------------------------------
def record_vote(election_id, voter_id, candidate_id, role):
    """Return a message and category after validating and atomically recording a vote."""
    if role != "VOTER":
        return "Only voters can cast ballots.", "danger"

    # Step 2 Check: Election is ACTIVE
    election = database.find_election(election_id)
    if not election or get_election_status(election["start_time"], election["end_time"]) != "ACTIVE":
        return "Voting rejected: This election is not currently active.", "danger"

    # Step 3 & 4 Checks: Voter is APPROVED and has NOT voted
    participation = database.find_participation(election_id, voter_id)
    if not participation or participation["status"] != "APPROVED":
        return "Voting rejected: You are not approved to vote in this election.", "danger"
    if participation["has_voted"] == 1:
        return "Duplicate voting prevented: You have already cast a ballot in this election.", "danger"

    # Verify candidate belongs to this election
    if not database.find_candidate(election_id, candidate_id):
        return "Invalid candidate selection.", "danger"

    # Step 6: Atomic Database Transaction (Ballot + Participation Status Update)
    try:
        with get_db():
            if database.mark_voted(election_id, voter_id) != 1:
                raise RuntimeError("Concurrent duplicate vote attempt blocked.")
            ballot_id = str(uuid.uuid4())
            database.insert_ballot(ballot_id, election_id, candidate_id)
        # Step 7: Confirmation Message
        return f"Vote successfully recorded! Your anonymous receipt ID is: {ballot_id[:8]}...", "success"
    except Exception:
        return "An unexpected error occurred. Transaction rolled back to preserve data integrity.", "danger"
