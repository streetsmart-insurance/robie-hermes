"""Phase 1 document-puller pilot runner — read-only by construction.

For each pilot policy the runner dispatches the carrier's adapter, then
verifies the download as destination evidence: the file must exist, be
non-empty, and start with PDF magic bytes. A missing file, an empty
file, or a non-PDF is recorded as a failure — never as a download.

Read-only gate: before any adapter code runs, the runner asserts the
adapter's ``allowed_actions`` is a subset of ``READ_ONLY_ACTIONS``
(``portal_login``, ``portal_download``, ``api_read``). An adapter that
declares anything else (send, upload, call, write) raises
``ReadOnlyViolation`` and the policy is recorded as blocked.

Nothing in this module sends email, places calls, uploads files, writes
to EZLynx, or binds coverage. The only network writes permitted are the
adapter's own portal login and document download.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .phase1_adapters.base import (
    READ_ONLY_ACTIONS,
    AdapterSpec,
    DownloadResult,
    PolicyRef,
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
    status: str = ""  # "downloaded" | "checked" | "failed" | "blocked"
    file: str = ""  # run-dir-relative path, "" when no file
    sha256: str = ""
    bytes: int = 0
    downloaded_at: str = ""
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_pilot(
    pilot: list[dict],
    *,
    run_dir: str | Path,
    registry: dict | None = None,
    browser_factory: Callable[[str], Any] | None = None,
    ezlynx_client: Any | None = None,
    credential_accessor: Any | None = None,
) -> list[PolicyEvidence]:
    """Run the pilot. Returns per-policy evidence; never raises per-policy.

    ``browser_factory(runtime)`` supplies the BrowserPort for portal
    adapters (production: Playwright on the box/sandbox). ``ezlynx_client``
    supplies the PolicyApi client for api-runtime adapters. In tests both
    are fakes — no live portal is ever touched.
    """
    registry = registry or DEFAULT_REGISTRY
    run_path = Path(run_dir)
    docs_path = run_path / "docs"
    evidence_path = run_path / "evidence"
    docs_path.mkdir(parents=True, exist_ok=True)
    evidence_path.mkdir(parents=True, exist_ok=True)

    out: list[PolicyEvidence] = []
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
                if browser_factory is None:
                    raise RuntimeError(
                        f"no browser_factory wired for {spec.runtime}-runtime adapter"
                    )
                browser = browser_factory(spec.runtime)
                try:
                    result = module.download(
                        ref, docs_path, browser, credential_accessor
                    )
                finally:
                    close = getattr(browser, "close", None)
                    if callable(close):
                        close()
            if not result.ok:
                ev.status = "failed"
                ev.failure_reason = result.detail
                ev.detail = result.detail
                ev.extra = dict(result.extra)
            elif result.file_path is None:
                # api-style check: no file, structured verdict only
                ev.status = "checked"
                ev.detail = result.detail
                ev.extra = dict(result.extra)
            else:
                sha, size = verify_download(result.file_path)
                ev.status = "downloaded"
                ev.file = str(result.file_path.relative_to(run_path))
                ev.sha256 = sha
                ev.bytes = size
                ev.downloaded_at = _utcnow()
                ev.detail = result.detail
                ev.extra = dict(result.extra)
        except ReadOnlyViolation as exc:
            ev.status = "blocked"
            ev.failure_reason = str(exc)
            ev.detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - per-policy failure is evidence
            ev.status = "failed"
            ev.failure_reason = f"{type(exc).__name__}: {exc}"[:300]
            ev.detail = ev.failure_reason
        safe_name = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in ref.policy_number
        )
        (evidence_path / f"{safe_name}.json").write_text(
            json.dumps(ev.to_dict(), indent=2), encoding="utf-8"
        )
        out.append(ev)
    (run_path / "summary.json").write_text(
        json.dumps([e.to_dict() for e in out], indent=2), encoding="utf-8"
    )
    return out
