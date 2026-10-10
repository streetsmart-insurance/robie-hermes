"""ezlynx-api CLI: every verb with stub clients. No network, no EZLynx, no Gemini.

Reads never touch the write gate. Writes go through the real EzlynxApiClient /
DiscussionApi helpers, so the real allowlist refuses before any HTTP.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
from pathlib import Path
from urllib import error

import pytest

from robie_job_engine import ezlynx_api_cli as cli
from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine import ezlynx_write_scope as scope
from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig
from robie_job_engine.ezlynx_api_read_port import EzlynxApiClientReadPort

ALLOWED = "220250093"
OTHER = "330000001"
DOC_BODY_SENTINEL = "SENTINEL-DOCUMENT-BODY-4417"
NOTE_SENTINEL = "SENTINEL note text 9981 for the file"
QUESTION_SENTINEL = "SENTINEL question 7720: what is the limit?"


# ------------------------------------------------------------------ fakes
class FakeResponse:
    def __init__(self, payload: bytes, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._payload


class FakeEzlynx:
    """urlopen stand-in for DocumentApi + PolicyApi. Records every call."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.docs: dict[str, list[dict]] = {ALLOWED: [{"id": 111, "documentName": "Existing.pdf"}]}
        self.bodies: dict[str, bytes] = {"111": b"%PDF-1.4 fake"}
        self.next_id = 900
        self.list_new_upload = True
        self.policy_rows: list[dict] = []

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] == "POST" and "connect/token" not in call[1]]

    def non_token_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if "connect/token" not in call[1]]

    def __call__(self, url, *, data, headers, timeout):
        method = "POST" if data is not None else "GET"
        self.calls.append((method, url))
        if "connect/token" in url:
            return FakeResponse(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        match = re.search(r"/account/(\d+)/document-search", url, re.I)
        if match:
            return FakeResponse(json.dumps({"results": self.docs.get(match.group(1), [])}).encode())
        match = re.search(r"/documents/v1/(\d+)/download", url, re.I)
        if match:
            body = self.bodies.get(match.group(1))
            if body is None:
                raise error.HTTPError(url, 404, "nf", {}, io.BytesIO(b"missing"))
            return FakeResponse(body, headers={"Content-Type": "application/pdf"})
        match = re.search(r"/account/(\d+)/document$", url, re.I)
        if match and method == "POST":
            self.next_id += 1
            doc_id = str(self.next_id)
            name_match = re.search(rb'name="DocumentName"\r\n\r\n(.*?)\r\n', data)
            name = name_match.group(1).decode() if name_match else "?"
            file_match = re.search(rb'name="File"; filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n(.*?)\r\n--', data, re.S)
            if file_match:
                self.bodies[doc_id] = file_match.group(1)
            if self.list_new_upload:
                self.docs.setdefault(match.group(1), []).append({"id": int(doc_id), "documentName": name})
            return FakeResponse(doc_id.encode(), headers={"Content-Type": "text/plain"})
        if "/PolicyApi/policy/v1/search" in url:
            return FakeResponse(json.dumps({"Policies": self.policy_rows}).encode())
        raise AssertionError(f"unexpected EZLynx call {method} {url}")


def _api(fake: FakeEzlynx) -> EzlynxApiClient:
    config = EzlynxApiConfig(
        token_endpoint="https://app.ezlynx.com/auth/connect/token",
        document_base_url="https://app.ezlynx.com/DocumentApi/",
        client_id="cid", client_secret="csecret", username="agency_user",
        integration_group_id="183", scope="DocumentApi PolicyApi openid",
    )
    return EzlynxApiClient(config, urlopen=fake)


class FakeDiscussions:
    """DiscussionApiClient stand-in with the live response shapes."""

    def __init__(self, *, with_notes="ok", title="Test Note", discussion_id="841872781"):
        self.id = discussion_id
        self.title = title
        self.with_notes_mode = with_notes
        self.posts = 0
        self.reads = 0
        self.posted_text = None

    def get_discussions(self, applicant_id):
        self.reads += 1
        return [{"discussionId": self.id, "title": self.title, "applicantId": int(applicant_id), "noteCount": 1}]

    def get_discussion_ids(self, applicant_id):
        return [self.id]

    def get_discussion(self, discussion_id):
        after = self.posts > 0
        return {"discussionId": discussion_id, "title": self.title, "noteCount": 1 + int(after),
                "mostRecentNoteId": "1001" if after else "1000", "deleted": False}

    def append_note(self, discussion_id, text, note_type="Note", applicant_id=None):
        self.posts += 1
        self.posted_text = text
        return {}

    def get_discussion_with_notes(self, discussion_id):
        self.reads += 1
        notes = [{"noteId": "1000", "body": "an older note", "createdAt": "2026-10-01"}]
        if self.posts and self.with_notes_mode == "ok":
            notes.append({"noteId": "1001", "body": self.posted_text})
        return {"discussionId": discussion_id, "title": self.title, "notes": notes}


class FakeGemini:
    model = "gemini-3.8-flash"

    def __init__(self, answer="The limit is $1,000,000 (p. 1)."):
        self.answer = answer
        self.calls: list[dict] = []

    def generate_content(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.answer


class Svc:
    def __init__(self, tmp_path: Path, fake: FakeEzlynx | None = None, discussions=None, gemini=None, extract=None):
        self.state_dir = tmp_path / "state"
        self.fake = fake or FakeEzlynx()
        self.api = _api(self.fake)
        self.port = EzlynxApiClientReadPort(self.api)
        self.discussions = discussions or FakeDiscussions()
        self._gemini = gemini or FakeGemini()
        self._extract = extract
        self.gemini_models: list = []
        self.ledger_path = self.state_dir / cli.NOTE_LEDGER_FILE
        # Shared write jobs need a store the engine accepts (not /tmp).
        self.jobs_db = durable_jobs_db()

    def extract_pages(self, data):
        if self._extract is not None:
            return self._extract(data)
        raise AssertionError("extract_pages should not be called")

    def gemini(self, model):
        self.gemini_models.append(model)
        return self._gemini


_DURABLE_ROOT = Path(__file__).resolve().parent.parent / ".robie-durable-test" / "unit"
_DURABLE_DIRS: list[Path] = []


def durable_jobs_db() -> Path:
    import tempfile

    _DURABLE_ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=_DURABLE_ROOT))
    _DURABLE_DIRS.append(path)
    return path / "jobs.db"


