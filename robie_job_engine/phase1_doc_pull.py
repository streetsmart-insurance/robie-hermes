"""Phase 1 document-puller pilot runner — read-only by construction.

For each pilot policy the runner dispatches the carrier's adapter, then
verifies the download as destination evidence: the file must exist, be
non-empty, and start with PDF magic bytes. A missing file, an empty
file, or a non-PDF is recorded as a failure — never as a download.

Content check: when the PDF's text is extractable, the policy number
must appear in it (punctuation/whitespace/case-insensitive). Readable
text WITHOUT the policy number means the wrong document came down:
the policy is recorded ``needs_review`` and the file is KEPT as
evidence — never silently accepted. When text extraction fails the
check is INCONCLUSIVE (magic bytes already proved it is a PDF) and the
download stands, marked as such.

Read-only gate: before any adapter code runs, the runner asserts the
adapter's ``allowed_actions`` is a subset of ``READ_ONLY_ACTIONS``
(``portal_login``, ``portal_download``, ``api_read``). An adapter that
declares anything else (send, upload, call, write) raises
``ReadOnlyViolation`` and the policy is recorded as blocked.

Reliability:

- Credential preflight runs BEFORE any browser work: every portal
  carrier's Secret Manager refs are checked for EXISTENCE via metadata
  only (values never read, never logged). A missing ref fails closed —
  no browser launches, no adapter executes, portal policies are
  recorded failed with the reason.
- One browser context per carrier (``CarrierSession``): sign in once,
  reuse for that carrier's policies. Session expiry mid-flow gets
  exactly one fresh-context re-login; transient failures (timeouts,
  dropped connections) are retried with exponential backoff; permanent
  failures are recorded as evidence, never raised.
- Idempotent re-runs: a policy with valid prior evidence in the same
  run dir is skipped (status ``skipped``), never re-pulled.
- Per-policy timestamped action log (selectors only — never filled
  values) is attached to the evidence; a screenshot is saved on any
  failure.

Nothing in this module sends email, places calls, uploads files, writes
to EZLynx, or binds coverage. The only network writes permitted are the
adapter's own portal login and document download.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from .phase1_adapters.base import (
    READ_ONLY_ACTIONS,
    AdapterSpec,
    DownloadResult,
    PolicyRef,
    SessionExpiredError,
)
from .phase1_browser import CarrierSession, run_with_retries
from .phase1_credentials import (
    CredentialConfigurationError,
    credential_preflight,
)
from .phase1_adapters import amtrust, asi, njcrib, philadelphia, pie, progressive_bor, safeco, travelers

DEFAULT_REGISTRY = {
    "amtrust": amtrust,
    "njcrib": njcrib,
    "pie": pie,
    "travelers": travelers,
    "philadelphia": philadelphia,
    "safeco": safeco,
    "asi": asi,
    "progressive_bor": progressive_bor,
}

PDF_MAGIC = b"%PDF-"
REQUIRED_PILOT_FIELDS = (
    "policy_number",
    "insured_name",
    "carrier_id",
    "report",
    "doc_kind",
)

# Evidence statuses: "downloaded" | "checked" | "failed" | "blocked" |
# "needs_review" (readable PDF text lacks the policy number — file kept) |
# "skipped" (valid prior evidence already in this run dir).


class ReadOnlyViolation(RuntimeError):
    """An adapter declared a non-read-only action. The run refuses it."""


class EvidenceError(RuntimeError):
    """A claimed download failed destination-evidence verification."""


@dataclass
class PolicyEvidence:
    policy_number: str
    insured_name: str
    carrier_id: str
    carrier_name: str
    report: str
    doc_kind: str
    runtime: str
    status: str = ""
    file: str = ""  # run-dir-relative path, "" when no file
    sha256: str = ""
    bytes: int = 0
    downloaded_at: str = ""
    screenshot: str = ""  # run-dir-relative path of the failure screenshot
    detail: str = ""
    failure_reason: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def load_pilot(pilot_path: str | Path) -> list[dict]:
    """Load and validate the pilot JSON. Raises on any schema problem."""
    raw = json.loads(Path(pilot_path).read_text(encoding="utf-8"))
    policies = raw.get("policies")
    if not isinstance(policies, list) or not policies:
        raise ValueError("pilot JSON must contain a non-empty 'policies' list")
    for i, row in enumerate(policies):
        missing = [f for f in REQUIRED_PILOT_FIELDS if not str(row.get(f) or "").strip()]
        if missing:
            raise ValueError(f"pilot policy #{i} missing fields: {', '.join(missing)}")
        if row["carrier_id"] not in DEFAULT_REGISTRY:
            raise ValueError(
                f"pilot policy #{i} ({row.get('policy_number')}): "
                f"unknown carrier_id {row['carrier_id']!r}"
            )
    return policies


def verify_download(path: Path) -> tuple[str, int]:
    """Destination-evidence check: exists, non-empty, PDF magic bytes.

    Returns (sha256_hex, size_bytes). Raises EvidenceError on any failure.
    """
    if not path.is_file():
        raise EvidenceError(f"downloaded file missing: {path}")
    size = path.stat().st_size
    if size == 0:
        raise EvidenceError(f"downloaded file is empty: {path}")
    with open(path, "rb") as fh:
        head = fh.read(len(PDF_MAGIC))
    if head != PDF_MAGIC:
        raise EvidenceError(
            f"downloaded file is not a PDF (bad magic bytes): {path}"
        )
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def _normalize(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def pdf_contains_policy_number(path: Path, policy_number: str) -> bool | None:
    """Content check: does the PDF's extractable text mention the policy?

    Returns True (found), False (readable text, number absent), or None
    (INCONCLUSIVE — text extraction failed, so the check cannot speak;
    the magic-bytes check in verify_download already proved it is a PDF).
    Never raises: extraction problems are inconclusive, not verdicts.
    """
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)
    except Exception:
        return None
    if not text.strip():
        return None
    return _normalize(policy_number) in _normalize(text)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(policy_number: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in policy_number)


def _load_prior_evidence(
    evidence_path: Path, run_path: Path, policy_number: str
) -> PolicyEvidence | None:
    """Idempotent re-run: return prior evidence if it is still valid.

    Valid = status "downloaded" or "checked", and for "downloaded" the
    file still exists and still passes verify_download.
    """
    prior_file = evidence_path / f"{_safe_name(policy_number)}.json"
    if not prior_file.is_file():
        return None
    try:
        data = json.loads(prior_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("status") not in ("downloaded", "checked"):
        return None
    if data.get("status") == "downloaded":
        rel = data.get("file") or ""
        if not rel:
            return None
        try:
            verify_download(run_path / rel)
        except EvidenceError:
            return None
    ev = PolicyEvidence(
        policy_number=data.get("policy_number", ""),
        insured_name=data.get("insured_name", ""),
        carrier_id=data.get("carrier_id", ""),
        carrier_name=data.get("carrier_name", ""),
        report=data.get("report", ""),
        doc_kind=data.get("doc_kind", ""),
        runtime=data.get("runtime", ""),
        status="skipped",
        file=data.get("file", ""),
        sha256=data.get("sha256", ""),
        bytes=data.get("bytes", 0),
        downloaded_at=data.get("downloaded_at", ""),
        screenshot=data.get("screenshot", ""),
        detail=(
            f"already completed in this run dir "
            f"(prior status: {data.get('status')}); kept existing evidence"
        ),
        extra=dict(data.get("extra") or {}),
    )
    return ev


def run_pilot(
    pilot: list[dict],
    *,
    run_dir: str | Path,
    registry: dict | None = None,
    browser_factory: Callable[[str], Any] | None = None,
    ezlynx_client: Any | None = None,
    credential_accessor: Any | None = None,
    secret_exists: Callable[[str], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PolicyEvidence]:
    """Run the pilot. Returns per-policy evidence; never raises per-policy.

    ``browser_factory(runtime)`` supplies the BrowserPort for portal
    adapters (production: Playwright on the box/sandbox). ``ezlynx_client``
    supplies the PolicyApi client for api-runtime adapters. In tests both
    are fakes — no live portal is ever touched.

    ``secret_exists(ref)`` is the metadata-only credential existence
    check used by the preflight (production default verifies via Secret
    Manager metadata without reading values). ``sleep`` is injectable
    for tests.
    """
    registry = registry or DEFAULT_REGISTRY
    run_path = Path(run_dir)
    docs_path = run_path / "docs"
    evidence_path = run_path / "evidence"
    shots_path = run_path / "screenshots"
    docs_path.mkdir(parents=True, exist_ok=True)
    evidence_path.mkdir(parents=True, exist_ok=True)
    shots_path.mkdir(parents=True, exist_ok=True)

    # Credential preflight BEFORE any browser work. Fail closed: a
    # missing ref means no browser launches and no adapter executes —
    # every portal policy is recorded failed with the reason.
    preflight_missing = credential_preflight(pilot, registry, secret_exists)
    preflight_failed = bool(preflight_missing)

    sessions: dict[str, CarrierSession] = {}
    relogin_used: set[str] = set()  # carriers that already had their one re-login

    def _session_for(carrier_id: str, runtime: str) -> CarrierSession:
        session = sessions.get(carrier_id)
        if session is None:
            if browser_factory is None:
                raise RuntimeError(
                    f"no browser_factory wired for {runtime}-runtime adapter"
                )
            session = CarrierSession(carrier_id, runtime, browser_factory)
            sessions[carrier_id] = session
        return session

    def _maybe_screenshot(port: Any, policy_number: str) -> str:
        shot_fn = getattr(port, "screenshot", None)
        if not callable(shot_fn):
            return ""
        dest = shots_path / f"{_safe_name(policy_number)}.png"
        try:
            shot_fn(dest)
        except Exception:
            return ""
        return str(dest.relative_to(run_path))

    out: list[PolicyEvidence] = []
    try:
        for row in pilot:
            module = registry[row["carrier_id"]]
            spec: AdapterSpec = module.ADAPTER
            ref = PolicyRef(
                policy_number=row["policy_number"],
                insured_name=row["insured_name"],
                report=row["report"],
                doc_kind=row["doc_kind"],
            )
            ev = PolicyEvidence(
                policy_number=ref.policy_number,
                insured_name=ref.insured_name,
                carrier_id=spec.carrier_id,
                carrier_name=spec.carrier_name,
                report=ref.report,
                doc_kind=ref.doc_kind,
                runtime=spec.runtime,
            )
            # Idempotent re-run: valid prior evidence wins, no re-pull.
            prior = _load_prior_evidence(evidence_path, run_path, ref.policy_number)
            if prior is not None:
                out.append(prior)
                continue
            port: Any = None
            try:
                illegal = set(spec.allowed_actions) - set(READ_ONLY_ACTIONS)
                if illegal:
                    raise ReadOnlyViolation(
                        f"adapter {spec.carrier_id} declares non-read-only "
                        f"actions: {sorted(illegal)}"
                    )
                if spec.runtime == "api":
                    if ezlynx_client is None:
                        raise RuntimeError("no ezlynx_client wired for api-runtime adapter")
                    result: DownloadResult = module.check(
                        ref, docs_path, ezlynx_client, credential_accessor
                    )
                else:
                    if preflight_failed:
                        raise CredentialConfigurationError(
                            "credential preflight failed: "
                            + "; ".join(preflight_missing)
                        )
                    session = _session_for(spec.carrier_id, spec.runtime)
                    port = session.port()
                    reset_log = getattr(port, "reset_log", None)
                    if callable(reset_log):
                        reset_log()

                    def _attempt(p: Any) -> DownloadResult:
                        return run_with_retries(
                            lambda: module.download(
                                ref, docs_path, p, credential_accessor
                            ),
                            sleep=sleep,
                        )

                    try:
                        result = _attempt(port)
                    except SessionExpiredError:
                        # Exactly one fresh-context re-login per carrier.
                        # The adapter's login steps run again inside
                        # download() on the new context.
                        if spec.carrier_id in relogin_used:
                            raise SessionExpiredError(
                                f"portal session for {spec.carrier_id} expired "
                                "twice; re-login did not stick"
                            )
                        relogin_used.add(spec.carrier_id)
                        port = session.reset()
                        reset_log = getattr(port, "reset_log", None)
                        if callable(reset_log):
                            reset_log()
                        result = _attempt(port)
                    action_log = getattr(port, "action_log", None)
                    if action_log:
                        ev.extra["action_log"] = list(action_log)
                if not result.ok:
                    ev.status = "failed"
                    ev.failure_reason = result.detail
                    ev.detail = result.detail
                    ev.extra.update(dict(result.extra))
                    if port is not None:
                        ev.screenshot = _maybe_screenshot(port, ref.policy_number)
                elif result.file_path is None:
                    # api-style check: no file, structured verdict only
                    ev.status = "checked"
                    ev.detail = result.detail
                    ev.extra.update(dict(result.extra))
                else:
                    sha, size = verify_download(result.file_path)
                    ev.file = str(result.file_path.relative_to(run_path))
                    ev.sha256 = sha
                    ev.bytes = size
                    ev.downloaded_at = _utcnow()
                    ev.detail = result.detail
                    ev.extra.update(dict(result.extra))
                    content = pdf_contains_policy_number(
                        result.file_path, ref.policy_number
                    )
                    if content is True:
                        ev.status = "downloaded"
                        ev.extra["content_check"] = "policy number found in PDF text"
                    elif content is False:
                        ev.status = "needs_review"
                        ev.failure_reason = (
                            f"PDF text is readable but does not contain policy "
                            f"number {ref.policy_number} — wrong document?"
                        )
                        ev.detail = ev.failure_reason
                        ev.extra["content_check"] = (
                            "policy number NOT found in PDF text"
                        )
                        if port is not None:
                            ev.screenshot = _maybe_screenshot(port, ref.policy_number)
                    else:
                        ev.status = "downloaded"
                        ev.extra["content_check"] = (
                            "inconclusive: no extractable PDF text "
                            "(magic bytes verified)"
                        )
            except ReadOnlyViolation as exc:
                ev.status = "blocked"
                ev.failure_reason = str(exc)
                ev.detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - per-policy failure is evidence
                ev.status = "failed"
                ev.failure_reason = f"{type(exc).__name__}: {exc}"[:300]
                ev.detail = ev.failure_reason
                if port is not None:
                    ev.screenshot = _maybe_screenshot(port, ref.policy_number)
            (evidence_path / f"{_safe_name(ref.policy_number)}.json").write_text(
                json.dumps(ev.to_dict(), indent=2), encoding="utf-8"
            )
            out.append(ev)
    finally:
        for session in sessions.values():
            session.close()
    (run_path / "summary.json").write_text(
        json.dumps([e.to_dict() for e in out], indent=2), encoding="utf-8"
    )
    return out
