"""Latest Jake pilot assignments; older SOP routing remains separately available."""
from .hello_intake import HelloIntake
from .certificates_intake import CertificatesIntake
from .intake_core import IntakeHold


def pilot_owner(api, key, explicit):
    lookup = getattr(api, 'lookup_pilot_assignee', None)
    if not callable(lookup):
        raise IntakeHold('Verified pilot assignment configuration is not connected')
    rows = lookup(key).checked()
    if len(rows) != 1 or rows[0].get('assignment_key') != key:
        raise IntakeHold('Pilot assignment is missing or ambiguous')
    user = rows[0].get('user_id')
    if not isinstance(user, str) or not user.strip() or (explicit and explicit != user):
        raise IntakeHold('Pilot user ID is missing or conflicts with requested assignment')
    return user


class HelloPilotIntake(HelloIntake):
    def assignment_for(self, applicant, policy, assignee_id, request_type):
        user = pilot_owner(self.api, 'hello_alejandro', assignee_id)
        return user, 'Pilot intake owner: Alejandro. Review and route service work under the SOP.\n'


class CertificatesPilotIntake(CertificatesIntake):
    def assignment_for(self, applicant, policy, assignee_id, request_type):
        user = pilot_owner(self.api, 'certificates_user', assignee_id)
        return super().assignment_for(applicant, policy, user, request_type)