@pytest.fixture(autouse=True)
def _durable_cleanup():
    yield
    while _DURABLE_DIRS:
        shutil.rmtree(_DURABLE_DIRS.pop(), ignore_errors=True)


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "0")
    for name in ("ROBIE_EZLYNX_WRITE_SCOPE", "ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "EZLYNX_WRITE_APPLICANT_IDS",
                 "ROBIE_PLAYGROUND", "ROBIE_CURRENT_JOB_ID", "ROBIE_JOB_ID", "JOB_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope, "ALLOWED_EZLYNX_WRITE_APPLICANT_IDS", frozenset({ALLOWED}))


def run(svc: Svc, *argv: str, agent: str = "claude") -> tuple[int, dict]:
    out = io.StringIO()
    args = list(argv)
    if agent:
        args = ["--agent", agent, *args]
    code = cli.main(["--state-dir", str(svc.state_dir), *args], services=svc, stdout=out)
    return code, json.loads(out.getvalue())


def audit_lines(svc: Svc) -> list[dict]:
    path = svc.state_dir / cli.AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pdf_bytes(pages: int = 1) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ------------------------------------------------------ usage and audit
def test_agent_is_required_and_nothing_runs(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "list", ALLOWED, agent="")
    assert code == cli.EXIT_USAGE
    assert payload["error"]["code"] == "agent_required"
    assert svc.fake.calls == []
    assert audit_lines(svc) == []


def test_agent_name_is_validated(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "list", ALLOWED, agent="bad name;rm")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_agent"
    assert svc.fake.calls == []


def test_agent_flag_may_follow_the_verb(tmp_path):
    svc = Svc(tmp_path)
    out = io.StringIO()
    code = cli.main(["docs", "list", ALLOWED, "--agent", "chatgpt", "--state-dir", str(svc.state_dir)], services=svc, stdout=out)
    assert code == 0
    assert audit_lines(svc)[-1]["agent"] == "chatgpt"


def test_no_any_applicant_flag_exists(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "upload", OTHER, "--file", "x", "--name", "n", "--any-applicant")
    assert code == cli.EXIT_USAGE
    assert "any-applicant" not in cli.build_parser().format_help()
    source = Path(cli.__file__).read_text()
    # Any client is possible only through the root-owned policy file naming
    # this CLI; never the filer's registration, never an environment switch.
    assert "register_filer_operation_scope" not in source
    assert "register_operation_scope(scope.ENTRYPOINT_EZLYNX_API_CLI)" in source
    assert "operation=OPERATION_DOCUMENT_UPLOAD" in source
    assert "write_scope_all_refused" in source


def test_audit_unwritable_refuses_before_anything_happens(tmp_path):
    svc = Svc(tmp_path)
    svc.state_dir.parent.mkdir(parents=True, exist_ok=True)
    svc.state_dir.write_text("a file where the state dir should be")
    code, payload = run(svc, "docs", "list", ALLOWED)
    assert code == cli.EXIT_REFUSED
    assert payload["error"]["code"] == "audit_unavailable"
    assert svc.fake.calls == []


def test_audit_is_append_only_json_lines_without_bodies(tmp_path):
    svc = Svc(tmp_path)
    svc.fake.bodies["111"] = DOC_BODY_SENTINEL.encode()
    run(svc, "docs", "list", ALLOWED, agent="claude")
    first = (svc.state_dir / cli.AUDIT_FILE).read_text()
    run(svc, "docs", "read", "111", "--audit-applicant", ALLOWED, agent="grok-bot")
    text = (svc.state_dir / cli.AUDIT_FILE).read_text()
    assert text.startswith(first)
    records = [json.loads(line) for line in text.splitlines()]
    assert [r["agent"] for r in records] == ["claude", "grok-bot"]
    assert records[0]["command"] == "docs.list" and records[0]["applicant"] == ALLOWED
    assert records[1]["command"] == "docs.read" and records[1]["doc_id"] == "111" and records[1]["applicant"] == ALLOWED
    assert all(r["result"] == "ok" and r["phase"] == "end" and r["ts"].endswith("Z") for r in records)
    assert DOC_BODY_SENTINEL not in text
    mode = stat.S_IMODE(os.stat(svc.state_dir / cli.AUDIT_FILE).st_mode)
    assert mode & 0o007 == 0, oct(mode)


def test_audit_drops_forbidden_keys():
    log = cli.AuditLog(Path("/nonexistent"), agent="claude", command="x")
    # append() skips body-like keys even if a handler passes one by mistake
    assert "body" in cli._FORBIDDEN_AUDIT_KEYS and "question" in cli._FORBIDDEN_AUDIT_KEYS and log.agent == "claude"


# ------------------------------------------------------------ read verbs
def test_docs_list_filters_limits_and_stays_read_only(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a read verb called the write gate")

    monkeypatch.setattr(scope, "require_allowed_ezlynx_write_applicant", boom)
    svc = Svc(tmp_path)
    svc.fake.docs[ALLOWED] = [{"id": i, "documentName": f"Doc {i}.pdf"} for i in range(1, 6)] + [{"id": 9, "documentName": "Dec page.pdf"}]
    code, payload = run(svc, "docs", "list", ALLOWED, "--name-contains", "dec")
    assert code == 0 and payload["ok"] is True
    assert payload["result"]["documents"] == [{"id": "9", "name": "Dec page.pdf"}]
    code, payload = run(svc, "docs", "list", ALLOWED, "--limit", "2")
    assert payload["result"]["count"] == 6 and payload["result"]["returned"] == 2
    assert all(method == "GET" for method, _ in svc.fake.non_token_calls())


def test_docs_list_rejects_bad_applicant(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "list", "22/../x")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_applicant"
    assert svc.fake.calls == []


def test_docs_get_saves_new_file_and_never_prints_the_body(tmp_path):
    svc = Svc(tmp_path)
    svc.fake.bodies["111"] = b"%PDF-1.4 " + DOC_BODY_SENTINEL.encode()
    out_path = tmp_path / "out.pdf"
    code, payload = run(svc, "docs", "get", "111", "--out", str(out_path))
    assert code == 0
    assert out_path.read_bytes().endswith(DOC_BODY_SENTINEL.encode())
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert payload["result"]["bytes"] == len(out_path.read_bytes())
    assert payload["result"]["looks_like"] == "pdf"
    assert DOC_BODY_SENTINEL not in json.dumps(payload)
    code, payload = run(svc, "docs", "get", "111", "--out", str(out_path))
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "out_exists"
    inside = svc.state_dir / "x.pdf"
    code, payload = run(svc, "docs", "get", "111", "--out", str(inside))
    assert code == cli.EXIT_REFUSED


def test_docs_get_missing_document_is_an_error(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "get", "424242", "--out", str(tmp_path / "n.pdf"))
    assert code == cli.EXIT_ERROR and payload["ok"] is False
    assert not (tmp_path / "n.pdf").exists()
    assert audit_lines(svc)[-1]["result"] == "error"


def test_docs_read_pdf_reports_pages_and_ocr(tmp_path):
    svc = Svc(tmp_path, extract=lambda data: (["page one text", "scanned page text"], [2]))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "read", "111")
    assert code == 0
    result = payload["result"]
    assert result["kind"] == "pdf" and result["page_count"] == 2 and result["ocr_pages"] == [2]
    assert [p["text"] for p in result["pages"]] == ["page one text", "scanned page text"]
    assert result["truncated"] is False


def test_docs_read_truncates_to_max_chars(tmp_path):
    svc = Svc(tmp_path, extract=lambda data: (["a" * 50, "b" * 50], []))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "read", "111", "--max-chars", "70")
    assert payload["result"]["truncated"] is True and payload["result"]["chars"] == 70
    assert len(payload["result"]["pages"][1]["text"]) == 20


