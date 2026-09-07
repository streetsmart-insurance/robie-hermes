"""Certificates inbox Phase 1. Human review/issuance remains a separate task."""
from .intake_core import IntakeWorker
from .intake_email import read_selected_message


class CertificatesIntake(IntakeWorker):
    process = "certificates"
    source_system = "gmail"
    title = "Certificates intake: review written request and upload source"

    def run_selected(self, gmail, *, mailbox, message_id, identifiers, assignee_id, due_at):
        source = read_selected_message(gmail, mailbox=mailbox, message_id=message_id)
        return self.perform(source, identifiers, assignee_id=assignee_id, due_at=due_at)
