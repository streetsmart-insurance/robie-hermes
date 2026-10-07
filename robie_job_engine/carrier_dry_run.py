"""Carrier document dry-run orchestrator.

BUILT 2026-10-05 (not just designed).

Runs every carrier pull module in sequence in DRY-RUN mode: PDFs land in each
carrier's dated QA pack folder and ledgers update, but nothing is ever filed
to EZLynx and no email is ever sent. One carrier failing never stops the
others.

Safety posture (defense in depth — the pull modules already enforce these):
- ``ROBIE_ENV=TEST`` is required before anything runs.
- Production hosts (hermes-poc*) are refused outright.
- The filing kill switch ``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX`` is forced
  to ``0`` for the process.
- ``--upload-drive`` is never passed; every module fails closed on it anyway.
- No pull module calls the EZLynx DocumentApi/DiscussionApi or sends email
  from its ``run_pull`` path (verified by inspection 2026-10-05).

Browser lifecycle: one shared Playwright CDP connection is established, each
carrier's page-object wraps its own already-open portal tab (selected by that
carrier's own tab selector, which fails closed when the tab is missing or
ambiguous), and the connection is closed cleanly at the end. Tests inject a
``browser_factory`` instead and never touch CDP.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import re
import socket
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

KILL_SWITCH_ENV = "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
NATGEN_WINDOW_DAYS = 14


@dataclass(frozen=True)
class CarrierSpec:
    """Everything the orchestrator needs to run one carrier's pull."""

    name: str
    display: str
    module_name: str
    browser_cls_name: str
    ledger_cls_name: str
    select_fn_name: str
    # "pages" -> select(list_of_pages); "browser" -> select(cdp_browser)
    select_takes: str = "pages"
    # attribute on the module holding the default QA root
    default_root_attr: str = "DEFAULT_OUTPUT_ROOT"
    # "run_pull" or "natgen_adapter"
    runner: str = "run_pull"


CARRIER_ORDER = (
    "progressive",
    "progressive_bop",
    "guard",
    "geico",
    "travelers",
    "natgen",
    "uticafirst",
    "farmersofsalem",
)

SPECS: dict[str, CarrierSpec] = {
    "progressive": CarrierSpec(
        name="progressive",
        display="Progressive (FAO)",
        module_name=".progressive_pending_cancellation",
        browser_cls_name="PlaywrightFaoCancellationBrowser",
        ledger_cls_name="FaoCancellationLedger",
        select_fn_name="select_fao_page",
    ),
    "progressive_bop": CarrierSpec(
        name="progressive_bop",
        display="Progressive BOP",
        module_name=".progressive_bop",
        browser_cls_name="PlaywrightFaoBopBrowser",
        ledger_cls_name="LocalNocLedger",
        select_fn_name="select_fao_page",
    ),
    "guard": CarrierSpec(
        name="guard",
        display="Guard",
        module_name=".guard_pending_cancellation",
        browser_cls_name="PlaywrightGuardBrowser",
        ledger_cls_name="GuardDeliveryLedger",
        select_fn_name="select_guard_page",
    ),
    "geico": CarrierSpec(
        name="geico",
        display="GEICO",
        module_name=".geico_pending_cancellation_noc",
        browser_cls_name="PlaywrightGeicoNocBrowser",
        ledger_cls_name="LocalDeliveryLedger",
        select_fn_name="select_gateway_page",
    ),
    "travelers": CarrierSpec(
        name="travelers",
        display="Travelers",
        module_name=".travelers_pending_cancellation",
        browser_cls_name="PlaywrightTravelersBrowser",
        ledger_cls_name="TravelersDeliveryLedger",
        select_fn_name="select_travelers_page",
    ),
    "natgen": CarrierSpec(
        name="natgen",
        display="NatGen",
        module_name=".natgen_pending_cancellation",
        browser_cls_name="PlaywrightNatGenNocBrowser",
        ledger_cls_name="LocalDeliveryLedger",
        select_fn_name="select_natgen_page",
        default_root_attr="DEFAULT_QA_ROOT",
        runner="natgen_adapter",
    ),
    "uticafirst": CarrierSpec(
        name="uticafirst",
        display="Utica First",
        module_name=".utica_pending_cancellation",
        browser_cls_name="PlaywrightUticaCancellationBrowser",
        ledger_cls_name="UticaDeliveryLedger",
        select_fn_name="select_utica_page",
    ),
    "farmersofsalem": CarrierSpec(
        name="farmersofsalem",
        display="Farmers of Salem",
        module_name=".farmersofsalem_pending_cancellation",
        browser_cls_name="FinysFoSBrowser",
        ledger_cls_name="LocalDeliveryLedger",
        select_fn_name="_select_finys_page",
        select_takes="browser",
    ),
}