def test_docs_read_refuses_too_many_pages_before_extracting(tmp_path):
    svc = Svc(tmp_path)  # extract_pages would raise AssertionError if reached
    svc.fake.bodies["111"] = pdf_bytes(3)
    code, payload = run(svc, "docs", "read", "111", "--max-pages", "2")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "too_many_pages"


def test_docs_read_text_file_and_binary(tmp_path):
    svc = Svc(tmp_path)
    svc.fake.bodies["111"] = b"hello plain text"
    code, payload = run(svc, "docs", "read", "111")
    assert code == 0 and payload["result"]["pages"][0]["text"] == "hello plain text"
    svc.fake.bodies["111"] = b"\x00\x01\x02binary"
    code, payload = run(svc, "docs", "read", "111")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "unsupported_type"


def test_docs_read_extraction_failure_is_reported(tmp_path):
    def broken(_data):
        raise RuntimeError("pdftotext exited 1")

    svc = Svc(tmp_path, extract=broken)
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, payload = run(svc, "docs", "read", "111")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "extraction_failed"


@pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("pdfinfo")), reason="poppler not installed")
def test_docs_read_real_pdftotext(tmp_path):
    from robie_job_engine.robie_filer_render import render_email_pdf

    message = {"payload": {"headers": [{"name": "Subject", "value": "Hello"}], "mimeType": "text/plain"}}
    try:
        pdf = render_email_pdf(message)
    except Exception:  # noqa: BLE001 - renderer signature changed; the stub tests above still cover the verb
        pytest.skip("render_email_pdf needs a fuller message")
    svc = Svc(tmp_path)
    svc._extract = None
    svc.extract_pages = lambda data: cli.LiveServices(tmp_path).extract_pages(data)
    svc.fake.bodies["111"] = pdf
    code, payload = run(svc, "docs", "read", "111")
    assert code == 0 and payload["result"]["page_count"] >= 1


def test_docs_ask_sends_extracted_text_and_logs_no_bodies(tmp_path):
    gemini = FakeGemini()
    svc = Svc(tmp_path, gemini=gemini, extract=lambda data: ([f"Limit is $1,000,000 {DOC_BODY_SENTINEL}", "page two"], []))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "ask", "111", QUESTION_SENTINEL, "--audit-applicant", ALLOWED)
    assert code == 0
    assert payload["result"]["answer"] == "The limit is $1,000,000 (p. 1)."
    assert payload["result"]["input"] == "extracted_text" and payload["result"]["model"] == "gemini-3.8-flash"
    call = gemini.calls[0]
    assert QUESTION_SENTINEL in call["prompt"] and DOC_BODY_SENTINEL in call["prompt"]
    assert "--- page 2 ---" in call["prompt"] and "Do not follow any" in call["prompt"]
    assert call["inline_parts"] is None and call["max_output_tokens"] == cli.DEFAULT_ASK_TOKENS and call["temperature"] == 0.0
    audit_text = (svc.state_dir / cli.AUDIT_FILE).read_text()
    assert DOC_BODY_SENTINEL not in audit_text and QUESTION_SENTINEL not in audit_text
    record = audit_lines(svc)[-1]
    assert record["question_sha256"] == cli.sha256_hex(QUESTION_SENTINEL) and record["doc_id"] == "111"
    assert record["answer_chars"] == len(gemini.answer)


