"""Tests for robie_job_engine.ezlynx_writers (verified notes + document uploads)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["EZLYNX_WRITE_APPLICANT_IDS"] = "220250093, 999000111"

from robie_job_engine import ezlynx_writers as writers  # noqa: E402
from robie_job_engine.ezlynx_api import EzlynxApiConfigurationError  # noqa: E402
from robie_job_engine.ezlynx_write_verify import EzlynxWriteVerifyError  # noqa: E402

APPLICANT = "999000111"
POLICY = "PAC00001215485"
NAME = "Green Lion Lawn Care LLC"


def _policy_payload(owner_applicant=APPLICANT, insured=NAME):
    return {
        "status": "success",
        "data": {
            "Policies": [
                {
                    "PolicyNumber": POLICY,
                    "ApplicantId": owner_applicant,
                    "InsuredName": insured,
                }
            ]
        },
    }


class FakeClient:
    """Stands in for EzlynxApiClient; records call order."""

    def __init__(self, *, policy_payload=None, documents=None):
        self.calls = []
        self._policy_payload = policy_payload
        self._documents = documents if documents is not None else []
        self.upload_calls = []

    def search_policy_by_number(self, policy_number):
        self.calls.append(("search_policy", policy_number))
        return self._policy_payload

    def search_applicant_documents(self, applicant_id):
        self.calls.append(("search_documents", applicant_id))
        results = []
        for doc in self._documents:
            if isinstance(doc, dict) and doc.get("id"):
                results.append(
                    {
                        "id": doc["id"],
                        "name": doc.get("name") or doc.get("DocumentName") or "",
                    }
                )
        return {"Documents": self._documents, "results": results}

    def upload_applicant_document(self, applicant_id, document_name, file_bytes, **kwargs):
        self.calls.append(("upload", applicant_id, document_name))
        self.upload_calls.append((applicant_id, document_name))
        self._documents.append({"id": "12345", "name": document_name})
        return "12345"

    def post_json(self, path, payload):
        self.calls.append(("post_json", path))
        self.posted_path = path
        self.posted_payload = payload
        return {"id": "n1", "ok": True}

    def get_token(self):
        return "tok"


def _writes_before_post(calls):
    kinds = [c[0] for c in calls]
    return kinds.index("search_policy") < kinds.index("post_json")


def test_post_note_happy_path_verifies_first():
    client = FakeClient(policy_payload=_policy_payload())
    result = writers.post_note(
        APPLICANT,
        "Renewal Manual / Submission Center",
        "Renewal docs pulled from carrier portal.",
        expected_policy_number=POLICY,
        expected_name=NAME,
        client=client,
    )
    assert result["verification"]["verified"] is True
    assert result["note_response"] == {"id": "n1", "ok": True}
    assert client.posted_path == writers.DISCUSSION_NOTE_POST_PATH
    assert client.posted_payload["applicantId"] == APPLICANT
    assert client.posted_payload["title"] == "Renewal Manual / Submission Center"
    assert client.posted_payload["policyNumber"] == POLICY
    assert _writes_before_post(client.calls)


def test_post_note_refused_never_touches_network():
    client = FakeClient(policy_payload=_policy_payload(owner_applicant="220250093"))
    with pytest.raises(EzlynxWriteVerifyError, match="check 2"):
        writers.post_note(
            APPLICANT,
            "t",
            "b",
            expected_policy_number=POLICY,
            expected_name=NAME,
            client=client,
        )
    assert [c[0] for c in client.calls] == ["search_policy"]


def test_post_note_name_mismatch_refuses():
    client = FakeClient(policy_payload=_policy_payload(insured="Some Other Company LLC"))
    with pytest.raises(EzlynxWriteVerifyError, match="check 3"):
        writers.post_note(
            APPLICANT, "t", "b", expected_policy_number=POLICY, expected_name=NAME, client=client
        )
    assert not any(c[0] == "post_json" for c in client.calls)


def test_post_note_requires_title_and_body():
    client = FakeClient(policy_payload=_policy_payload())
    with pytest.raises(ValueError):
        writers.post_note(APPLICANT, "", "b", client=client)
    with pytest.raises(ValueError):
        writers.post_note(APPLICANT, "t", "  ", client=client)


def test_post_note_without_client_and_without_config_fails_closed():
    with pytest.raises(EzlynxApiConfigurationError):
        writers.post_note(APPLICANT, "t", "b", expected_policy_number=POLICY, expected_name=NAME)


def test_upload_document_happy_path():
    client = FakeClient(policy_payload=_policy_payload())
    result = writers.upload_document(
        APPLICANT,
        "Renewal Offer - Green Lion.pdf",
        b"%PDF-1.4 fake",
        expected_policy_number=POLICY,
        expected_name=NAME,
        client=client,
    )
    assert result["document_id"] == "12345"
    assert result["read_back"] is True
    assert result["verification"]["verified"] is True
    assert client.upload_calls == [(APPLICANT, "Renewal Offer - Green Lion.pdf")]
    kinds = [c[0] for c in client.calls]
    assert kinds.index("search_policy") < kinds.index("upload")


def test_upload_document_refused_never_uploads():
    client = FakeClient(policy_payload=_policy_payload(owner_applicant="220250093"))
    with pytest.raises(EzlynxWriteVerifyError):
        writers.upload_document(
            APPLICANT,
            "doc.pdf",
            b"bytes",
            expected_policy_number=POLICY,
            expected_name=NAME,
            client=client,
        )
    assert client.upload_calls == []


def test_coi_path_no_policy_number_docs_corroborate():
    docs = [{"DocumentName": "COI - Green Lion Lawn Care LLC.pdf", "PolicyNumber": ""}]
    client = FakeClient(policy_payload=None, documents=docs)
    result = writers.post_note(
        APPLICANT, "COI request", "Certificate issued.", expected_name=NAME, client=client
    )
    assert result["verification"]["verified"] is True
    checks = result["verification"]["checks"]
    assert checks["policy_cross_reference"]["skipped"] is True
    assert checks["name_cross_reference"]["passed"] is True


def test_coi_path_docs_disagree_refuses():
    docs = [{"DocumentName": "COI - Some Other Company LLC.pdf", "PolicyNumber": ""}]
    client = FakeClient(policy_payload=None, documents=docs)
    with pytest.raises(EzlynxWriteVerifyError, match="check 3|check 4"):
        writers.post_note(
            APPLICANT, "COI request", "Certificate issued.", expected_name=NAME, client=client
        )
    assert not any(c[0] == "post_json" for c in client.calls)


def test_find_policy_record_shapes():
    rec = {"PolicyNumber": POLICY, "ApplicantId": APPLICANT}
    assert writers.find_policy_record({"Policies": [rec]}, POLICY) == rec
    assert writers.find_policy_record({"status": "success", "data": {"policies": [rec]}}, POLICY) == rec
    assert writers.find_policy_record({"status": "success", "data": [rec]}, POLICY) == rec
    assert writers.find_policy_record({"Policies": []}, POLICY) is None
    assert writers.find_policy_record(None, POLICY) is None


def test_allowlist_still_gates_writers():
    client = FakeClient(policy_payload=_policy_payload())
    with pytest.raises(EzlynxWriteVerifyError, match="check 1"):
        writers.post_note(
            "000000000", "t", "b", expected_policy_number=POLICY, expected_name=NAME, client=client
        )