def _refuse_production_host() -> None:
    """Dry runs never execute on a Production host, no exceptions."""
    from .intake_core import IntakeHold

    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Carrier dry run refuses Production host")


def _require_dry_run_environment() -> None:
    """Enforce every dry-run gate before any carrier runs."""
    from .intake_core import IntakeHold, require_test

    require_test()  # ROBIE_ENV must be TEST
    _refuse_production_host()
    # Belt and suspenders: the filing kill switch stays off for dry runs.
    os.environ[KILL_SWITCH_ENV] = "0"


def _resolve_carrier_names(carriers: str | None) -> tuple[str, ...]:
    if carriers is None:
        return CARRIER_ORDER
    from .intake_core import IntakeHold

    names = tuple(n.strip().lower() for n in carriers.split(",") if n.strip())
    if not names:
        raise IntakeHold("Carrier list is empty; name at least one carrier")
    unknown = [n for n in names if n not in SPECS]
    if unknown:
        raise IntakeHold(f"Unknown carrier(s): {', '.join(unknown)}")
    # Preserve CARRIER_ORDER, drop duplicates.
    seen: set[str] = set()
    ordered = tuple(n for n in CARRIER_ORDER for n in (n,) if n in names and not (n in seen or seen.add(n)))
    return ordered


def _pack_dir(spec: CarrierSpec, output_root: str | None, as_of: date) -> Path:
    """Dated QA pack folder for one carrier: <root>/<YYYY-MM-DD>/."""
    mod = importlib.import_module(spec.module_name, package="robie_job_engine")
    if output_root:
        return Path(output_root) / as_of.isoformat()
    default_root = Path(getattr(mod, spec.default_root_attr))
    pack_fn = getattr(mod, "qa_pack_dir", None)
    if callable(pack_fn):
        return pack_fn(default_root, as_of)
    return default_root / as_of.isoformat()


def _make_ledger_archive(spec: CarrierSpec, pack: Path) -> tuple[Any, Any]:
    from .intake_core import SourceArchive

    mod = importlib.import_module(spec.module_name, package="robie_job_engine")
    ledger_cls = getattr(mod, spec.ledger_cls_name)
    ledger = ledger_cls(pack)
    ensure = getattr(ledger, "ensure_private", None)
    if callable(ensure):
        ensure()
    archive = SourceArchive(pack / "sources")
    return ledger, archive


def _wrap_browser(spec: CarrierSpec, cdp_browser: Any) -> Any:
    """Wrap the carrier's already-open portal tab in its page object."""
    mod = importlib.import_module(spec.module_name, package="robie_job_engine")
    select = getattr(mod, spec.select_fn_name)
    if spec.select_takes == "browser":
        page = select(cdp_browser)
    else:
        pages = [p for ctx in cdp_browser.contexts for p in ctx.pages]
        page = select(pages)
    cls = getattr(mod, spec.browser_cls_name)
    return cls(page)