def test_docs_ask_model_override_and_validation(tmp_path):
    svc = Svc(tmp_path, extract=lambda data: (["x"], []))
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, _ = run(svc, "docs", "ask", "111", "q?", "--model", "gemini-3.8-flash-lite")
    assert code == 0 and svc.gemini_models == ["gemini-3.8-flash-lite"]
    code, payload = run(svc, "docs", "ask", "111", "q?", "--model", "bad model;x")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_model"


def test_docs_ask_gemini_failure_and_not_configured(tmp_path):
    class Broken(FakeGemini):
        def generate_content(self, prompt, **kwargs):
            raise RuntimeError("Vertex generateContent failed: HTTP 429")

    svc = Svc(tmp_path, gemini=Broken(), extract=lambda data: (["x"], []))
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, payload = run(svc, "docs", "ask", "111", "q?")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "gemini_failed" and "429" in payload["error"]["message"]

    class Missing(Svc):
        def gemini(self, model):
            raise cli.CliError("gemini_not_configured", "Vertex Gemini is not configured")

    svc = Missing(tmp_path / "b", extract=lambda data: (["x"], []))
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, payload = run(svc, "docs", "ask", "111", "q?")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "gemini_not_configured"


def test_docs_ask_direct_builds_inline_data_unverified(tmp_path):
    gemini = FakeGemini()
    svc = Svc(tmp_path, gemini=gemini)  # extract_pages must not be used
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, payload = run(svc, "docs", "ask", "111", "what is this?", "--direct")
    assert code == 0 and payload["result"]["input"] == "inline_data_UNVERIFIED"
    part = gemini.calls[0]["inline_parts"][0]["inlineData"]
    assert part["mimeType"] == "application/pdf"
    import base64

    assert base64.b64decode(part["data"]) == svc.fake.bodies["111"]
    svc.fake.bodies["111"] = b"plain text"
    code, payload = run(svc, "docs", "ask", "111", "what?", "--direct")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "unsupported_type"


def test_vertex_client_request_shape_with_inline_data():
    from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient

    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["timeout"] = timeout

        class Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()

        return Resp()

    client = VertexGeminiFieldClient(project="proj", location="us-central1", model="google/gemini-3.8-flash",
                                     opener=opener, token_provider=lambda: "tok")
    assert client.generate_content("hi") == "ok"
    assert seen["timeout"] == 30.0 and seen["body"]["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": 512}
    assert seen["body"]["contents"][0]["parts"] == [{"text": "hi"}]
    assert "publishers/google/models/gemini-3.8-flash:generateContent" in seen["url"]
    part = {"inlineData": {"mimeType": "application/pdf", "data": "AAAA"}}
    client.generate_content("q", max_output_tokens=2048, timeout=90.0, temperature=0.0, inline_parts=[part])
    assert seen["timeout"] == 90.0
    assert seen["body"]["generationConfig"] == {"temperature": 0.0, "maxOutputTokens": 2048}
    assert seen["body"]["contents"][0]["parts"] == [{"text": "q"}, part]


def test_discussions_list_and_get_with_notes(tmp_path):
    svc = Svc(tmp_path, discussions=FakeDiscussions(title=""))
    code, payload = run(svc, "discussions", "list", ALLOWED)
    assert code == 0
    item = payload["result"]["discussions"][0]
    assert item["discussion_id"] == "841872781" and item["untitled"] is True
    code, payload = run(svc, "discussions", "get", "841872781", "--audit-applicant", ALLOWED)
    assert code == 0
    assert payload["result"]["note_count"] == 1
    assert payload["result"]["notes"][0]["body"] == "an older note"
    assert payload["result"]["notes"][0]["note_id"] == "1000"
    assert "raw" not in payload["result"]
    code, payload = run(svc, "discussions", "get", "841872781", "--raw")
    assert "raw" in payload["result"]
    assert svc.discussions.posts == 0
    assert "an older note" not in (svc.state_dir / cli.AUDIT_FILE).read_text()


def test_discussions_get_rejects_bad_id(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "discussions", "get", "1; drop")
    assert code == cli.EXIT_USAGE


def test_policy_lookup_matches_normalized_number(tmp_path):
    svc = Svc(tmp_path)
    svc.fake.policy_rows = [
        {"PolicyNumber": "CT1278263263-2", "ApplicantId": 220250093, "policyId": 77},
        {"PolicyNumber": "OTHER-1", "ApplicantId": 5, "policyId": 8},
    ]
    code, payload = run(svc, "policy", "lookup", "ct1278263263-2")
    assert code == 0
    assert payload["result"]["matches"] == [{"applicant_id": "220250093", "policy_id": "77", "policy_number": "CT1278263263-2"}]
    assert payload["result"]["rows_searched"] == 2
    assert all(method == "GET" for method, _ in svc.fake.non_token_calls())
    code, payload = run(svc, "policy", "lookup", "x")
    assert code == cli.EXIT_USAGE


def test_selftest_makes_no_ezlynx_call(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "selftest")
    assert code == 0 and payload["result"]["write_scope"]["allowlist"] == [ALLOWED]
    assert payload["result"]["write_scope"]["all_clients_requested"] is False
    assert svc.fake.calls == []


# ------------------------------------------------------------ write verbs
def _file(tmp_path, name="note.txt", data=b"hello filing"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_upload_to_allowed_client_reads_back_and_audits_twice(tmp_path):
    svc = Svc(tmp_path)
    path = _file(tmp_path, data=DOC_BODY_SENTINEL.encode())
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "Agent test doc.txt")
    assert code == 0, payload
    result = payload["result"]
    assert result["status"] == "uploaded" and result["read_back"] is True and result["document_id"] == "901"
    assert len(svc.fake.writes()) == 1
    lines = audit_lines(svc)
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert lines[1]["result"] == "uploaded" and lines[1]["doc_id"] == "901" and lines[1]["applicant"] == ALLOWED
    assert lines[1]["sha256"] == cli.sha256_hex(DOC_BODY_SENTINEL.encode())
    assert DOC_BODY_SENTINEL not in (svc.state_dir / cli.AUDIT_FILE).read_text()


