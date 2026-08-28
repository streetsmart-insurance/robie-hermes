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
from .ascend_sender_roles import (
    ACCOUNT_MANAGER_LOCATOR,
    COMMERCIAL_LOCATOR,
    IMPORT_DOCUMENT_LOCATOR,
    NEW_PROGRAM_LOCATOR,
    PRODUCER_LOCATOR,
    PROGRAMS_KPI_LOCATOR,
    PROGRAMS_READY_TIMEOUT_MS,
    PROGRAMS_TABLE_LOCATOR,
    locator_is_new_program_caret,
    programs_spinner_timeout_error,
    requested_by_from_payload,
    roles_for_requested_by,
    should_overwrite_role,
    unknown_sender_hitl,
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


def _new_program_target(page: Any) -> Any:
    return page.get_by_role("button", name="+ New program", exact=True)


def wait_programs_ready(page: Any, *, timeout_ms: int = PROGRAMS_READY_TIMEOUT_MS) -> None:
    """Do not click + New program until the spinner is gone and the page is usable."""
    button = _new_program_target(page)
    try:
        button.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(programs_spinner_timeout_error(str(exc))) from exc
    if locator_is_new_program_caret(NEW_PROGRAM_LOCATOR):
        raise UniqueLocatorError("refusing split-menu caret; unique + New program required")
    require_unique_locator(button, locator=NEW_PROGRAM_LOCATOR)
    enabled = getattr(button, "is_enabled", None)
    if callable(enabled) and not enabled():
        raise RuntimeError(programs_spinner_timeout_error("primary + New program is not enabled"))
    kpi = page.get_by_text("Programs at risk")
    table = page.get_by_role("table")
    kpi_count = int(kpi.count()) if callable(getattr(kpi, "count", None)) else 0
    table_count = int(table.count()) if callable(getattr(table, "count", None)) else 0
    if kpi_count < 1 and table_count < 1:
        raise RuntimeError(
            programs_spinner_timeout_error(
                "programs table or KPI cards are not present"
            )
        )


def _resolve(page: Any, spec_id: str, payload: dict[str, Any]) -> tuple[Any, str]:
    exact_address = str(payload.get("exact_address") or payload.get("test_address") or "")
    mapping: dict[str, tuple[Callable[[], Any], str]] = {
        "wait_programs_ready": (
            lambda: page.get_by_text("Programs at risk"),
            PROGRAMS_KPI_LOCATOR,
        ),
        "new_program": (
            lambda: _new_program_target(page),
            NEW_PROGRAM_LOCATOR,
        ),
        "import_document": (
            lambda: page.get_by_role("button", name="Import document"),
            IMPORT_DOCUMENT_LOCATOR,
        ),
        "producer_role": (
            lambda: page.get_by_label("Producer"),
            PRODUCER_LOCATOR,
        ),
        "account_manager_role": (
            lambda: page.get_by_label("Account Manager"),
            ACCOUNT_MANAGER_LOCATOR,
        ),
        "commercial_customer": (
            lambda: page.get_by_role("radio", name="Commercial customer"),
            COMMERCIAL_LOCATOR,
        ),
        "insured_fields": (
            lambda: page.get_by_label("Name"),
            _label_text("Name"),
        ),
        "address_autocomplete": (
            lambda: page.get_by_role("option", name=exact_address, exact=True)
            if exact_address
            else page.get_by_label("Address line 1"),
            _locator_text("option", exact_address or "exact_address", exact=True)
            if exact_address
            else _label_text("Address line 1"),
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
    if spec_id == "wait_programs_ready":
        wait_programs_ready(page)
        return
    if spec_id == "stop_before_save":
        return
    if spec_id == "new_program":
        if locator_is_new_program_caret(NEW_PROGRAM_LOCATOR):
            raise UniqueLocatorError("refusing split-menu caret")
        wait_programs_ready(page)
        target.click()
        return
    if spec_id == "commercial_customer":
        checked = getattr(target, "is_checked", None)
        if callable(checked) and checked():
            return
        target.check() if hasattr(target, "check") else target.click()
        return
    if spec_id == "import_document":
        return
    if spec_id in {"producer_role", "account_manager_role"}:
        roles = roles_for_requested_by(requested_by_from_payload(payload))
        if roles.get("hitl_required") or not roles.get("resolved"):
            raise RuntimeError(unknown_sender_hitl(requested_by=roles.get("requested_by") or ""))
        current = ""
        inner = getattr(target, "input_value", None)
        if callable(inner):
            current = str(inner() or "")
        if should_overwrite_role(current, str(roles["resolved"])):
            fill = getattr(target, "fill", None)
            if callable(fill):
                fill(str(roles["resolved"]))
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
                    if spec_id == "wait_programs_ready":
                        wait_programs_ready(page)
                        steps.append(
                            pass_step(
                                spec_id,
                                locator=PROGRAMS_KPI_LOCATOR + "|" + PROGRAMS_TABLE_LOCATOR,
                            )
                        )
                        continue
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
