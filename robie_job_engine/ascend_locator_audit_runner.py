"""Live Ascend new-program walk for hermes-test-01 only.

GitHub CI must not import Playwright or talk to Ascend. This module lazy-imports
Playwright inside ``run_live_walk``. Unique locator required. Strict-mode
violation is FAIL. No Gemini. No .first / .nth / .last. Stop before Save
program, Send email, Copy checkout, payment, or bind.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .ascend_locator_audit import (
    FLOW_STEPS,
    PROGRAMS_URL,
    PunchStep,
    UniqueLocatorError,
    fail_step,
    pass_step,
    refuse_forbidden_account,
    refuse_live_ci,
    refuse_production_env,
    require_unique_locator,
    save_and_lookup_quote_pdf,
)
from .quote_replay import refuse_production_targets
from .runtime_env import TEST_ENV_NAME, current_robie_env


CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL") or os.environ.get(
    "ROBIE_BROWSER_CDP_URL", "http://127.0.0.1:9222"
)


def _locator_text(role: str, name: str, *, exact: bool = False) -> str:
    flag = ", exact=True" if exact else ""
    return f'get_by_role("{role}", name="{name}"{flag})'


def _label_text(label: str) -> str:
    return f'get_by_label("{label}")'


def _resolve(page: Any, spec_id: str, payload: dict[str, Any]) -> tuple[Any, str]:
    exact_address = str(payload.get("exact_address") or payload.get("test_address") or "")
    mapping: dict[str, tuple[Callable[[], Any], str]] = {
        "new_program": (
            lambda: page.get_by_role("button", name="New program"),
            _locator_text("button", "New program"),
        ),
        "commercial_customer": (
            lambda: page.get_by_role("radio", name="Commercial"),
            _locator_text("radio", "Commercial"),
        ),
        "import_document": (
            lambda: page.get_by_role("button", name="Import document"),
            _locator_text("button", "Import document"),
        ),
        "insured_fields": (
            lambda: page.get_by_label("Insured"),
            _label_text("Insured"),
        ),
        "address_autocomplete": (
            lambda: page.get_by_role("option", name=exact_address, exact=True)
            if exact_address
            else page.get_by_label("Address"),
            _locator_text("option", exact_address or "exact_address", exact=True)
            if exact_address
            else _label_text("Address"),
        ),
        "quote_number": (
            lambda: page.get_by_label("Quote number"),
            _label_text("Quote number"),
        ),
        "carrier": (lambda: page.get_by_label("Carrier"), _label_text("Carrier")),
        "wholesaler": (
            lambda: page.get_by_label("Wholesaler"),
            _label_text("Wholesaler"),
        ),
        "coverage_type": (
            lambda: page.get_by_label("Coverage type"),
            _label_text("Coverage type"),
        ),
        "dates": (
            lambda: page.get_by_label("Effective date"),
            _label_text("Effective date"),
        ),
        "premium": (lambda: page.get_by_label("Premium"), _label_text("Premium")),
        "taxes": (lambda: page.get_by_label("Taxes"), _label_text("Taxes")),
        "agency_fee": (
            lambda: page.get_by_label("Agency Fee"),
            _label_text("Agency Fee"),
        ),
        "stop_before_save": (
            lambda: page.get_by_role("button", name="Save program"),
            _locator_text("button", "Save program"),
        ),
    }
    builder, text = mapping[spec_id]
    return builder(), text


def _act(page: Any, spec_id: str, target: Any, payload: dict[str, Any]) -> None:
    require_unique_locator(target)
    if spec_id == "stop_before_save":
        return
    if spec_id == "new_program":
        target.click()
        return
    if spec_id == "commercial_customer":
        target.check() if hasattr(target, "check") else target.click()
        return
    if spec_id == "import_document":
        target.click()
        return
    if spec_id == "address_autocomplete":
        if str(payload.get("exact_address") or ""):
            target.click()
        return
    fill = getattr(target, "fill", None)
    value = {
        "insured_fields": str(payload.get("test_insured") or "ROBIE Test LLC"),
        "quote_number": str(payload.get("test_quote_number") or "TEST-ASCEND-AUDIT"),
        "carrier": str(payload.get("test_carrier") or "Test Carrier"),
        "wholesaler": str(payload.get("test_wholesaler") or "Test Wholesaler"),
        "coverage_type": str(payload.get("test_coverage") or "Commercial Auto"),
        "dates": str(payload.get("test_effective") or "01/01/2027"),
        "premium": str(payload.get("test_premium") or "1000"),
        "taxes": str(payload.get("test_taxes") or "50"),
        "agency_fee": str(payload.get("test_agency_fee") or "350"),
    }.get(spec_id)
    if callable(fill) and value is not None:
        fill(value)


def run_live_walk(payload: dict[str, Any]) -> list[PunchStep]:
    """Walk Ascend with Playwright strict mode. Test-VM only."""
    refuse_production_env()
    refuse_live_ci()
    if current_robie_env() != TEST_ENV_NAME:
        raise RuntimeError("live Ascend walk requires ROBIE_ENV=TEST on hermes-test-01")
    refuse_forbidden_account(
        payload.get("account"),
        payload.get("insured"),
        payload.get("client"),
        payload.get("text"),
    )
    db_path = str(payload.get("db_path") or "")
    artifact_root = str(payload.get("artifact_root") or "")
    job_id = str(payload.get("job_id") or "")
    refuse_production_targets(db_path, artifact_root)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return [
            fail_step(
                "open_programs",
                f"Playwright is not installed on this runner: {exc}",
                locator=f'page.goto("{PROGRAMS_URL}")',
            )
        ]

    steps: list[PunchStep] = []
    cdp = str(payload.get("cdp_url") or CDP_URL)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp)
            if not browser.contexts:
                raise RuntimeError("PLAYWRIGHT_BLOCKED: Chrome has no browser context")
            page = browser.contexts[0].new_page()
            try:
                page.goto(PROGRAMS_URL, wait_until="domcontentloaded")
                steps.append(
                    pass_step(
                        "open_programs",
                        locator=f'page.goto("{PROGRAMS_URL}")',
                    )
                )
            except Exception as exc:  # noqa: BLE001
                steps.append(
                    fail_step(
                        "open_programs",
                        exc,
                        locator=f'page.goto("{PROGRAMS_URL}")',
                    )
                )
                return steps
            for spec in FLOW_STEPS:
                spec_id = spec["id"]
                if spec_id in {"open_programs"} or spec_id.startswith("quote_pdf"):
                    continue
                locator = spec.get("locator") or ""
                try:
                    target, locator = _resolve(page, spec_id, payload)
                    require_unique_locator(target, locator=locator)
                    if spec_id == "stop_before_save":
                        steps.append(pass_step(spec_id, locator=locator))
                        continue
                    _act(page, spec_id, target, payload)
                    steps.append(pass_step(spec_id, locator=locator))
                except UniqueLocatorError as exc:
                    steps.append(fail_step(spec_id, exc, locator=locator))
                    return steps
                except Exception as exc:  # noqa: BLE001 — punch list records the exact error
                    steps.append(fail_step(spec_id, exc, locator=locator))
                    return steps
    except Exception as exc:  # noqa: BLE001
        steps.append(
            fail_step(
                "open_programs",
                exc,
                locator=f'page.goto("{PROGRAMS_URL}")',
            )
        )
        return steps

    try:
        record = save_and_lookup_quote_pdf(
            db_path=db_path,
            job_id=job_id,
            artifact_root=artifact_root,
            message_id=str(payload.get("message_id") or job_id),
        )
        stored = str(record.get("stored_path") or "")
        steps.append(pass_step("quote_pdf_save", artifact_path=stored))
        steps.append(pass_step("quote_pdf_lookup", artifact_path=stored))
        steps.append(pass_step("quote_pdf_open", artifact_path=stored))
    except Exception as exc:  # noqa: BLE001
        steps.append(fail_step("quote_pdf_save", exc, artifact_path=artifact_root))
        steps.append(fail_step("quote_pdf_lookup", exc, artifact_path=artifact_root))
        steps.append(fail_step("quote_pdf_open", exc, artifact_path=artifact_root))
    return steps