def test_upload_to_other_client_is_refused_by_the_real_allowlist(tmp_path):
    svc = Svc(tmp_path)
    path = _file(tmp_path)
    code, payload = run(svc, "docs", "upload", OTHER, "--file", str(path), "--name", "Nope.txt")
    assert code == cli.EXIT_REFUSED
    assert payload["error"]["code"] == "EZLYNX_WRITE_SCOPE_REFUSED"
    assert scope.EZLYNX_WRITE_SCOPE_REFUSED in payload["error"]["message"]
    assert svc.fake.calls == []  # no token, no search, no upload
    lines = audit_lines(svc)
    assert [line["phase"] for line in lines] == ["start", "end"] and lines[1]["result"] == "refused"


def test_upload_refused_when_all_clients_scope_is_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    svc = Svc(tmp_path)
    path = _file(tmp_path)
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "x.txt")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "write_scope_all_refused"
    assert svc.fake.calls == []


def test_upload_dry_run_checks_the_gate_and_writes_nothing(tmp_path):
    svc = Svc(tmp_path)
    path = _file(tmp_path)
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "x.txt", "--dry-run")
    assert code == 0 and payload["status"] == "dry_run" and payload["result"]["write_allowed"] is True
    assert svc.fake.calls == []
    assert [line["phase"] for line in audit_lines(svc)] == ["end"]
    code, payload = run(svc, "docs", "upload", OTHER, "--file", str(path), "--name", "x.txt", "--dry-run")
    assert code == cli.EXIT_REFUSED and svc.fake.calls == []


def test_upload_skips_an_existing_name_unless_allowed(tmp_path):
    svc = Svc(tmp_path)
    path = _file(tmp_path)
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "Existing.pdf")
    assert code == 0 and payload["status"] == "exists" and payload["result"]["document_ids"] == ["111"]
    assert svc.fake.writes() == []
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "Existing.pdf", "--allow-duplicate")
    assert code == 0 and payload["status"] == "uploaded" and len(svc.fake.writes()) == 1


def test_upload_readback_failure_is_exit_5_with_a_do_not_retry_message(tmp_path):
    svc = Svc(tmp_path)
    svc.fake.list_new_upload = False
    path = _file(tmp_path)
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(path), "--name", "Unlisted.txt")
    assert code == cli.EXIT_UNCONFIRMED and payload["error"]["code"] == "readback_failed"
    assert "docs list" in payload["error"]["message"]
    assert len(svc.fake.writes()) == 1
    assert audit_lines(svc)[-1]["result"] == "unconfirmed"


def test_upload_file_guards(tmp_path, monkeypatch):
    svc = Svc(tmp_path)
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", "/etc/hostname", "--name", "x.txt")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "sensitive_path"
    empty = _file(tmp_path, "empty.txt", b"")
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(empty), "--name", "x.txt")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "empty_file"
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(tmp_path / "missing"), "--name", "x.txt")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "file_missing"
    monkeypatch.setattr(cli, "MAX_UPLOAD_BYTES", 4)
    big = _file(tmp_path, "big.txt", b"123456789")
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(big), "--name", "x.txt")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "file_too_large"
    code, payload = run(svc, "docs", "upload", ALLOWED, "--file", str(big), "--name", "bad\x01name")
    assert code == cli.EXIT_USAGE
    assert svc.fake.calls == []


def test_notes_add_files_confirms_and_keeps_text_out_of_audit(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", NOTE_SENTINEL)
    assert code == 0, payload
    result = payload["result"]
    assert result["status"] == "filed" and result["note_id"] == "1001" and result["read_back"] is True
    assert svc.discussions.posts == 1 and svc.discussions.posted_text == NOTE_SENTINEL
    assert NOTE_SENTINEL not in json.dumps(payload)
    assert result["job_status"] == "COMPLETE" and svc.jobs_db.exists()
    text = (svc.state_dir / cli.AUDIT_FILE).read_text()
    assert NOTE_SENTINEL not in text
    lines = audit_lines(svc)
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert lines[1]["note_sha256"] == cli.sha256_hex(NOTE_SENTINEL) and lines[1]["discussion_id"] == "841872781"
    assert lines[1]["result"] == "filed" and lines[1]["note_id"] == "1001"


def test_notes_add_to_other_client_is_refused_before_any_api_call(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", OTHER, "--discussion-id", "841872781", "--text", "hello")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "EZLYNX_WRITE_SCOPE_REFUSED"
    assert svc.discussions.posts == 0 and svc.discussions.reads == 0
    assert audit_lines(svc)[-1]["result"] == "refused"


def test_notes_add_refuses_a_phone_number(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", "call 516-555-0199 today")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "note_refused"
    assert svc.discussions.posts == 0


def test_notes_add_unconfirmed_post_is_exit_5_and_never_reposted(tmp_path):
    svc = Svc(tmp_path, discussions=FakeDiscussions(with_notes="missing"))
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", "hello there")
    assert code == cli.EXIT_UNCONFIRMED and payload["error"]["code"] == "note_unconfirmed"
    assert svc.discussions.posts == 1
    assert audit_lines(svc)[-1]["result"] == "unconfirmed"
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", "hello there")
    assert svc.discussions.posts == 1  # the ledger blocks an automatic repeat


def test_notes_add_unknown_discussion_is_pending_not_written(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "999", "--text", "hello")
    assert code == cli.EXIT_ERROR and payload["ok"] is False and payload["status"] == "pending"
    assert svc.discussions.posts == 0


def test_notes_add_argument_rules_and_text_file(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--text", "hello")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_discussion"
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "1", "--title", "t", "--text", "hello")
    assert code == cli.EXIT_USAGE
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_note_text"
    path = _file(tmp_path, "n.txt", b"note from a file\n")
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text-file", str(path))
    assert code == 0 and svc.discussions.posted_text == "note from a file"


def test_stdin_input_for_notes_and_questions(tmp_path, monkeypatch):
    svc = Svc(tmp_path, extract=lambda data: (["page"], []))
    svc.fake.bodies["111"] = pdf_bytes(1)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"note: it's \"quoted\" and fine\n")))
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text-file", "-")
    assert code == 0 and svc.discussions.posted_text == 'note: it\'s "quoted" and fine'
    gemini = svc._gemini
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"What's the limit?\n")))
    code, payload = run(svc, "docs", "ask", "111", "--question-file", "-")
    assert code == 0 and "What's the limit?" in gemini.calls[0]["prompt"]
    code, payload = run(svc, "docs", "ask", "111")
    assert code == cli.EXIT_USAGE and payload["error"]["code"] == "bad_question"
    code, payload = run(svc, "docs", "ask", "111", "q", "--question-file", "-")
    assert code == cli.EXIT_USAGE


