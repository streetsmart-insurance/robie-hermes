"""Ascend locator-audit + real-pipeline Test simulator.

A full Job Engine job type. Test-only. Not Production-ready. Commercial auto
already on Production is not a free pass for a NEW site or workflow.

CI runs the punch-list reporter and artifact-path assertions only. The live
Ascend walk is hermes-test-01 / ROBIE_ENV=TEST. GitHub runners never talk to
Ascend or EZLynx. No Gemini. No .first / .nth / .last. No bind, email, or
Save program. Loom ascend-finance is never overwritten here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .complete_guard import (
    complete_is_prohibited,
    destination_identity_missing,
    expected_postcondition_missing,
)
from .models import (
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from .operations import OperationsStore
from .playwright_write_guard import locator_is_positional_guess, locator_selector_text
from .quote_replay import is_live_hermes_path, refuse_production_targets
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, ProductionGuardError, current_robie_env
from .store import JobStore
from .ascend_sender_roles import (
    IMPORT_DOCUMENT_LOCATOR,
    NEW_PROGRAM_ACCESSIBLE_NAME,
    NEW_PROGRAM_LOCATOR,
)


JOB_TYPE = "ascend.locator_artifact_audit"
WORKER_NAME = "ascend-locator-audit"
SCENARIO_ID = "ascend:locator-and-artifact-audit"
CONCAT_SCENARIO_ID = "artifact-path:concat-job-id-eb96f620"
SKILL_NAME = "ascend-locator-artifact-audit"
PROGRAMS_URL = "https://dashboard.useascend.com/programs"
REPORT_KIND = "locator_artifact_audit_report"

# Production job eb96f620 HITL PLAYWRIGHT_BLOCKED: worker looked in
# artifacts/{job_id[:-11]}{artifact_id}/ instead of artifacts/{full_job_id}/.
PRODUCTION_EB96_JOB_ID = "eb96f620-f8c3-4006-8eb4-d938d2a44c73"
PRODUCTION_EB96_ARTIFACT_ID = "3a41af0e-ca7f-4a57-8cae-67d3ca55c1c5"
PRODUCTION_EB96_WRONG_FOLDER = (
    "eb96f620-f8c3-4006-8eb4-d3a41af0e-ca7f-4a57-8cae-67d3ca55c1c5"
)

FORBIDDEN_ACCOUNTS = frozenset({"PAWIVA", "221398001"})
FORBIDDEN_ACTIONS = (
    "Save program",
    "Send email",
    "Copy checkout",
    "payment",
    "bind",
)
STOP_BEFORE = FORBIDDEN_ACTIONS
TEST_INSURED = "ROBIE Test LLC"
TEST_QUOTE_NUMBER = "TEST-ASCEND-AUDIT"
MINIMAL_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
    b"2 0 obj<< /Type /Pages /Count 1 /Kids [3 0 R] >>endobj\n"
    b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>endobj\n"
    b"trailer<< /Root 1 0 R >>\n"
    b"%%EOF\n"
)
STRICT_MARKERS = (
    "strict mode violation",
    "resolved to 2 elements",
    "resolved to 0 elements",
    "resolved to more than one",
    "locator resolved to",
)
POSITIONAL_MARKERS = (".first", ".nth(", ".last", "nth=", " >> nth")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-",
    re.IGNORECASE,
)
UUID_SEARCH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
NEW_SITE_WORKFLOWS = frozenset({"ascend", "next-carrier-portal"})
OPERATOR_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

FLOW_STEPS: tuple[dict[str, str], ...] = (
    {
        "id": "open_programs",
        "description": "Open Ascend programs dashboard",
        "locator": f'page.goto("{PROGRAMS_URL}")',
    },
    {
        "id": "wait_programs_ready",
        "description": "Log seconds until unique New program primary is ready",
        "locator": 'get_by_text("Programs at risk")',
    },
    {
        "id": "new_program",
        "description": "Click unique primary New program (never the caret); wait_for_url /create/new",
        "locator": NEW_PROGRAM_LOCATOR,
    },
    {
        "id": "import_document",
        "description": (
            "Wait until unique Import document is visible after /create/new "
            "(log seconds); then log Import vs Upload vs dropzone"
        ),
        "locator": IMPORT_DOCUMENT_LOCATOR,
    },
    {
        "id": "unique_listbox_options",
        "description": (
            "Open each create-form combobox; unique Name+email option "
            "for Producer/AM (not name-only Carlo Ferrara); Carrier / "
            "State / Coverage type from the imported Test quote (38c0fa79)"
        ),
        "locator": 'get_by_role("option", name=intended, exact=True)',
    },
    {
        "id": "producer_role",
        "description": "Log Producer prefill; overwrite from requested_by (never leave Robie AI)",
        "locator": 'get_by_label("Producer")',
    },
    {
        "id": "account_manager_role",
        "description": "Log Account Manager prefill; overwrite from requested_by (never leave Robie AI)",
        "locator": 'get_by_label("Account Manager")',
    },
    {
        "id": "customer_type",
        "description": "Commercial vs Personal radio from line of business (not the form default)",
        "locator": 'get_by_role("radio", name="Commercial customer")',
    },
    {
        "id": "insured_fields",
        "description": "Customer Name (Test account only)",
        "locator": 'get_by_label("Name")',
    },
    {
        "id": "address_autocomplete",
        "description": "Address autocomplete — pick the exact row",
        "locator": 'get_by_role("option", name=exact_address, exact=True)',
    },
    {
        "id": "quote_number",
        "description": "Quote number",
        "locator": 'get_by_label("Quote number")',
    },
    {
        "id": "carrier",
        "description": "Carrier",
        "locator": 'get_by_label("Carrier")',
    },
    {
        "id": "wholesaler",
        "description": "Wholesaler",
        "locator": 'get_by_label("Wholesaler")',
    },
    {
        "id": "coverage_type",
        "description": "Coverage type",
        "locator": 'get_by_label("Coverage type")',
    },
    {
        "id": "dates",
        "description": "Effective and expiration dates",
        "locator": 'get_by_label("Effective date")',
    },
    {
        "id": "premium",
        "description": "Premium",
        "locator": 'get_by_label("Premium")',
    },
    {
        "id": "taxes",
        "description": "Taxes",
        "locator": 'get_by_label("Taxes")',
    },
    {
        "id": "agency_fee",
        "description": "Log Agency Fee default (expect $0.00 / empty); set 500 if the field exists",
        "locator": 'get_by_label("Agency Fee")',
    },
    {
        "id": "stop_before_save",
        "description": "Stop before Save program / Send email / Copy checkout / payment / bind",
        "locator": 'get_by_role("button", name="Save program")',
    },
    {
        "id": "quote_pdf_save",
        "description": "Save quote PDF under the real job-id artifact folder",
        "locator": "",
    },
    {
        "id": "quote_pdf_lookup",
        "description": "Look up the quote PDF the same way a live Chat job does",
        "locator": "",
    },
    {
        "id": "quote_pdf_open",
        "description": "Open the PDF the worker just saved",
        "locator": "",
    },
)


class UniqueLocatorError(RuntimeError):
    """Playwright strict-mode / non-unique locator. Always FAIL. No Gemini."""


class ArtifactPathError(RuntimeError):
    """Job artifact folder is missing, concatenated, or not the job id."""


class MissingQuotePdfError(RuntimeError):
    """Quote PDF is missing or cannot be opened after save."""


class ForbiddenAccountError(RuntimeError):
    """Real client / PAWIVA / 221398001 is refused."""


class FinanceCompleteError(RuntimeError):
    """This audit job may not COMPLETE a finance agreement."""


@dataclass
class PunchStep:
    id: str
    description: str
    status: str
    locator: str = ""
    artifact_path: str = ""
    error: str | None = None
    observed: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        blob = {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "locator": self.locator or None,
            "artifact_path": self.artifact_path or None,
            "error": self.error,
        }
        if self.observed:
            blob["observed"] = self.observed
        return blob


@dataclass
class PunchList:
    job_id: str
    scenario: str = SCENARIO_ID
    steps: list[PunchStep] = field(default_factory=list)
    stopped_before: tuple[str, ...] = STOP_BEFORE
    kind: str = REPORT_KIND
    finance_agreement: bool = False

    @property
    def overall(self) -> str:
        if not self.steps:
            return "FAIL"
        return "FAIL" if any(step.status == "FAIL" for step in self.steps) else "PASS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scenario": self.scenario,
            "kind": self.kind,
            "finance_agreement": self.finance_agreement,
            "overall": self.overall,
            "stopped_before": list(self.stopped_before),
            "steps": [step.as_dict() for step in self.steps],
        }

    def format_text(self) -> str:
        lines = [
            f"Ascend locator + artifact audit — {self.overall}",
            f"job_id: {self.job_id}",
            f"scenario: {self.scenario}",
            f"kind: {self.kind} (report, not a finance agreement)",
            "stopped before: " + ", ".join(self.stopped_before),
        ]
        for step in self.steps:
            where = step.locator or step.artifact_path or ""
            extra = f" {where}" if where else ""
            err = f" — {step.error}" if step.error else ""
            observed = ""
            if step.observed:
                bits = []
                if "seconds" in step.observed:
                    bits.append(f"seconds={step.observed['seconds']}")
                if step.observed.get("default") is not None:
                    bits.append(f"default={step.observed['default']!r}")
                if step.observed.get("set_value") is not None:
                    bits.append(f"set={step.observed['set_value']!r}")
                if step.observed.get("labels"):
                    bits.append("labels=" + ",".join(str(item) for item in step.observed["labels"]))
                if step.observed.get("producer_default") is not None:
                    bits.append(f"producer={step.observed['producer_default']!r}")
                if step.observed.get("account_manager_default") is not None:
                    bits.append(f"am={step.observed['account_manager_default']!r}")
                if step.observed.get("url"):
                    bits.append(f"url={step.observed['url']}")
                if bits:
                    observed = " [" + "; ".join(bits) + "]"
            lines.append(f"- {step.id}: {step.status}{extra}{err}{observed}")
        return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def refuse_production_env(*, env: str | None = None) -> None:
    environment = (env if env is not None else current_robie_env()).upper()
    if environment in PRODUCTION_ENV_NAMES:
        raise ProductionGuardError(
            "refusing ascend locator-artifact audit: ROBIE_ENV is Production. "
            "This Test run is required before a NEW site/workflow may run on "
            "a real Production account. Commercial auto is not a free pass."
        )


def refuse_live_ci() -> None:
    if str(os.environ.get("GITHUB_ACTIONS") or "").strip() == "true":
        raise ProductionGuardError(
            "refusing live Ascend/EZLynx walk on GitHub-hosted CI"
        )
    if str(os.environ.get("CI") or "").strip().lower() in {"1", "true", "yes"}:
        if current_robie_env() != TEST_ENV_NAME:
            raise ProductionGuardError(
                "refusing live Ascend walk on CI without ROBIE_ENV=TEST"
            )


def refuse_forbidden_account(*values: Any) -> None:
    blobs = " ".join(str(item or "") for item in values).upper()
    for banned in FORBIDDEN_ACCOUNTS:
        if banned in blobs:
            raise ForbiddenAccountError(
                f"refusing real client account {banned}; Test account only"
            )
    lowered = blobs.casefold()
    if "pawiva" in lowered:
        raise ForbiddenAccountError("refusing real client account PAWIVA; Test account only")


def locator_is_positional(locator: str | Any) -> bool:
    text = locator if isinstance(locator, str) else locator_selector_text(locator)
    folded = str(text or "").casefold()
    if locator_is_positional_guess(locator if not isinstance(locator, str) else text):
        return True
    return any(marker in folded for marker in POSITIONAL_MARKERS)


def is_strict_mode_violation(error: str | BaseException) -> bool:
    text = str(error or "").casefold()
    name = type(error).__name__.casefold() if isinstance(error, BaseException) else ""
    if "strict" in name:
        return True
    return any(marker in text for marker in STRICT_MARKERS)


def classify_locator_failure(
    error: str | BaseException,
    *,
    locator: str = "",
    step_id: str = "",
    description: str = "",
) -> PunchStep:
    """Unique-locator fail is FAIL. No Gemini. No .first/.nth/.last guess."""
    text = str(error)
    name = type(error).__name__ if isinstance(error, BaseException) else "Error"
    if locator_is_positional(locator):
        classified = (
            "positional .first/.nth/.last guess is FAIL; "
            "Playwright strict mode requires a unique locator"
        )
    elif is_strict_mode_violation(error):
        classified = f"strict mode violation: {text}"
    elif name == "TimeoutError" or "timeouterror" in name.casefold() or "timeout" in text.casefold():
        classified = f"TimeoutError: {text}"
    else:
        classified = f"{name}: {text}"
    return PunchStep(
        id=step_id or "locator",
        description=description or "unique locator required",
        status="FAIL",
        locator=locator,
        error=classified,
    )


def require_unique_locator(target: Any, *, locator: str = "") -> None:
    """Treat non-unique or positional locators as FAIL. Never call Gemini."""
    resolved = locator or locator_selector_text(target)
    if locator_is_positional(target) or locator_is_positional(resolved):
        raise UniqueLocatorError(
            "positional .first/.nth/.last guess is FAIL; unique locator required"
        )
    count_fn = getattr(target, "count", None)
    if callable(count_fn):
        try:
            observed = int(count_fn())
        except Exception as exc:  # noqa: BLE001 — classify, do not guess
            raise UniqueLocatorError(f"locator count failed: {type(exc).__name__}: {exc}") from exc
        if observed != 1:
            raise UniqueLocatorError(
                f"strict mode violation: locator resolved to {observed} elements: {resolved}"
            )
        return
    raise UniqueLocatorError(
        "locator could not be uniquely identified; refuse to guess"
    )


def canonical_job_artifact_dir(artifact_root: str | Path, job_id: str) -> Path:
    """Artifact folder name must equal the job id. Never concatenate ids."""
    if not str(job_id or "").strip():
        raise ArtifactPathError("job id is missing; cannot build artifact folder")
    if any(part in job_id for part in ("/", "\\", "..")):
        raise ArtifactPathError(f"job id is not a single folder name: {job_id!r}")
    if looks_concatenated_job_id(job_id):
        raise ArtifactPathError(
            f"job id looks concatenated, not a single id: {job_id!r}"
        )
    return Path(artifact_root) / job_id


def worker_lookup_artifact_dir(
    artifact_root: str | Path,
    job_id: str,
    *,
    artifact_id: str | None = None,
) -> Path:
    """The only legal worker lookup folder: ``{artifact_root}/{full_job_id}/``.

    Slicing ``job_id`` or concatenating ``job_id[:n]`` with ``artifact_id``
    is FAIL. This is the construction workers must use — not
    ``job_id[:-11] + artifact_id`` (Production ``eb96f620``).
    """
    if artifact_id:
        forbidden = sliced_concat_job_folder(job_id, artifact_id)
        if forbidden == job_id:
            raise ArtifactPathError(
                "artifact id must not replace the job-id folder"
            )
    return canonical_job_artifact_dir(artifact_root, job_id)


def sliced_concat_job_folder(job_id: str, artifact_id: str) -> str:
    """Reproduce Production ``eb96f620``: ``job_id[:-11] + artifact_id``.

    Always illegal as a lookup folder. Tests use this to prove FAIL.
    """
    prefix = job_id[:-11] if len(job_id) >= 11 else job_id
    return f"{prefix}{artifact_id}"


def is_sliced_job_id_plus_artifact_id(
    folder: str,
    job_id: str,
    artifact_id: str,
) -> bool:
    """True when the folder is any ``job_id[:n] + artifact_id`` mash-up."""
    name = str(folder or "").strip()
    if not name or not job_id or not artifact_id:
        return False
    if name == job_id:
        return False
    if name == sliced_concat_job_folder(job_id, artifact_id):
        return True
    for index in range(1, len(job_id)):
        if name == f"{job_id[:index]}{artifact_id}":
            return True
    return False


def looks_concatenated_job_id(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if UUID_RE.fullmatch(text):
        return False
    if len(text) >= 72 and UUID_RE.fullmatch(text[:36]) and UUID_RE.fullmatch(text[36:72]):
        return True
    if UUID_RE.fullmatch(text[:36]) and len(text) > 36:
        return True
    # Production eb96f620: job_id prefix + artifact UUID. First 36 chars
    # are not themselves a UUID (the splice sits inside the last group).
    if len(text) > 36 and UUID_PREFIX_RE.match(text) and UUID_SEARCH_RE.search(text):
        return True
    return False


def artifact_folder_for_path(stored_path: str | Path, artifact_root: str | Path) -> str:
    stored = Path(stored_path).resolve()
    root = Path(artifact_root).resolve()
    try:
        relative = stored.relative_to(root)
    except ValueError as exc:
        raise ArtifactPathError(
            f"artifact path {str(stored)!r} is outside artifact root {str(root)}"
        ) from exc
    if not relative.parts:
        raise ArtifactPathError("artifact path has no job-id folder")
    return relative.parts[0]


def assert_artifact_path_matches_job_id(
    stored_path: str | Path,
    *,
    job_id: str,
    artifact_root: str | Path,
    artifact_id: str | None = None,
) -> Path:
    """FAIL when the job folder is concatenated or not exactly the job id."""
    folder = artifact_folder_for_path(stored_path, artifact_root)
    if artifact_id and is_sliced_job_id_plus_artifact_id(folder, job_id, artifact_id):
        raise ArtifactPathError(
            f"lookup path is job_id[:n]+artifact_id ({folder!r}); "
            f"required {{artifact_root}}/{{full_job_id}}/ = {job_id!r}"
        )
    if looks_concatenated_job_id(folder):
        raise ArtifactPathError(
            f"mangled/concatenated job-id folder {folder!r} "
            f"does not equal job id {job_id!r}"
        )
    if folder != job_id:
        extra = (
            f" (looks concatenated from {job_id!r})"
            if job_id and job_id in folder
            else ""
        )
        raise ArtifactPathError(
            f"artifact job folder {folder!r} does not equal job id {job_id!r}{extra}"
        )
    legal = worker_lookup_artifact_dir(
        artifact_root, job_id, artifact_id=artifact_id
    )
    if Path(stored_path).resolve().parent != legal.resolve():
        raise ArtifactPathError(
            f"lookup path parent {Path(stored_path).resolve().parent} "
            f"is not exactly {legal}"
        )
    return Path(stored_path)


def assert_quote_pdf_openable(stored_path: str | Path | None) -> Path:
    """FAIL if the worker cannot open the PDF it just saved."""
    if not stored_path:
        raise MissingQuotePdfError("missing PDF after save: no stored_path")
    path = Path(stored_path)
    if not path.is_file():
        raise MissingQuotePdfError(
            f"missing PDF after save: {path} is not a readable file"
        )
    size = path.stat().st_size
    if size <= 0:
        raise MissingQuotePdfError(f"missing PDF after save: empty file {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MissingQuotePdfError(
            f"worker cannot open the PDF it just saved: {type(exc).__name__}: {exc}"
        ) from exc
    if not data.startswith(b"%PDF"):
        raise MissingQuotePdfError(
            f"worker cannot open the PDF it just saved: not a PDF at {path}"
        )
    return path


def save_and_lookup_quote_pdf(
    *,
    db_path: str,
    job_id: str,
    artifact_root: str,
    pdf_bytes: bytes | None = None,
    source_name: str = "ascend-test-quote.pdf",
    message_id: str = "ascend-locator-audit",
) -> dict[str, Any]:
    """Save and look up a quote PDF the same way a live Chat job does."""
    refuse_production_targets(db_path, artifact_root)
    data = pdf_bytes if pdf_bytes is not None else MINIMAL_PDF
    if not data:
        raise MissingQuotePdfError("missing PDF after save: empty bytes")
    ops = OperationsStore(db_path, artifact_root)
    record = ops.ingest_bytes(
        job_id=job_id,
        data=data,
        original_name=source_name,
        source_external_id=f"{message_id}:quote-pdf",
        mime_type="application/pdf",
        source_platform="google_chat",
    )
    stored = str(record.get("stored_path") or "")
    artifact_id = str(record.get("id") or "")
    lookup_dir = worker_lookup_artifact_dir(
        artifact_root, job_id, artifact_id=artifact_id or None
    )
    if Path(stored).resolve().parent != lookup_dir.resolve():
        raise ArtifactPathError(
            f"worker lookup dir must be {lookup_dir}; stored parent is "
            f"{Path(stored).resolve().parent}"
        )
    assert_artifact_path_matches_job_id(
        stored,
        job_id=job_id,
        artifact_root=artifact_root,
        artifact_id=artifact_id or None,
    )
    found = ops.list_artifacts(job_id)
    if not any(item.get("id") == record.get("id") for item in found):
        raise MissingQuotePdfError(
            f"missing PDF after save: job {job_id} has no artifact row"
        )
    assert_quote_pdf_openable(stored)
    return record


def pass_step(
    step_id: str,
    *,
    locator: str = "",
    artifact_path: str = "",
    description: str = "",
    observed: dict[str, Any] | None = None,
) -> PunchStep:
    spec = _step_spec(step_id)
    return PunchStep(
        id=step_id,
        description=description or spec.get("description") or step_id,
        status="PASS",
        locator=locator or spec.get("locator") or "",
        artifact_path=artifact_path,
        observed=dict(observed or {}),
    )


def fail_step(
    step_id: str,
    error: str | BaseException,
    *,
    locator: str = "",
    artifact_path: str = "",
    description: str = "",
    observed: dict[str, Any] | None = None,
) -> PunchStep:
    spec = _step_spec(step_id)
    loc = locator or spec.get("locator") or ""
    notes = dict(observed or {})
    if step_id.startswith("quote_pdf") or artifact_path:
        return PunchStep(
            id=step_id,
            description=description or spec.get("description") or step_id,
            status="FAIL",
            locator=loc,
            artifact_path=artifact_path,
            error=str(error),
            observed=notes,
        )
    step = classify_locator_failure(
        error,
        locator=loc,
        step_id=step_id,
        description=description or spec.get("description") or step_id,
    )
    if notes:
        step.observed = notes
    return step


def _step_spec(step_id: str) -> dict[str, str]:
    for item in FLOW_STEPS:
        if item["id"] == step_id:
            return item
    return {"id": step_id, "description": step_id, "locator": ""}


def empty_punch_list(job_id: str) -> PunchList:
    return PunchList(job_id=job_id)


def live_operator_idempotency_key(run_id: str = "") -> str | None:
    """Stable key for one explicit operator run; empty preserves legacy behavior."""
    value = str(run_id or "").strip()
    if not value:
        return None
    if not OPERATOR_RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "operator run id must be 1-128 letters, digits, dot, colon, underscore, or dash"
        )
    return f"{JOB_TYPE}:operator:{value}"


def site_workflow_hold_reason(
    site: str,
    *,
    env: str | None = None,
    passing_audit_count: int = 0,
    required: int | None = None,
) -> str | None:
    """NEW site/workflow on Production still needs N clean Test audits.

    Commercial auto already on Production is not a free pass.
    """
    from .job_type_gate import required_clean_test_jobs

    environment = (env if env is not None else current_robie_env()).upper()
    if environment not in PRODUCTION_ENV_NAMES:
        return None
    needed = required if required is not None else required_clean_test_jobs()
    key = str(site or "").strip().casefold()
    if key not in NEW_SITE_WORKFLOWS and key != "ascend":
        key = key or "new-site"
    if passing_audit_count >= needed:
        return None
    return (
        f"NEW site/workflow {site!r} is not Production-ready: "
        f"{passing_audit_count}/{needed} clean Test "
        f"{SCENARIO_ID} jobs on hermes-test-01. "
        "Commercial auto already on Production is not a free pass."
    )


def audit_report_destination(report_id: str, punch_list: PunchList) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "kind": REPORT_KIND,
        "finance_agreement": False,
        "outcome": punch_list.overall,
        "locator": report_id,
    }


def refuse_finance_agreement_complete(destination: dict[str, Any] | None) -> None:
    blob = dict(destination or {})
    if blob.get("finance_agreement") is True:
        raise FinanceCompleteError(
            "this audit job ends as a report, not COMPLETE of a finance agreement"
        )
    kind = str(blob.get("kind") or "")
    if kind and kind != REPORT_KIND:
        raise FinanceCompleteError(
            f"COMPLETE destination kind {kind!r} is not an audit report"
        )
    for key in ("program_id", "agreement_id", "checkout_url", "bind_id"):
        if blob.get(key):
            raise FinanceCompleteError(
                "this audit job ends as a report, not COMPLETE of a finance agreement"
            )


def refuse_complete_without_destination_evidence(
    *,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    locator: str | None,
    job_id: str | None,
    intended: str | None = None,
) -> None:
    missing = expected_postcondition_missing(expected)
    if missing:
        raise PermissionError(missing)
    identity = destination_identity_missing(
        locator=locator,
        expected=expected,
        observed=observed,
        intended=intended or locator,
        job_id=job_id,
    )
    if identity:
        raise PermissionError(identity)
    prohibited = complete_is_prohibited(observed)
    if prohibited:
        raise PermissionError(prohibited)


def persist_punch_list(
    *,
    db_path: str,
    job_id: str,
    artifact_root: str,
    punch_list: PunchList,
) -> dict[str, Any]:
    refuse_production_targets(db_path, artifact_root)
    payload = json.dumps(punch_list.as_dict(), indent=2, sort_keys=True).encode("utf-8")
    ops = OperationsStore(db_path, artifact_root)
    record = ops.ingest_bytes(
        job_id=job_id,
        data=payload,
        original_name="ascend-locator-audit-report.json",
        source_external_id=f"{job_id}:punch-list",
        mime_type="application/json",
        source_platform="robie_job_engine",
    )
    stored = str(record.get("stored_path") or "")
    assert_artifact_path_matches_job_id(
        stored, job_id=job_id, artifact_root=artifact_root
    )
    return record


def default_audit_payload(
    *,
    live: bool = False,
    requested_by: str = "",
    quote_path: str = "",
    quote_text: str = "",
) -> dict[str, Any]:
    from .ascend_create_combobox import (
        combobox_payload_from_audit,
        default_test_combobox_payload,
    )
    from .ascend_create_defaults import TEST_AGENCY_FEE
    from .ascend_sender_roles import requested_by_from_payload, roles_for_requested_by

    sender = str(requested_by or "").strip()
    roles = roles_for_requested_by(sender) if sender else roles_for_requested_by("")
    combobox = default_test_combobox_payload(requested_by=sender)
    merged = combobox_payload_from_audit(
        {
            **combobox,
            "requested_by": sender,
            "quote_path": str(quote_path or "").strip(),
            "quote_text": str(quote_text or "").strip(),
        }
    )
    payload = {
        "scenario": SCENARIO_ID,
        "resource_id": SCENARIO_ID,
        "report_only": True,
        "test_account_only": True,
        "live": bool(live),
        "requested_by": sender or requested_by_from_payload({}),
        "producer": roles.get("producer") or combobox.get("producer"),
        "account_manager": roles.get("account_manager") or combobox.get("account_manager"),
        "stop_before": list(STOP_BEFORE),
        "forbidden_accounts": sorted(FORBIDDEN_ACCOUNTS),
        "test_insured": TEST_INSURED,
        "test_quote_number": TEST_QUOTE_NUMBER,
        "test_agency_fee": TEST_AGENCY_FEE,
        "programs_url": PROGRAMS_URL,
        "quote_fields": merged.get("quote_fields") or {},
        "expected_postcondition": {
            "report_id": SCENARIO_ID,
            "kind": REPORT_KIND,
            "finance_agreement": False,
        },
    }
    if merged.get("quote_path"):
        payload["quote_path"] = merged["quote_path"]
    if merged.get("quote_text"):
        payload["quote_text"] = merged["quote_text"]
    if merged.get("quote_carrier"):
        payload["quote_carrier"] = merged["quote_carrier"]
    if merged.get("quote_coverage"):
        payload["quote_coverage"] = merged["quote_coverage"]
        payload.setdefault("coverage_type", merged["quote_coverage"])
        payload.setdefault("line_of_business", merged["quote_coverage"])
    if merged.get("quote_state"):
        payload["quote_state"] = merged["quote_state"]
    return payload


class AscendLocatorAuditWorker:
    """Walk the Test-only audit (or CI fixtures) and persist a punch-list report."""

    def __init__(
        self,
        *,
        walk: Callable[[dict[str, Any]], list[PunchStep]] | None = None,
        artifact_root: str | None = None,
    ) -> None:
        self.walk = walk
        self.artifact_root = artifact_root

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        del idempotency_key
        payload = dict(job.get("payload") or {})
        refuse_forbidden_account(
            payload.get("account"),
            payload.get("account_name"),
            payload.get("client"),
            payload.get("text"),
            payload.get("insured"),
            payload.get("quote_path"),
            payload.get("quote_text"),
            payload.get("test_quote_path"),
        )
        live = bool(payload.get("live"))
        if live:
            try:
                refuse_production_env()
                refuse_live_ci()
            except ProductionGuardError as exc:
                return WorkerResult(
                    False, JOB_TYPE, {}, retryable=False, error=str(exc)
                )
            if current_robie_env() != TEST_ENV_NAME:
                return WorkerResult(
                    False,
                    JOB_TYPE,
                    {},
                    retryable=False,
                    error="live Ascend walk requires ROBIE_ENV=TEST on hermes-test-01",
                )
        job_id = str(job.get("id") or "")
        db_path = str(
            job.get("db_path")
            or payload.get("db_path")
            or os.environ.get("ROBIE_JOB_DB")
            or ""
        )
        artifact_root = str(
            self.artifact_root
            or payload.get("artifact_root")
            or os.environ.get("ROBIE_ARTIFACT_ROOT")
            or ""
        )
        if not db_path or not artifact_root:
            return WorkerResult(
                False,
                JOB_TYPE,
                {},
                retryable=False,
                error="audit job is missing db_path or artifact_root",
            )
        try:
            refuse_production_targets(db_path, artifact_root)
        except ProductionGuardError as exc:
            return WorkerResult(
                False, JOB_TYPE, {}, retryable=False, error=str(exc)
            )
        punch = empty_punch_list(job_id)
        walker = self.walk or (
            _live_walk if live else _fixture_walk
        )
        try:
            punch.steps.extend(walker({**payload, "job_id": job_id, "db_path": db_path, "artifact_root": artifact_root}))
        except (ForbiddenAccountError, ProductionGuardError) as exc:
            return WorkerResult(
                False, JOB_TYPE, {}, retryable=False, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 — punch list must record, not crash
            punch.steps.append(
                fail_step("walk", exc, description="audit walk")
            )
        try:
            report = persist_punch_list(
                db_path=db_path,
                job_id=job_id,
                artifact_root=artifact_root,
                punch_list=punch,
            )
        except Exception as exc:  # noqa: BLE001
            return WorkerResult(
                False,
                JOB_TYPE,
                {},
                retryable=False,
                error=f"punch list could not be persisted: {type(exc).__name__}: {exc}",
            )
        report_id = str(report.get("id") or f"report:{job_id}")
        destination = audit_report_destination(report_id, punch)
        destination["stored_path"] = str(report.get("stored_path") or "")
        return WorkerResult(
            True,
            JOB_TYPE,
            destination,
            detail={
                "punch_list": punch.as_dict(),
                "overall": punch.overall,
                "stopped_before": list(STOP_BEFORE),
                "finance_agreement": False,
            },
            retryable=False,
        )


def _fixture_walk(payload: dict[str, Any]) -> list[PunchStep]:
    """CI / isolated walk. No live Ascend. Exercises the real PDF artifact path."""
    job_id = str(payload.get("job_id") or "")
    db_path = str(payload.get("db_path") or "")
    artifact_root = str(payload.get("artifact_root") or "")
    steps: list[PunchStep] = []
    injected = list(payload.get("fixture_steps") or [])
    if injected:
        for item in injected:
            if isinstance(item, PunchStep):
                steps.append(item)
                continue
            status = str(item.get("status") or "FAIL")
            error = item.get("error")
            step_id = str(item.get("id") or "step")
            if status == "PASS":
                steps.append(
                    pass_step(
                        step_id,
                        locator=str(item.get("locator") or ""),
                        artifact_path=str(item.get("artifact_path") or ""),
                        description=str(item.get("description") or ""),
                    )
                )
            else:
                steps.append(
                    fail_step(
                        step_id,
                        error or "FAIL",
                        locator=str(item.get("locator") or ""),
                        artifact_path=str(item.get("artifact_path") or ""),
                        description=str(item.get("description") or ""),
                    )
                )
        return steps
    from .ascend_create_defaults import (
        CREATE_PATH,
        CREATE_URL,
        PAWIVA_SPINNER_SECONDS,
        TEST_AGENCY_FEE,
        WAIT_FOR_URL,
        classify_document_labels,
        log_role_defaults,
        refuse_unexpected_agency_fee_default,
        require_create_form_seconds_logged,
        require_create_new_url,
        require_spinner_seconds_logged,
        test_agency_fee_set_value,
    )
    from .ascend_sender_roles import (
        NEW_PROGRAM_LOCATOR,
        ROBIE_AI,
        requested_by_from_payload,
        roles_for_requested_by,
    )

    sender = requested_by_from_payload(payload)
    roles = roles_for_requested_by(sender)
    fixture_seconds = payload.get("fixture_spinner_seconds", PAWIVA_SPINNER_SECONDS)
    fixture_fee_default = payload.get("fixture_agency_fee_default", "$0.00")
    fixture_labels = classify_document_labels(
        payload.get("fixture_document_labels") or "Import document"
    )
    fixture_form_seconds = payload.get("fixture_create_form_seconds", 2.0)
    for spec in FLOW_STEPS:
        if spec["id"].startswith("quote_pdf"):
            continue
        if spec["id"] == "wait_programs_ready":
            leak = require_spinner_seconds_logged(fixture_seconds)
            observed = {"seconds": fixture_seconds, "primary": NEW_PROGRAM_ACCESSIBLE_NAME}
            if leak:
                steps.append(
                    fail_step(spec["id"], leak, locator=spec["locator"], observed=observed)
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=f"{spec['description']}: {fixture_seconds}s",
                        observed=observed,
                    )
                )
            continue
        if spec["id"] in {"producer_role", "account_manager_role"}:
            prefill = str(payload.get("fixture_role_default") or ROBIE_AI)
            logged = log_role_defaults(
                requested_by=sender,
                producer=prefill if spec["id"] == "producer_role" else str(roles.get("producer") or prefill),
                account_manager=(
                    prefill
                    if spec["id"] == "account_manager_role"
                    else str(roles.get("account_manager") or prefill)
                ),
            )
            if roles.get("hitl_required") or not roles.get("resolved"):
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=(
                            "HITL required; did not write Robie AI "
                            f"({roles.get('hitl_text') or 'unknown requested_by'})"
                        ),
                        observed={
                            "producer_default": logged.get("producer_default"),
                            "account_manager_default": logged.get("account_manager_default"),
                            "requested_by": sender,
                        },
                    )
                )
                continue
            leak = logged.get("error")
            # Prefill Robie AI is expected; FAIL only if we would leave it.
            if leak and prefill == ROBIE_AI:
                leak = None
            overwritten = log_role_defaults(
                requested_by=sender,
                producer=str(roles.get("producer") or ""),
                account_manager=str(roles.get("account_manager") or ""),
            )
            leak = overwritten.get("error") or leak
            observed = {
                "producer_default": prefill if spec["id"] == "producer_role" else overwritten.get("producer_default"),
                "account_manager_default": (
                    prefill
                    if spec["id"] == "account_manager_role"
                    else overwritten.get("account_manager_default")
                ),
                "set_value": roles.get("option") or roles.get("resolved"),
                "requested_by": sender,
            }
            if leak:
                steps.append(
                    fail_step(spec["id"], leak, locator=spec["locator"], observed=observed)
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=(
                            f"{spec['description']}: logged {prefill!r} then "
                            f"{roles.get('option') or roles['resolved']}"
                        ),
                        observed=observed,
                    )
                )
            continue
        if spec["id"] == "customer_type":
            from .ascend_customer_type import resolve_customer_type

            decision = resolve_customer_type(payload)
            if decision.get("hitl_required") or not decision.get("resolved"):
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=(
                            "HITL required; did not keep form default or guess from name "
                            f"({decision.get('hitl_text') or 'missing LOB'})"
                        ),
                    )
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=str(decision.get("locator") or spec["locator"]),
                        description=f"{spec['description']}: {decision['radio']}",
                    )
                )
            continue
        if spec["id"] == "import_document":
            leak = require_create_form_seconds_logged(fixture_form_seconds)
            observed = {
                **fixture_labels,
                "seconds": fixture_form_seconds,
                "primary": "Import document",
            }
            if leak:
                steps.append(
                    fail_step(spec["id"], leak, locator=spec["locator"], observed=observed)
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=(
                            f"{spec['description']}: {fixture_form_seconds}s "
                            f"{fixture_labels['labels']}"
                        ),
                        observed=observed,
                    )
                )
            continue
        if spec["id"] == "unique_listbox_options":
            from .ascend_create_combobox import (
                audit_fixture_comboboxes,
                combobox_payload_from_audit,
            )

            combobox_payload = combobox_payload_from_audit(
                {
                    **payload,
                    "requested_by": sender,
                    "producer": roles.get("option")
                    or roles.get("producer")
                    or payload.get("producer"),
                    "account_manager": roles.get("option")
                    or roles.get("account_manager")
                    or payload.get("account_manager"),
                }
            )
            if roles.get("option"):
                combobox_payload["producer"] = roles["option"]
                combobox_payload["account_manager"] = roles["option"]
            audit = audit_fixture_comboboxes(combobox_payload)
            observed = {
                "blocked_fields": audit.get("blocked_fields") or [],
                "fields": [
                    {
                        "field": item.get("field"),
                        "intended": item.get("intended"),
                        "match_count": item.get("match_count"),
                        "status": item.get("status"),
                    }
                    for item in audit.get("reports") or []
                ],
            }
            if not audit.get("ok"):
                blocked = ", ".join(audit.get("blocked_fields") or []) or "unknown"
                steps.append(
                    fail_step(
                        spec["id"],
                        f"PLAYWRIGHT_BLOCKED: create/new listbox not unique; "
                        f"blocked field: {blocked}",
                        locator=spec["locator"],
                        observed=observed,
                    )
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=f"{spec['description']}: {len(observed['fields'])} comboboxes unique",
                        observed=observed,
                    )
                )
            continue
        if spec["id"] == "new_program":
            url_error = require_create_new_url(CREATE_URL)
            observed = {
                "url": CREATE_URL,
                "wait_for_url": WAIT_FOR_URL,
                "path": CREATE_PATH,
            }
            if url_error:
                steps.append(
                    fail_step(
                        spec["id"],
                        url_error,
                        locator=NEW_PROGRAM_LOCATOR,
                        observed=observed,
                    )
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=NEW_PROGRAM_LOCATOR,
                        description=f"{spec['description']}: {WAIT_FOR_URL}",
                        observed=observed,
                    )
                )
            continue
        if spec["id"] == "agency_fee":
            leak = refuse_unexpected_agency_fee_default(
                fixture_fee_default, field_present=True
            )
            observed = {
                "default": fixture_fee_default,
                "set_value": test_agency_fee_set_value(),
                "field_present": True,
            }
            if leak:
                steps.append(
                    fail_step(spec["id"], leak, locator=spec["locator"], observed=observed)
                )
            else:
                steps.append(
                    pass_step(
                        spec["id"],
                        locator=spec["locator"],
                        description=(
                            f"{spec['description']}: default {fixture_fee_default!r} "
                            f"set {TEST_AGENCY_FEE}"
                        ),
                        observed=observed,
                    )
                )
            continue
        if spec["id"] == "stop_before_save":
            steps.append(
                pass_step(
                    spec["id"],
                    locator=spec["locator"],
                    description=spec["description"],
                    observed={"stop_before": list(STOP_BEFORE)},
                )
            )
            continue
        steps.append(
            pass_step(
                spec["id"],
                locator=spec["locator"],
                description=spec["description"],
            )
        )
    try:
        record = save_and_lookup_quote_pdf(
            db_path=db_path,
            job_id=job_id,
            artifact_root=artifact_root,
            message_id=str(payload.get("message_id") or "fixture"),
        )
        stored = str(record.get("stored_path") or "")
        steps.append(pass_step("quote_pdf_save", artifact_path=stored))
        steps.append(pass_step("quote_pdf_lookup", artifact_path=stored))
        steps.append(pass_step("quote_pdf_open", artifact_path=stored))
    except (ArtifactPathError, MissingQuotePdfError, ProductionGuardError) as exc:
        steps.append(fail_step("quote_pdf_save", exc, artifact_path=artifact_root))
        steps.append(fail_step("quote_pdf_lookup", exc, artifact_path=artifact_root))
        steps.append(fail_step("quote_pdf_open", exc, artifact_path=artifact_root))
    return steps


def _live_walk(payload: dict[str, Any]) -> list[PunchStep]:
    from .ascend_locator_audit_runner import run_live_walk

    return run_live_walk(payload)


class AscendLocatorAuditVerifier:
    """Reread the punch-list report. Never COMPLETE a finance agreement."""

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        payload = dict(job.get("payload") or {})
        report_id = str(
            destination.get("report_id")
            or payload.get("report_id")
            or ""
        )
        expected = {
            "report_id": report_id,
            "kind": REPORT_KIND,
            "finance_agreement": False,
        }
        try:
            refuse_finance_agreement_complete(destination)
            stored = str(destination.get("stored_path") or "")
            if not stored:
                raise MissingQuotePdfError("audit report stored_path is missing")
            observed_report = json.loads(Path(stored).read_text(encoding="utf-8"))
            observed = {
                "report_id": report_id,
                "kind": str(observed_report.get("kind") or ""),
                "finance_agreement": bool(observed_report.get("finance_agreement")),
                "outcome": observed_report.get("overall"),
                "stored_path": stored,
            }
            verified = (
                bool(report_id)
                and observed["kind"] == REPORT_KIND
                and observed["finance_agreement"] is False
                and Path(stored).is_file()
            )
            error = None if verified else "audit report destination did not match"
        except Exception as exc:  # noqa: BLE001 — verifier fail-closed
            observed = {
                "report_id": report_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
            verified = False
            error = "audit report could not be reread"
        evidence = VerificationEvidence(
            method="AUDIT_REPORT_REREAD",
            source="ascend-locator-artifact-audit",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_utc_now(),
            locator=report_id or None,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)


def run_concat_job_id_eb96f620_assertion(*, work_dir: Path) -> dict[str, Any]:
    """Deterministic FAIL for Production job eb96f620 sliced+concat lookup."""
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing artifact-path concat assertion on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = PRODUCTION_EB96_JOB_ID
    artifact_id = PRODUCTION_EB96_ARTIFACT_ID
    wrong_folder = sliced_concat_job_folder(job_id, artifact_id)
    if wrong_folder != PRODUCTION_EB96_WRONG_FOLDER:
        return {
            "ok": False,
            "error": (
                f"sliced_concat_job_folder reproduced {wrong_folder!r}, "
                f"expected {PRODUCTION_EB96_WRONG_FOLDER!r}"
            ),
        }
    artifacts = Path(work_dir) / "artifacts"
    legal_dir = worker_lookup_artifact_dir(
        artifacts, job_id, artifact_id=artifact_id
    )
    if legal_dir != artifacts / job_id:
        return {
            "ok": False,
            "error": f"legal lookup dir {legal_dir} is not {{artifact_root}}/{{full_job_id}}/",
            "legal_dir": str(legal_dir),
            "wrong_dir": str(artifacts / wrong_folder),
        }
    legal_dir.mkdir(parents=True, exist_ok=True)
    pdf = legal_dir / f"{artifact_id}-quote.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    wrong_dir = artifacts / wrong_folder
    wrong_dir.mkdir(parents=True, exist_ok=True)
    wrong_pdf = wrong_dir / f"{artifact_id}-quote.pdf"
    wrong_pdf.write_bytes(MINIMAL_PDF)
    if not looks_concatenated_job_id(wrong_folder):
        return {
            "ok": False,
            "error": "Production concat folder was not classified as concatenated",
            "legal_dir": str(legal_dir),
            "wrong_dir": str(wrong_dir),
        }
    if not is_sliced_job_id_plus_artifact_id(wrong_folder, job_id, artifact_id):
        return {
            "ok": False,
            "error": "Production folder was not detected as job_id[:n]+artifact_id",
            "legal_dir": str(legal_dir),
            "wrong_dir": str(wrong_dir),
        }
    try:
        assert_artifact_path_matches_job_id(
            wrong_pdf,
            job_id=job_id,
            artifact_root=artifacts,
            artifact_id=artifact_id,
        )
        return {
            "ok": False,
            "error": "Production concat lookup path was accepted",
            "legal_dir": str(legal_dir),
            "wrong_dir": str(wrong_dir),
        }
    except ArtifactPathError:
        pass
    assert_artifact_path_matches_job_id(
        pdf,
        job_id=job_id,
        artifact_root=artifacts,
        artifact_id=artifact_id,
    )
    assert_quote_pdf_openable(pdf)
    return {
        "ok": True,
        "legal_dir": str(legal_dir),
        "wrong_dir": str(wrong_dir),
        "job_id": job_id,
        "artifact_id": artifact_id,
    }


def run_concat_job_id_eb96f620_scenario(*, work_dir: Path) -> dict[str, Any]:
    """Named scenario: artifact-path:concat-job-id-eb96f620. Isolated only."""
    try:
        result = run_concat_job_id_eb96f620_assertion(work_dir=work_dir)
    except Exception as exc:  # noqa: BLE001
        return {
            "id": CONCAT_SCENARIO_ID,
            "kind": "logic",
            "ok": False,
            "outcome": "FAILED",
            "evidence": f"{type(exc).__name__}: {exc}",
            "finance_agreement": False,
        }
    ok = bool(result.get("ok"))
    return {
        "id": CONCAT_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            f"lookup must be {{artifact_root}}/{PRODUCTION_EB96_JOB_ID}/; "
            f"job_id[:-11]+artifact_id = {PRODUCTION_EB96_WRONG_FOLDER} is FAIL"
            if ok
            else str(result.get("error") or "concat lookup was accepted")
        ),
        "job_id": PRODUCTION_EB96_JOB_ID,
        "finance_agreement": False,
    }


def run_ci_assertion_battery(*, work_dir: Path) -> dict[str, Any]:
    """Named scenario for GitHub CI. No live Ascend. No Production paths."""
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing ascend locator audit on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "ascend-audit.db")
    artifacts = str(work_dir / "artifacts")
    store = JobStore(db)
    job = store.create_job(
        JOB_TYPE,
        {
            **default_audit_payload(live=False),
            "worker": WORKER_NAME,
            "db_path": db,
            "artifact_root": artifacts,
        },
        idempotency_key=f"ascend-audit:{uuid4()}",
    )
    job_id = job["id"]
    checks: list[PunchStep] = []

    unique = classify_locator_failure(
        RuntimeError("strict mode violation: get_by_role('button') resolved to 2 elements"),
        locator='get_by_role("button", name="New program")',
        step_id="unique_locator",
        description="unique-locator fail is FAIL",
    )
    if unique.status == "FAIL" and "strict mode violation" in str(unique.error or ""):
        checks.append(pass_step("unique_locator", locator=unique.locator))
    else:
        checks.append(fail_step("unique_locator", "unique-locator fail was not classified as FAIL"))

    try:
        require_unique_locator(_CountTarget(2), locator='get_by_label("Insured")')
        checks.append(fail_step("unique_locator_count", "non-unique locator was accepted"))
    except UniqueLocatorError as exc:
        checks.append(pass_step("unique_locator_count", locator=str(exc)))

    try:
        require_unique_locator(_CountTarget(1), locator='get_by_label("Agency Fee")')
        checks.append(pass_step("unique_locator_ok", locator='get_by_label("Agency Fee")'))
    except UniqueLocatorError as exc:
        checks.append(fail_step("unique_locator_ok", exc))

    positional = classify_locator_failure(
        RuntimeError("matched"),
        locator='page.locator("button").first',
        step_id="positional_guess",
    )
    checks.append(
        pass_step("positional_guess")
        if positional.status == "FAIL" and "positional" in str(positional.error)
        else fail_step("positional_guess", "positional guess was not FAIL")
    )

    concat_id = f"{job_id}{job_id}"
    concat_dir = Path(artifacts) / concat_id
    concat_dir.mkdir(parents=True, exist_ok=True)
    concat_pdf = concat_dir / "quote.pdf"
    concat_pdf.write_bytes(MINIMAL_PDF)
    try:
        assert_artifact_path_matches_job_id(
            concat_pdf, job_id=job_id, artifact_root=artifacts
        )
        checks.append(fail_step("concatenated_job_id", "concatenated folder was accepted"))
    except ArtifactPathError as exc:
        checks.append(pass_step("concatenated_job_id", artifact_path=str(concat_pdf)))
        if "concatenat" not in str(exc).casefold() and job_id not in str(exc):
            checks[-1] = fail_step("concatenated_job_id", exc, artifact_path=str(concat_pdf))

    try:
        prod = run_concat_job_id_eb96f620_assertion(work_dir=work_dir / "eb96f620")
        if prod.get("ok"):
            checks.append(
                pass_step(
                    "eb96f620_sliced_concat",
                    artifact_path=str(prod.get("legal_dir") or ""),
                )
            )
        else:
            checks.append(
                fail_step(
                    "eb96f620_sliced_concat",
                    prod.get("error") or "Production concat path was accepted",
                    artifact_path=str(prod.get("wrong_dir") or ""),
                )
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(fail_step("eb96f620_sliced_concat", exc))

    from .ascend_create_combobox import run_unique_listbox_option_scenario
    from .ascend_create_defaults import (
        run_agency_fee_default_scenario,
        run_role_default_log_scenario,
        run_spinner_timing_scenario,
        run_too_soon_zero_element_scenario,
    )
    from .ascend_customer_type import run_customer_type_lob_scenario
    from .ascend_sender_roles import (
        run_sender_not_robie_ai_scenario,
        run_wait_spinner_scenario,
    )

    roles_report = run_sender_not_robie_ai_scenario()
    checks.append(
        pass_step("sender_not_robie_ai")
        if roles_report.get("ok")
        else fail_step("sender_not_robie_ai", roles_report.get("evidence") or "FAIL")
    )
    role_defaults = run_role_default_log_scenario()
    checks.append(
        pass_step("role_defaults_logged")
        if role_defaults.get("ok")
        else fail_step("role_defaults_logged", role_defaults.get("evidence") or "FAIL")
    )
    spinner_report = run_wait_spinner_scenario()
    checks.append(
        pass_step("wait_spinner")
        if spinner_report.get("ok")
        else fail_step("wait_spinner", spinner_report.get("evidence") or "FAIL")
    )
    timing_report = run_spinner_timing_scenario()
    checks.append(
        pass_step("spinner_timing")
        if timing_report.get("ok")
        else fail_step("spinner_timing", timing_report.get("evidence") or "FAIL")
    )
    too_soon_report = run_too_soon_zero_element_scenario()
    checks.append(
        pass_step("too_soon_zero_element")
        if too_soon_report.get("ok")
        else fail_step(
            "too_soon_zero_element", too_soon_report.get("evidence") or "FAIL"
        )
    )
    fee_report = run_agency_fee_default_scenario()
    checks.append(
        pass_step("agency_fee_default")
        if fee_report.get("ok")
        else fail_step("agency_fee_default", fee_report.get("evidence") or "FAIL")
    )
    listbox_report = run_unique_listbox_option_scenario()
    checks.append(
        pass_step("unique_listbox_option")
        if listbox_report.get("ok")
        else fail_step("unique_listbox_option", listbox_report.get("evidence") or "FAIL")
    )
    customer_report = run_customer_type_lob_scenario()
    checks.append(
        pass_step("customer_type_lob")
        if customer_report.get("ok")
        else fail_step("customer_type_lob", customer_report.get("evidence") or "FAIL")
    )

    missing = Path(artifacts) / job_id / "missing-quote.pdf"
    try:
        assert_quote_pdf_openable(missing)
        checks.append(fail_step("missing_pdf", "missing PDF was accepted"))
    except MissingQuotePdfError:
        checks.append(pass_step("missing_pdf", artifact_path=str(missing)))

    try:
        record = save_and_lookup_quote_pdf(
            db_path=db,
            job_id=job_id,
            artifact_root=artifacts,
        )
        stored = str(record["stored_path"])
        assert_artifact_path_matches_job_id(
            stored, job_id=job_id, artifact_root=artifacts
        )
        assert_quote_pdf_openable(stored)
        checks.append(pass_step("chat_pdf_roundtrip", artifact_path=stored))
    except Exception as exc:  # noqa: BLE001
        checks.append(fail_step("chat_pdf_roundtrip", exc))

    try:
        refuse_complete_without_destination_evidence(
            expected={"ok": True},
            observed={"ok": True},
            locator=None,
            job_id=job_id,
        )
        checks.append(fail_step("complete_without_evidence", "COMPLETE was allowed"))
    except PermissionError:
        checks.append(pass_step("complete_without_evidence"))

    try:
        refuse_finance_agreement_complete(
            {"finance_agreement": True, "program_id": "prog-1"}
        )
        checks.append(fail_step("no_finance_complete", "finance COMPLETE was allowed"))
    except FinanceCompleteError:
        checks.append(pass_step("no_finance_complete"))

    worker = AscendLocatorAuditWorker(artifact_root=artifacts)
    live_job = dict(job)
    live_job["db_path"] = db
    live_job["payload"] = dict(job["payload"])
    live_job["payload"]["db_path"] = db
    live_job["payload"]["artifact_root"] = artifacts
    result = worker.perform(live_job, idempotency_key=job["idempotency_key"])
    if not result.succeeded:
        checks.append(fail_step("worker_report", result.error or "worker failed"))
    else:
        try:
            refuse_finance_agreement_complete(result.destination)
            checks.append(pass_step("worker_report", artifact_path=str(result.destination.get("stored_path") or "")))
        except FinanceCompleteError as exc:
            checks.append(fail_step("worker_report", exc))

    punch = PunchList(job_id=job_id, steps=checks)
    ok = punch.overall == "PASS"
    return {
        "id": SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": punch.format_text(),
        "punch_list": punch.as_dict(),
        "job_id": job_id,
        "finance_agreement": False,
    }


class _CountTarget:
    def __init__(self, count: int, selector: str = "label") -> None:
        self._count = count
        self._selector = selector

    def count(self) -> int:
        return self._count


def run_live_test_job(
    *,
    db_path: str,
    artifact_root: str,
    requested_by: str = "",
    quote_path: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Full Job Engine job on hermes-test-01. Never Production. Never CI live."""
    refuse_production_env()
    refuse_live_ci()
    if current_robie_env() != TEST_ENV_NAME:
        raise ProductionGuardError("live walk requires ROBIE_ENV=TEST")
    refuse_production_targets(db_path, artifact_root)
    store = JobStore(db_path)
    payload = {
        **default_audit_payload(
            live=True, requested_by=requested_by, quote_path=quote_path
        ),
        "worker": WORKER_NAME,
        "db_path": db_path,
        "artifact_root": artifact_root,
    }
    if str(run_id or "").strip():
        payload["operator_run_id"] = str(run_id).strip()
    job = store.create_job(
        JOB_TYPE,
        payload,
        idempotency_key=live_operator_idempotency_key(run_id),
    )
    job["db_path"] = db_path
    worker = AscendLocatorAuditWorker(artifact_root=artifact_root)
    result = worker.perform(job, idempotency_key=job["idempotency_key"])
    overall = ""
    if result.detail:
        overall = str(result.detail.get("overall") or "")
    if result.succeeded and overall.upper() == "PASS":
        from .action_gate import CREATE_PROGRAM_ACTION, maybe_record_live_test_punch_list

        maybe_record_live_test_punch_list(
            action_id=CREATE_PROGRAM_ACTION,
            job_id=str(job["id"]),
            overall=overall,
            live=True,
            extra={"job_type": JOB_TYPE, "never_pawiva": True},
        )
    return {
        "job_id": job["id"],
        "succeeded": result.succeeded,
        "destination": result.destination,
        "detail": result.detail,
        "error": result.error,
        "finance_agreement": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ascend locator + artifact audit. CI asserts the punch list and "
            "artifact path. --live is Test-VM only. Does not bind, email, "
            "Save program, or flip Production."
        )
    )
    parser.add_argument("--ci", action="store_true", help="Fixture assertions only. No live Ascend.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Walk Ascend on hermes-test-01 (ROBIE_ENV=TEST). Never GitHub CI.",
    )
    parser.add_argument("--db", default="")
    parser.add_argument("--artifact-root", default="")
    parser.add_argument("--work-dir", default="")
    parser.add_argument(
        "--requested-by",
        default="",
        help="Chat sender (Carlo Ferrara / Jake Ferrara). Empty is dry HITL.",
    )
    parser.add_argument(
        "--quote",
        default="",
        help=(
            "Test-account quote to Import (pdf/txt). Carrier / State / "
            "Coverage type come from this file. Never PAWIVA. Empty leaves "
            "those fields HITL and continues to Agency Fee."
        ),
    )
    parser.add_argument(
        "--run-id",
        default="",
        help=(
            "Explicit operator-run id. A new value creates a fresh Test job; "
            "reusing the same value remains idempotent."
        ),
    )
    args = parser.parse_args(argv)
    if args.live:
        refuse_production_env()
        refuse_live_ci()
        db = args.db or os.environ.get("ROBIE_JOB_DB") or ""
        artifacts = args.artifact_root or os.environ.get("ROBIE_ARTIFACT_ROOT") or ""
        if not db or not artifacts:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "live walk requires --db and --artifact-root on hermes-test-01",
                    },
                    sort_keys=True,
                )
            )
            return 2
        report = run_live_test_job(
            db_path=db,
            artifact_root=artifacts,
            requested_by=str(args.requested_by or ""),
            quote_path=str(args.quote or ""),
            run_id=str(args.run_id or ""),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("succeeded") else 2
    work = Path(args.work_dir) if args.work_dir else Path(".robie-durable-test") / "ascend-locator-audit"
    report = run_ci_assertion_battery(work_dir=work)
    print(json.dumps({k: report[k] for k in ("id", "ok", "outcome", "evidence") if k in report}, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
