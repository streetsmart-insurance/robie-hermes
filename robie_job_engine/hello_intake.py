"""Hello inbox Phase 1. No automatic request classification or SOP routing."""
from .intake_core import IntakeWorker
from .intake_email import read_selected_message


class HelloIntake(IntakeWorker):
    process = "hello"
    source_system = "gmail"
    title = "Hello intake: review incoming item and upload source"

    def run_selected(self, gmail, *, mailbox, message_id, identifiers, assignee_id, due_at):
        source = read_selected_message(gmail, mailbox=mailbox, message_id=message_id)
        return self.perform(source, identifiers, assignee_id=assignee_id, due_at=due_at)
