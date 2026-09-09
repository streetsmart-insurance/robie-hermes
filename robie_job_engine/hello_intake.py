"""Hello Phase 1 routing from a human-confirmed request type and fresh ownership."""
from .intake_core import IntakeHold, IntakeWorker
from .intake_email import read_selected_message


class HelloIntake(IntakeWorker):
    process = "hello"
    source_system = "gmail"
    title = "Hello intake: review incoming item and upload source"

    def assignment_for(self, applicant, policy, assignee_id, request_type):
        roles = {"new_business": "originating_producer", "renewal": "applicable_csr", "midterm": "applicable_csr"}
        if request_type not in roles:
            raise IntakeHold("Human-confirmed Hello request type required: new_business, renewal or midterm")
        if request_type != "new_business" and not policy:
            raise IntakeHold("Renewal/midterm routing requires a verified policy")
        lookup = getattr(self.api, "lookup_hello_owner", None)
        if not callable(lookup):
            raise IntakeHold("Hello ownership API mapping has not been connected")
        rows = lookup(applicant, policy, request_type).checked()
        if len(rows) != 1:
            raise IntakeHold("Hello ownership is missing or ambiguous")
        owner = rows[0]
        if (owner.get("applicant_id") != applicant or owner.get("policy_id") != policy
                or owner.get("role") != roles[request_type]):
            raise IntakeHold("Hello ownership does not confirm the matched account/policy and required role")
        user_id = owner.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise IntakeHold("Hello owner has no usable user ID")
        if assignee_id and assignee_id != user_id:
            raise IntakeHold("Explicit assignee conflicts with SOP ownership")
        return user_id, f"Human-confirmed request type: {request_type}; route: {roles[request_type]}.\n"

    def run_selected(self, gmail, *, mailbox, message_id, identifiers, due_at,
                     request_type="", assignee_id=""):
        source = read_selected_message(gmail, mailbox=mailbox, message_id=message_id)
        return self.perform(source, identifiers, assignee_id=assignee_id, due_at=due_at, request_type=request_type)
