"""Review-only SOP plan for human-confirmed document facts. Never uploads or labels."""
from dataclasses import dataclass
import hashlib
import json
from .intake_core import IntakeHold, require_test

NAMES = {
    'AI': 'Additional Information', 'AU': 'Audit Request', 'AR': 'Audit Results',
    'CN': 'Cancellation', 'CM': 'Claim', 'CO': 'Correspondence',
    'CR': 'Policy Change Request', 'EP': 'Endorsement Processed',
    'ER': 'Endorsement Request', 'IP': 'Inspection Request', 'LR': 'Loss Runs',
    'NR': 'Non-Renewal', 'BN': 'Binder', 'RC': 'Recommendations',
    'RI': 'Reinstatement', 'RO': 'Renewal Offer', 'SA': 'Signed Application',
    'PD_POLICY': 'Policy', 'PD_DECLARATIONS': 'Declarations Page',
    'FINANCE': 'Cancellation Finance', 'CONDITIONAL_RENEWAL': 'Conditional Renewal Offer',
    'QUOTE': 'Quote Proposal',
}
WORKFLOW = {'AI', 'AU', 'CN', 'IP', 'NR', 'RC'}


@dataclass(frozen=True)
class DocumentFacts:
    first_page: int
    last_page: int
    code: str
    applicant_id: str
    policy_id: str
    policy_number: str
    confirmed: bool = False


def plan_batch(source_id, source_sha256, page_count, documents):
    """Stable page-range identity; no OCR, binary splitting or destination proof implied."""
    require_test()
    if not source_id or len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256):
        raise IntakeHold('Original source ID and SHA-256 are required')
    if type(page_count) is not int or not 1 <= page_count <= 1000 or not documents:
        raise IntakeHold('A bounded page count and reviewed document partition are required')
    covered = set()
    plans = []
    for doc in documents:
        if doc.confirmed is not True or doc.code not in NAMES:
            raise IntakeHold('Confirm each document type; unknown or ambiguous types require review')
        if not all(isinstance(v, str) and v.strip() for v in (doc.applicant_id, doc.policy_id, doc.policy_number)):
            raise IntakeHold('Confirm account, policy ID and policy number before planning')
        if (type(doc.first_page) is not int or type(doc.last_page) is not int
                or not 1 <= doc.first_page <= doc.last_page <= page_count):
            raise IntakeHold('Invalid page range')
        pages = set(range(doc.first_page, doc.last_page + 1))
        if covered & pages:
            raise IntakeHold('Overlapping page ranges require review')
        covered.update(pages)
        identity = json.dumps([source_id, source_sha256, doc.first_page, doc.last_page])
        plans.append(dict(
            source_key='document:' + hashlib.sha256(identity.encode()).hexdigest(),
            first_page=doc.first_page, last_page=doc.last_page,
            document_title=NAMES[doc.code], applicant_id=doc.applicant_id,
            policy_id=doc.policy_id, policy_number=doc.policy_number,
            task_decision='check_existing_then_required_workflow' if doc.code in WORKFLOW else 'review_related_work_and_conditions',
            client_sharing='prohibited' if doc.code == 'QUOTE' else 'review_SOP_before_sharing',
            status='REVIEW_ONLY', destination_verified=False, manual_upload_required=True,
        ))
    if covered != set(range(1, page_count + 1)):
        raise IntakeHold('Every page must belong to a reviewed document')
    return plans
