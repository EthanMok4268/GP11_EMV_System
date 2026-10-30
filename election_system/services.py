"""Election status and voting business rules."""
import uuid
from datetime import datetime

from . import db as database
from .db import get_db


# -------------------------------------------------------------------------
# DETERMINE ELECTION STATUS
# -------------------------------------------------------------------------
def get_election_status(start_time, end_time):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if now < start_time:
        return "UPCOMING"
    if start_time <= now <= end_time:
        return "ACTIVE"
    return "CLOSED"


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