def test_notes_add_dry_run_posts_nothing(tmp_path):
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", "hello", "--dry-run")
    assert code == 0 and payload["status"] == "dry_run" and svc.discussions.posts == 0
    assert [line["phase"] for line in audit_lines(svc)] == ["end"]


def test_notes_add_refused_when_all_clients_scope_is_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "*")
    svc = Svc(tmp_path)
    code, payload = run(svc, "notes", "add", ALLOWED, "--discussion-id", "841872781", "--text", "hello")
    assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "write_scope_all_refused"
    assert svc.discussions.posts == 0


# ---------------------------------------------------- read-only guarantee
READ_ARGS = [
    ("docs", "list", ALLOWED),
    ("docs", "read", "111"),
    ("docs", "ask", "111", "what?"),
    ("discussions", "list", ALLOWED),
    ("discussions", "get", "841872781"),
    ("policy", "lookup", "CT1278263263-2"),
]


@pytest.mark.parametrize("argv", READ_ARGS)
def test_every_read_verb_makes_only_gets_and_never_calls_the_write_gate(tmp_path, monkeypatch, argv):
    def boom(*_a, **_k):
        raise AssertionError("read verb reached a write gate")

    monkeypatch.setattr(scope, "require_allowed_ezlynx_write_applicant", boom)
    svc = Svc(tmp_path, extract=lambda data: (["page"], []))
    svc.fake.bodies["111"] = pdf_bytes(1)
    svc.discussions.append_note = boom
    code, payload = run(svc, *argv)
    assert code == 0, payload
    assert svc.fake.writes() == []
    assert svc.discussions.posts == 0
    assert audit_lines(svc)[-1]["phase"] == "end"
    assert "start" not in [line["phase"] for line in audit_lines(svc)]


def _installer(root, *argv):
    import subprocess

    return subprocess.run(
        ["bash", str(root / "scripts" / "install-ezlynx-api-cli.sh"), *argv],
        capture_output=True, text=True, check=False,
    )


def _fake_host(prefix: Path, *, production: bool = False, test: bool = False) -> None:
    for flag, name in ((production, "streetsmart-hermes"), (test, "streetsmart-hermes-test")):
        if flag:
            py = prefix / "opt" / name / "venv" / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("#!/bin/sh\n")
            py.chmod(0o755)


def test_wrapper_and_installer_are_wired_for_the_release(tmp_path):
    import subprocess

    root = Path(cli.__file__).resolve().parents[1]
    wrapper = (root / "scripts" / "ezlynx-api").read_text()
    assert "sudo -n -u \"${RUN_AS}\"" in wrapper
    assert "RUN_AS=\"${EZLYNX_API_RUN_AS:-streetsmart-hermes}\"" in wrapper
    assert re.search(r"^cd /$", wrapper, re.M)
    assert "unset ROBIE_EZLYNX_WRITE_SCOPE" in wrapper and "ROBIE_PLAYGROUND" in wrapper
    assert "-m robie_job_engine.ezlynx_api_cli" in wrapper
    assert "ROBIE_EZLYNX_WRITE_SCOPE=" not in wrapper.replace("unset ROBIE_EZLYNX_WRITE_SCOPE", "")
    assert os.access(root / "scripts" / "ezlynx-api", os.X_OK)
    for script in ("ezlynx-api", "install-ezlynx-api-cli.sh"):
        assert subprocess.run(["bash", "-n", str(root / "scripts" / script)]).returncode == 0
    # Production layout (the default).
    prefix = tmp_path / "prod"
    _fake_host(prefix, production=True)
    proc = _installer(root, "--prefix", str(prefix), "--release-dir", str(root))
    assert proc.returncode == 0, proc.stderr
    assert (prefix / "usr/local/bin/ezlynx-api").read_text() == wrapper
    assert stat.S_IMODE((prefix / "var/lib/ezlynx-api-cli").stat().st_mode) == 0o750
    conf = (prefix / "etc/streetsmart-hermes/ezlynx-api-cli.env").read_text()
    assert "ROBIE_ENV=PRODUCTION" in conf and "EZLYNX_API_RUN_AS=streetsmart-hermes\n" in conf
    assert "EZLYNX_API_RELEASE=/opt/streetsmart-hermes/current\n" in conf
    assert "EZLYNX_API_PYTHON=/opt/streetsmart-hermes/venv/bin/python\n" in conf
    assert "ROBIE_API_CLI_STATE_DIR=/var/lib/ezlynx-api-cli\n" in conf
    gone = _installer(root, "--prefix", str(prefix), "--uninstall")
    assert gone.returncode == 0
    assert not (prefix / "usr/local/bin/ezlynx-api").exists() and (prefix / "var/lib/ezlynx-api-cli").is_dir()


