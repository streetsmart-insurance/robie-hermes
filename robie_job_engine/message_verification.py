"""Use fresh reads for message reports; authorize completion only for covered requests."""
import re
from dataclasses import replace

# A policy-existence read can prove this entire request. It cannot prove a
# cancellation, field edit, document upload, or outgoing message merely because
# the policy exists. Other bounded action types have their own verifiers.
_POLICY_PRESENCE = re.compile(
    r'(?:please\s+)?(?:check|confirm|verify)\s+(?:that\s+)?(?:the\s+)?policy'
    r'(?:\s+(?:number|#))?\s+([\w/-]+)\s+(?:exists|is\s+present)'
    r'\s+on\s+applicant\s+(\d+)\s*[.!]?\s*', re.I)


def presence_request(text):
    """Email subjects remain part of the requested scope, not disposable metadata."""
    if text.startswith('Subject: '):
        subject, separator, body = text.partition('\n\n')
        if not separator:
            return None
        subject = subject[len('Subject: '):].strip()
        body_match = _POLICY_PRESENCE.fullmatch(body.strip())
        if not body_match:
            return None
        subject_match = _POLICY_PRESENCE.fullmatch(subject)
        if subject.casefold() in {'task', 'robie task', 'policy check', 'verification'}:
            return body_match
        if subject_match and subject_match.groups() == body_match.groups():
            return body_match
        return None
    return _POLICY_PRESENCE.fullmatch(text.strip())


class MessageOutcomeVerifier:
    def __init__(self, reader):
        self.reader = reader

    def verify(self, job, action):
        result = self.reader.verify(job, action)
        if not result.verified:
            return result
        expected = result.evidence.expected
        # Applicant-scoped filings (note and/or documents, no policy number)
        # are verified by the destination readback itself: the readback IS
        # the outcome check. The presence-request gate below only applies to
        # policy-presence verifications, where "policy exists" must not stand
        # in for a richer requested outcome.
        if not expected.get("policy_number"):
            return result
        payload = job.get('payload') or {}
        request = str(payload.get('request_text') or payload.get('text') or payload.get('prompt') or '')
        match = presence_request(request)
        covered = bool(match and match.group(1) == expected.get('policy_number') and
                       match.group(2) == str(expected.get('applicant_id')))
        if covered:
            return result
        return replace(result, verified=False, retryable=False,
            error='Policy/document presence was checked, but this check does not establish every requested outcome. Review the checked facts and remaining task requirements.')
