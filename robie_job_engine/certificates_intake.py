"""Certificates inbox Phase 1. Human review/issuance remains a separate task."""
from .intake_core import IntakeHold, IntakeWorker
from .intake_email import read_selected_message


class CertificatesIntake(IntakeWorker):
    process = "certificates"
    source_system = "gmail"
    title = "Certificates intake: review written request and upload source"

    def assignment_for(self, applicant, policy, assignee_id, request_type):
        if not isinstance(assignee_id, str) or not assignee_id.strip():
            raise IntakeHold("A configured Certificates team assignee ID is required")
        lookup = getattr(self.api, "lookup_certificates_team_member", None)
        if not callable(lookup):
            raise IntakeHold("Authoritative Certificates team membership has not been connected")
        rows = lookup(assignee_id).checked()
        if (len(rows) != 1 or rows[0].get("user_id") != assignee_id
                or rows[0].get("certificates_team_member") is not True):
            raise IntakeHold("Assignee is not uniquely verified as a Certificates team member")
        return assignee_id, (
            "Route: verified Certificates team member. Human certificate review required; "
            "intake does not issue a certificate or verify coverage.\n"
        )

    def run_selected(self, gmail, *, mailbox, message_id, identifiers, assignee_id, due_at):
        source = read_selected_message(gmail, mailbox=mailbox, message_id=message_id)
        return self.perform(source, identifiers, assignee_id=assignee_id, due_at=due_at)
