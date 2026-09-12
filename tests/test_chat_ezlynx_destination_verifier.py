"""Tests for HermesChatEzlynxDestinationVerifier.

The first test is the one that matters: a job that posted a note about work
it never did must NOT verify. Everything else is supporting.
"""

import unittest

from robie_job_engine.chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier

BOND_POLICY = "73834086"
BOND_APPLICANT = "194066748"


class FakePort:
    """Configurable stand-in for the real EZLynx API client."""

    def __init__(self, policies=None, documents=None, discussions=None, raise_on=None):
        self._policies = policies if policies is not None else []
        self._documents = documents if documents is not None else []
        self._discussions = discussions if discussions is not None else []
        self._raise_on = raise_on or set()

    def policy_by_number(self, policy_number):
        if "policy" in self._raise_on:
            raise ConnectionError("PolicyApi unreachable")
        return {"status": "success", "data": {"Policies": self._policies}}

    def documents_for_applicant(self, applicant_id, policy_id=0):
        if "documents" in self._raise_on:
            raise ConnectionError("DocumentApi search unreachable")
        return self._documents

    def download_document(self, document_id):
        if "download" in self._raise_on:
            raise ConnectionError("DocumentApi download unreachable")
        for doc in self._documents:
            if str(doc.get("id") or "") == str(document_id):
                return doc.get("body") or b"%PDF-1.4 test"
        raise FileNotFoundError(document_id)

    def discussions_for_applicant(self, applicant_id):
        if "discussions" in self._raise_on:
            raise ConnectionError("discussions unreachable")
        return self._discussions


def job(applicant=BOND_APPLICANT, policy=BOND_POLICY):
    return {
        "id": "85f5eae0",
        "action_type": "hermes.google_chat_task",
        "created_at": "2026-09-10T22:00:00+00:00",
        "payload": {"applicant_id": applicant, "policy_number": policy},
    }


def action(**kw):
    dest = {
        "applicant_id": BOND_APPLICANT,
        "policy_number": BOND_POLICY,
        "discussion_title": "Bond",
        "document_names": [],
    }
    dest.update(kw)
    return {"destination": dest, "detail": {}}


def real_policy_row():
    return {
        "PolicyNumber": BOND_POLICY,
        "ApplicantId": BOND_APPLICANT,
        "CarrierName": "Western Surety/CNA",
        "LineOfBusiness": "Bonds Misc",
    }