def _run_natgen_pull(browser: Any, ledger: Any, archive: Any, *, as_of: date) -> dict[str, Any]:
    """NatGen has no run_pull; replicate its main()'s pull faithfully."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("natgen")
    mod = importlib.import_module(".natgen_pending_cancellation", package="robie_job_engine")
    retrieval_mod = importlib.import_module(".natgen_retrieval", package="robie_job_engine")
    start = as_of - timedelta(days=NATGEN_WINDOW_DAYS - 1)
    end = as_of
    portal = mod.NatGenPendingCancellationPortal(browser, ledger)
    items = retrieval_mod.NatGenRetrieval(None, archive).pull_pending_cancellation_noc(
        portal, start=start, end=end
    )
    downloaded: list[dict[str, Any]] = []
    for item in items:
        row = portal.noc(item.source_id)
        downloaded.append(
            {
                "document_id": item.source_id,
                "filename": item.filename,
                "sha256": item.digest,
                "bytes": len(item.content),
                "policy_number": row.policy_number,
                "insured_name": row.insured_name,
                "path": str(ledger.pdf_path(row.processed_on, item.filename)),
            }
        )
    return {
        "status": "PULLED",
        "count": len(downloaded),
        "downloaded": downloaded,
        "held": list(getattr(portal, "date_holds", []) or []),
        "skipped_already_delivered": list(getattr(portal, "skipped_document_ids", []) or []),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ezlynx": "not_run",
    }


def _run_standard_pull(spec: CarrierSpec, browser: Any, ledger: Any, archive: Any, *, as_of: date) -> dict[str, Any]:
    mod = importlib.import_module(spec.module_name, package="robie_job_engine")
    run_pull = getattr(mod, "run_pull")
    return run_pull(browser, ledger, archive, as_of=as_of)


@contextlib.contextmanager
def _shared_cdp(cdp_url: str | None) -> Iterator[Any]:
    """One Playwright CDP connection shared by every carrier in the run."""
    from .intake_core import IntakeHold

    url = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
        raise IntakeHold("Dry-run browser attach must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Dry-run browser attach must use the local Test CDP endpoint")
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        yield playwright.chromium.connect_over_cdp(url)
    finally:
        playwright.stop()


def _normalize_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    downloaded = receipt.get("downloaded", []) or []
    held = receipt.get("held", []) or []
    skipped = receipt.get("skipped_already_delivered", receipt.get("skipped", [])) or []
    count = receipt.get("count")
    if not isinstance(count, int):
        count = len(downloaded) if isinstance(downloaded, list) else 0
    return {
        "downloaded": count,
        "skipped": len(skipped) if isinstance(skipped, list) else 0,
        "held": held if isinstance(held, list) else [],
    }


def _run_one(
    spec: CarrierSpec,
    day: date,
    output_root: str | None,
    make_browser: Callable[[], Any],
) -> dict[str, Any]:
    """Run one carrier. Never raises: failures become HELD/FAILED results."""
    from .intake_core import IntakeHold

    pack = _pack_dir(spec, output_root, day)
    try:
        ledger, archive = _make_ledger_archive(spec, pack)
        browser = make_browser()
        if spec.runner == "natgen_adapter":
            receipt = _run_natgen_pull(browser, ledger, archive, as_of=day)
        else:
            receipt = _run_standard_pull(spec, browser, ledger, archive, as_of=day)
    except Exception as exc:  # noqa: BLE001 - one carrier must never stop the run
        details = getattr(exc, "details", None)
        if isinstance(exc, IntakeHold) or isinstance(details, dict):
            held = None
            if isinstance(details, dict):
                held = details.get("held")
            return {
                "display": spec.display,
                "status": "HELD",
                "downloaded": 0,
                "skipped": 0,
                "held": held if isinstance(held, list) and held else [{"reason": str(exc)}],
                "error": None,
                "pack": str(pack),
            }
        return {
            "display": spec.display,
            "status": "FAILED",
            "downloaded": 0,
            "skipped": 0,
            "held": [],
            "error": f"{type(exc).__name__}: {exc}",
            "pack": str(pack),
        }
    norm = _normalize_receipt(receipt if isinstance(receipt, dict) else {})
    return {
        "display": spec.display,
        "status": "OK",
        "downloaded": norm["downloaded"],
        "skipped": norm["skipped"],
        "held": norm["held"],
        "error": None,
        "pack": str(pack),
    }


def run_dry_run(
    carriers: str | None = None,
    *,
    as_of: date | None = None,
    output_root: str | None = None,
    cdp_url: str | None = None,
    browser_factory: Callable[[CarrierSpec], Any] | None = None,
) -> dict[str, Any]:
    """Run the carrier pulls in sequence. Returns the summary dict.

    ``browser_factory`` injects a per-carrier browser wrapper (tests/dev);
    when omitted, one shared CDP connection serves every carrier.
    """
    _require_dry_run_environment()
    day = as_of or date.today()
    names = _resolve_carrier_names(carriers)
    results: dict[str, dict[str, Any]] = {}
    if browser_factory is not None:
        for name in names:
            spec = SPECS[name]
            results[name] = _run_one(spec, day, output_root, lambda s=spec: browser_factory(s))
    else:
        with _shared_cdp(cdp_url) as cdp_browser:
            for name in names:
                spec = SPECS[name]
                results[name] = _run_one(spec, day, output_root, lambda s=spec: _wrap_browser(s, cdp_browser))
    totals = {"ok": 0, "held": 0, "failed": 0}
    for r in results.values():
        totals[{"OK": "ok", "HELD": "held", "FAILED": "failed"}[r["status"]]] += 1
    return {
        "as_of": day.isoformat(),
        "mode": "dry-run",
        "carriers": results,
        "totals": totals,
    }


def render_summary(summary: dict[str, Any]) -> str:
    """Plain-English one-screen summary of the dry run."""
    lines = [
        f"Carrier dry run for {summary['as_of']} (mode: {summary['mode']}; nothing filed to EZLynx, no emails sent)."
    ]
    for name in CARRIER_ORDER:
        r = summary["carriers"].get(name)
        if r is None:
            continue
        if r["status"] == "OK":
            extra = f", {len(r['held'])} held" if r["held"] else ""
            lines.append(f"- {r['display']}: OK — {r['downloaded']} downloaded, {r['skipped']} skipped{extra}.")
        elif r["status"] == "HELD":
            reason = r["held"][0].get("reason", "held") if r["held"] else "held"
            lines.append(f"- {r['display']}: HELD — {reason}")
        else:
            lines.append(f"- {r['display']}: FAILED — {r['error']}")
    t = summary["totals"]
    lines.append(f"Totals: {t['ok']} ok, {t['held']} held, {t['failed']} failed.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run every carrier document pull (TEST only; never files to EZLynx, never sends email)"
    )
    parser.add_argument(
        "--carriers",
        default=None,
        help=f"comma-separated subset of: {', '.join(CARRIER_ORDER)} (default: all)",
    )
    parser.add_argument("--as-of", default=date.today().isoformat(), help="pull date YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="override QA pack root (default: each carrier's own)")
    parser.add_argument("--cdp-url", default=None, help="CDP endpoint (default: ROBIE_BROWSER_CDP_URL or 127.0.0.1:9222)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        day = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"Bad --as-of date: {args.as_of!r} (use YYYY-MM-DD)", file=sys.stderr)
        return 2
    try:
        summary = run_dry_run(
            args.carriers,
            as_of=day,
            output_root=args.output_root,
            cdp_url=args.cdp_url,
        )
    except Exception as exc:  # noqa: BLE001 - environment gate failures
        print(f"Dry run refused: {exc}", file=sys.stderr)
        return 2
    print(render_summary(summary))
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0 if summary["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