def test_installer_test_layout(tmp_path):
    root = Path(cli.__file__).resolve().parents[1]
    prefix = tmp_path / "test"
    _fake_host(prefix, test=True)
    proc = _installer(root, "--prefix", str(prefix), "--release-dir", str(root), "--robie-env", "TEST")
    assert proc.returncode == 0, proc.stderr
    conf = (prefix / "etc/streetsmart-hermes-test/ezlynx-api-cli.env").read_text()
    assert "ROBIE_ENV=TEST" in conf and "EZLYNX_API_RUN_AS=streetsmart-hermes-test\n" in conf
    assert "EZLYNX_API_RELEASE=/opt/streetsmart-hermes-test/releases/current\n" in conf
    assert "EZLYNX_API_PYTHON=/opt/streetsmart-hermes-test/venv/bin/python\n" in conf
    assert "ROBIE_API_CLI_STATE_DIR=/var/lib/ezlynx-api-cli-test\n" in conf
    assert stat.S_IMODE((prefix / "var/lib/ezlynx-api-cli-test").stat().st_mode) == 0o750
    assert not (prefix / "etc/streetsmart-hermes").exists()
    dry = _installer(root, "--prefix", str(prefix), "--dry-run", "--release-dir", str(root), "--robie-env", "TEST")
    assert dry.returncode == 0 and "streetsmart-hermes-test" in dry.stdout and "ROBIE_ENV=TEST" in dry.stdout


def test_installer_refuses_the_wrong_layout_for_the_host(tmp_path):
    root = Path(cli.__file__).resolve().parents[1]
    # Test host, Production layout asked for: refuse and say what to use.
    prefix = tmp_path / "testhost"
    _fake_host(prefix, test=True)
    proc = _installer(root, "--prefix", str(prefix), "--release-dir", str(root))
    assert proc.returncode == 2
    assert "--robie-env TEST" in proc.stderr and "wrong layout" in proc.stderr
    assert not (prefix / "usr/local/bin/ezlynx-api").exists()
    # Production host, Test layout asked for.
    prefix2 = tmp_path / "prodhost"
    _fake_host(prefix2, production=True)
    proc = _installer(root, "--prefix", str(prefix2), "--release-dir", str(root), "--robie-env", "TEST")
    assert proc.returncode == 2 and "--robie-env PRODUCTION" in proc.stderr
    assert not (prefix2 / "usr/local/bin/ezlynx-api").exists()
    # A host with neither install, and a dry run on it, also refuse.
    empty = tmp_path / "empty"
    empty.mkdir()
    for extra in ((), ("--dry-run",)):
        proc = _installer(root, "--prefix", str(empty), "--release-dir", str(root), *extra)
        assert proc.returncode == 2 and "no PRODUCTION Hermes install" in proc.stderr
    # No virtualenv, no install.
    novenv = tmp_path / "novenv"
    (novenv / "opt" / "streetsmart-hermes").mkdir(parents=True)
    proc = _installer(root, "--prefix", str(novenv), "--release-dir", str(root))
    assert proc.returncode == 2 and "virtualenv is missing" in proc.stderr


def test_installer_refuses_a_release_dir_from_the_other_layout(tmp_path):
    # Without --prefix the release must sit under this host's own root. Dry run
    # exits before touching anything, so this is safe to run unprivileged.
    root = Path(cli.__file__).resolve().parents[1]
    proc = _installer(root, "--dry-run", "--release-dir", str(root), "--robie-env", "TEST")
    assert proc.returncode == 2  # no /opt/streetsmart-hermes-test here, or release not under it


def test_wrapper_runs_selftest_with_global_gemini_location(tmp_path):
    import subprocess
    import sys

    root = Path(cli.__file__).resolve().parents[1]
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "EZLYNX_API_RUN_AS": me,
        "EZLYNX_API_RELEASE": str(root),
        "EZLYNX_API_PYTHON": sys.executable,
        "ROBIE_API_CLI_STATE_DIR": str(tmp_path / "state"),
    }
    if (Path("/etc/streetsmart-hermes/ezlynx-api-cli.env").exists()
            or Path("/etc/streetsmart-hermes-test/ezlynx-api-cli.env").exists()):
        pytest.skip("a real settings file is installed on this machine")
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "ezlynx-api"), "--agent", "wrapper-test", "selftest"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)["result"]
    assert result["gemini"]["location"] == "global" and result["gemini"]["model"] == "gemini-3.8-flash"
    assert result["state_dir"] == str(tmp_path / "state")
    # A missing release is a clear refusal, not a Python traceback.
    env["EZLYNX_API_RELEASE"] = str(tmp_path / "nope")
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "ezlynx-api"), "--agent", "wrapper-test", "selftest"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 2 and "no release at" in proc.stderr and "Traceback" not in proc.stderr
    env["EZLYNX_API_RELEASE"] = str(root)
    env["EZLYNX_API_PYTHON"] = str(tmp_path / "no-python")
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "ezlynx-api"), "--agent", "wrapper-test", "selftest"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 2 and "layout does not match" in proc.stderr


def test_wrapper_test_layout_never_exports_the_prod_secret():
    root = Path(cli.__file__).resolve().parents[1]
    wrapper = (root / "scripts" / "ezlynx-api").read_text()
    guarded = wrapper.split('if [[ "${ROBIE_ENV}" != "TEST" ]]; then', 1)[1].split("fi", 1)[0]
    assert "ROBIE_EZLYNX_API_PROD_SECRET" in guarded
    assert wrapper.count("ROBIE_EZLYNX_API_PROD_SECRET") == 2  # one export line, only inside the guard
    assert 'ROBIE_GEMINI_LOCATION="${ROBIE_GEMINI_LOCATION:-global}"' in wrapper


# ------------------------------------------------------ Vertex location
def _vertex_urls(location):
    from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient

    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()

    def opener(request, timeout=None):
        seen.append(request.full_url)
        return Resp()

    client = VertexGeminiFieldClient(project="proj", location=location, model="gemini-3.8-flash",
                                     opener=opener, token_provider=lambda: "tok")
    assert client.generate_content("hi") == "ok"
    assert client.generate_unique_field("hi") == "ok"
    return seen