class VerifierTests(unittest.TestCase):

    # -- THE RULE THIS FILE EXISTS FOR -------------------------------- #

    def test_a_note_alone_never_verifies(self):
        """ROBIE posted the note but never created the policy. Must fail."""
        port = FakePort(
            policies=[],                                   # policy does NOT exist
            discussions=[{"title": "Bond"}],               # but the note does
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertFalse(r.verified, "a self-posted note must never verify the work")
        self.assertIn("not present in EZLynx", r.error)

    def test_note_presence_is_recorded_but_flagged_as_receipt(self):
        port = FakePort(policies=[real_policy_row()], discussions=[{"title": "Bond"}])
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertTrue(r.verified)
        self.assertTrue(r.evidence.observed["discussion_receipt_present"])
        self.assertTrue(r.evidence.observed["note_is_receipt_not_evidence"])

    def test_duplicate_discussion_titles_are_observed_not_fatal(self):
        port = FakePort(
            policies=[real_policy_row()],
            discussions=[{"title": "Bond"}, {"title": "Bond"}],
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertTrue(r.verified)
        self.assertEqual(r.evidence.observed["discussion_duplicate_titles"], 2)

    def test_missing_note_does_not_block_a_real_policy(self):
        """The note is a receipt. Its absence is not a verification failure."""
        port = FakePort(policies=[real_policy_row()], discussions=[])
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertTrue(r.verified)
        self.assertFalse(r.evidence.observed["discussion_receipt_present"])

    # -- the Bond case, end to end ------------------------------------ #

    def test_bond_job_verifies_when_policy_and_document_are_real(self):
        port = FakePort(
            policies=[real_policy_row()],
            documents=[{"id": "818921949", "name": "Bond - Western Surety.pdf"}],
            discussions=[{"title": "Bond"}],
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(), action(document_names=["Bond - Western Surety.pdf"])
        )
        self.assertTrue(r.verified)
        self.assertTrue(r.evidence.authoritative)
        self.assertEqual(r.evidence.method, "EZLYNX_API_DESTINATION_READBACK")
        self.assertEqual(r.evidence.source, "ezlynx-policyapi+documentapi")
        self.assertEqual(r.evidence.observed["document_ids"], ["818921949"])
        self.assertIsNone(r.error)

    def test_document_url_without_id_never_verifies(self):
        port = FakePort(
            policies=[real_policy_row()],
            documents=[{
                "name": "Bond - Western Surety.pdf",
                "documentUrl": "https://old-wrong.example/doc/1",
            }],
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(), action(document_names=["Bond - Western Surety.pdf"])
        )
        self.assertFalse(r.verified)
        self.assertIn("DocumentApi", r.error)

    def test_document_download_failure_is_retryable(self):
        port = FakePort(
            policies=[real_policy_row()],
            documents=[{"id": "818921949", "name": "Bond - Western Surety.pdf"}],
            raise_on={"download"},
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(), action(document_names=["Bond - Western Surety.pdf"])
        )
        self.assertFalse(r.verified)
        self.assertTrue(r.retryable)
        self.assertIn("download", r.error)

    def test_document_claimed_but_absent_fails(self):
        port = FakePort(
            policies=[real_policy_row()],
            documents=[{"name": "something else.pdf"}],
        )
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(), action(document_names=["Bond - Western Surety.pdf"])
        )
        self.assertFalse(r.verified)
        self.assertIn("document", r.error)

    # -- the worker may not choose its own subject --------------------- #

    def test_worker_cannot_redirect_to_another_applicant(self):
        port = FakePort(policies=[real_policy_row()])
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(applicant="194066748"), action(applicant_id="999999999")
        )
        self.assertFalse(r.verified)
        self.assertIn("different applicant", r.error)
        self.assertFalse(r.evidence.authoritative)

    def test_policy_for_a_different_applicant_does_not_count(self):
        row = real_policy_row()
        row["ApplicantId"] = "111111111"
        port = FakePort(policies=[row])
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertFalse(r.verified)

    # -- fail closed, and say which kind of failure -------------------- #

    def test_api_unreachable_is_retryable_and_not_authoritative(self):
        port = FakePort(raise_on={"policy"})
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertFalse(r.verified)
        self.assertTrue(r.retryable, "a transient read failure must be retryable")
        self.assertFalse(r.evidence.authoritative, "unknown state is never authoritative")

    def test_confirmed_absence_is_authoritative_and_not_retryable(self):
        """We looked and it genuinely is not there — that is a real answer."""
        port = FakePort(policies=[])
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertFalse(r.verified)
        self.assertTrue(r.evidence.authoritative)
        self.assertFalse(r.retryable)

    def test_no_policy_number_anywhere_is_blocked_not_guessed(self):
        j = job(policy="")
        j["payload"].pop("policy_number")
        a = action(policy_number="")
        r = HermesChatEzlynxDestinationVerifier(port=FakePort()).verify(j, a)
        self.assertFalse(r.verified)
        self.assertIn("nothing to re-read", r.error)

    def test_document_read_failure_is_retryable_not_a_false_negative(self):
        port = FakePort(policies=[real_policy_row()], raise_on={"documents"})
        r = HermesChatEzlynxDestinationVerifier(port).verify(
            job(), action(document_names=["x.pdf"])
        )
        self.assertFalse(r.verified)
        self.assertTrue(r.retryable)

    def test_discussion_read_failure_does_not_sink_a_verified_policy(self):
        port = FakePort(policies=[real_policy_row()], raise_on={"discussions"})
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertTrue(r.verified, "a receipt lookup failing must not fail the job")
        self.assertIsNone(r.evidence.observed["discussion_receipt_present"])

    # -- envelope shapes ------------------------------------------------ #

    def test_handles_alternate_response_containers(self):
        v = HermesChatEzlynxDestinationVerifier
        for container in ("Policies", "Results", "Items", "results"):
            port = FakePort()
            port.policy_by_number = lambda n, c=container: {
                "status": "success", "data": {c: [real_policy_row()]}
            }
            self.assertTrue(v(port).verify(job(), action()).verified, container)

    def test_error_envelope_is_treated_as_not_found(self):
        port = FakePort()
        port.policy_by_number = lambda n: {"status": "error", "code": 401, "error": "nope"}
        r = HermesChatEzlynxDestinationVerifier(port).verify(job(), action())
        self.assertFalse(r.verified)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class ApplicantIdentityTests(unittest.TestCase):
    def test_policy_without_applicant_cannot_verify_bound_job(self):
        row = real_policy_row()
        del row['ApplicantId']
        result = HermesChatEzlynxDestinationVerifier(FakePort(policies=[row])).verify(job(), action())
        self.assertFalse(result.verified)