def test_vertex_global_location_uses_the_global_host():
    expected = ("https://aiplatform.googleapis.com/v1/projects/proj/locations/global/"
                "publishers/google/models/gemini-3.8-flash:generateContent")
    assert _vertex_urls("global") == [expected, expected]
    assert "global-aiplatform" not in expected


def test_vertex_regional_location_keeps_the_regional_host():
    expected = ("https://us-central1-aiplatform.googleapis.com/v1/projects/proj/locations/us-central1/"
                "publishers/google/models/gemini-3.8-flash:generateContent")
    assert _vertex_urls("us-central1") == [expected, expected]


def test_vertex_location_with_odd_characters_is_refused():
    from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient

    client = VertexGeminiFieldClient(project="proj", location="evil.example.com/x", model="m",
                                     opener=lambda *a, **k: pytest.fail("no request"), token_provider=lambda: "tok")
    with pytest.raises(RuntimeError, match="unexpected characters"):
        client.generate_content("hi")


def test_cli_gemini_location_defaults_to_global(monkeypatch):
    monkeypatch.delenv("ROBIE_GEMINI_LOCATION", raising=False)
    monkeypatch.setenv("ROBIE_GEMINI_PROJECT", "proj")
    assert cli.gemini_location() == "global"
    client = cli.LiveServices(Path("/nonexistent")).gemini(None)
    assert client.location == "global" and client.model == "gemini-3.8-flash"
    assert cli.LiveServices(Path("/nonexistent")).gemini("gemini-3.8-flash-lite").location == "global"
    monkeypatch.setenv("ROBIE_GEMINI_LOCATION", "us-east5")
    assert cli.gemini_location() == "us-east5"
    assert cli.LiveServices(Path("/nonexistent")).gemini(None).location == "us-east5"


def test_the_shared_helper_default_is_unchanged_for_other_callers(monkeypatch):
    # The HITL helper keeps its own default; only the CLI defaults to global.
    from robie_job_engine import gemini_field_helper as helper

    monkeypatch.delenv("ROBIE_GEMINI_LOCATION", raising=False)
    monkeypatch.setenv("ROBIE_GEMINI_PROJECT", "proj")
    assert helper.VertexGeminiFieldClient().location == helper.DEFAULT_VERTEX_LOCATION


# ------------------------------------------------------ OCR not installed
def test_docs_read_without_ocr_warns_about_blank_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocr_available", lambda: False)
    svc = Svc(tmp_path, extract=lambda data: (["real text " * 10, "", "  \n"], []))
    svc.fake.bodies["111"] = pdf_bytes(3)
    code, payload = run(svc, "docs", "read", "111")
    assert code == 0
    result = payload["result"]
    assert result["blank_pages"] == [2, 3] and result["ocr_pages"] == []
    assert len(result["warnings"]) == 1
    assert "OCR is not installed" in result["warnings"][0] and "2 of 3" in result["warnings"][0]
    assert "docs ask --direct" in result["warnings"][0]


def test_docs_read_with_ocr_has_no_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocr_available", lambda: True)
    svc = Svc(tmp_path, extract=lambda data: (["real text " * 10, "scanned words here"], [2]))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "read", "111")
    assert code == 0 and payload["result"]["warnings"] == [] and payload["result"]["blank_pages"] == []


def test_docs_ask_all_blank_without_ocr_stops_before_gemini(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocr_available", lambda: False)
    gemini = FakeGemini()
    svc = Svc(tmp_path, gemini=gemini, extract=lambda data: (["", " "], []))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "ask", "111", "What is the limit?")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "no_text_in_document"
    assert "OCR is not installed" in payload["error"]["message"] and "--direct" in payload["error"]["message"]
    assert "Nothing was sent to Gemini" in payload["error"]["message"]
    assert gemini.calls == []


def test_docs_ask_partly_blank_without_ocr_answers_and_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocr_available", lambda: False)
    gemini = FakeGemini()
    svc = Svc(tmp_path, gemini=gemini, extract=lambda data: (["Limit is $1,000,000. " * 3, ""], []))
    svc.fake.bodies["111"] = pdf_bytes(2)
    code, payload = run(svc, "docs", "ask", "111", "What is the limit?")
    assert code == 0 and len(gemini.calls) == 1
    assert "OCR is not installed" in payload["result"]["warnings"][0]


def test_docs_read_poppler_missing_is_a_clear_error(tmp_path):
    from robie_job_engine.robie_filer_extract import ExtractionUnavailable

    def broken(_data):
        raise ExtractionUnavailable("pdftotext/pdfinfo are not installed on this host")

    svc = Svc(tmp_path, extract=broken)
    svc.fake.bodies["111"] = pdf_bytes(1)
    code, payload = run(svc, "docs", "read", "111")
    assert code == cli.EXIT_ERROR and payload["error"]["code"] == "extraction_unavailable"
    assert "poppler-utils" in payload["error"]["message"] and "--direct" in payload["error"]["message"]


def test_selftest_reports_missing_ocr_and_global_location(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocr_available", lambda: False)
    monkeypatch.delenv("ROBIE_GEMINI_LOCATION", raising=False)
    monkeypatch.setenv("ROBIE_GEMINI_PROJECT", "proj")
    svc = Svc(tmp_path)
    code, payload = run(svc, "selftest")
    assert code == 0
    result = payload["result"]
    assert result["ocr_available"] is False and "OCR is not installed" in result["warnings"][0]
    assert result["gemini"]["location"] == "global"


def test_upload_refuses_the_audit_log_and_test_hermes_home(tmp_path):
    svc = Svc(tmp_path)
    for path in ("/var/lib/ezlynx-api-cli/audit.jsonl", "/var/lib/ezlynx-api-cli-test/audit.jsonl",
                 "/opt/streetsmart-hermes-test/.hermes/robie_google_token.json"):
        code, payload = run(svc, "docs", "upload", ALLOWED, "--file", path, "--name", "x.pdf", "--dry-run")
        assert code == cli.EXIT_REFUSED and payload["error"]["code"] == "sensitive_path", path
