"""Live Ascend new-program walk for hermes-test-01 only.

GitHub CI must not import Playwright or talk to Ascend. This module lazy-imports
Playwright inside ``run_live_walk``. Unique locator required. Strict-mode
violation is FAIL. No Gemini. No .first / .nth / .last. Stop before Save
program, Send email, Copy checkout, payment, or bind.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from .ascend_create_combobox import (
    COMBOBOX_FIELDS,
    classify_listbox_options,
    intended_option_for_field,
    option_locator,
    unique_option_block_reason,
    unique_option_hitl,
)
from .ascend_create_defaults import (
    CREATE_PATH,
    CREATE_URL_RE,
    TEST_AGENCY_FEE,
    WAIT_FOR_URL,
    classify_document_labels,
    log_role_defaults,
    refuse_unexpected_agency_fee_default,
    require_create_form_seconds_logged,
    require_create_new_url,
    require_spinner_seconds_logged,
    should_set_test_agency_fee,
    spinner_seconds,
    test_agency_fee_set_value,
    unclear_document_label_hitl,
)
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
from .ascend_customer_type import (
    COMMERCIAL,
    COMMERCIAL_LOCATOR,
    resolve_customer_type,
    unknown_lob_hitl,
)
from .ascend_sender_roles import (
    ACCOUNT_MANAGER_LOCATOR,
    CREATE_FORM_READY_TIMEOUT_MS,
    IMPORT_DOCUMENT_ACCESSIBLE_NAME,
    IMPORT_DOCUMENT_LOCATOR,
    NEW_PROGRAM_ACCESSIBLE_NAME,
    NEW_PROGRAM_LOCATOR,
    PRODUCER_LOCATOR,
    PROGRAMS_KPI_LOCATOR,
    PROGRAMS_READY_TIMEOUT_MS,
    PROGRAMS_TABLE_LOCATOR,
    UPLOAD_DOCUMENT_ACCESSIBLE_NAME,
    create_form_timeout_error,
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
    return page.get_by_role("button", name=NEW_PROGRAM_ACCESSIBLE_NAME, exact=True)


def _import_document_target(page: Any) -> Any:
    return page.get_by_role(
        "button", name=IMPORT_DOCUMENT_ACCESSIBLE_NAME, exact=True
    )


def wait_programs_ready(
    page: Any, *, timeout_ms: int = PROGRAMS_READY_TIMEOUT_MS
) -> dict[str, Any]:
    """Wait out the spinner. Log seconds until the unique primary is ready."""
    started = time.monotonic()
    button = _new_program_target(page)
    try:
        button.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        seconds = spinner_seconds(started, time.monotonic())
        raise RuntimeError(
            programs_spinner_timeout_error(f"{exc}; logged {seconds}s")
        ) from exc
    if locator_is_new_program_caret(NEW_PROGRAM_LOCATOR):
        raise UniqueLocatorError("refusing split-menu caret; unique New program required")
    require_unique_locator(button, locator=NEW_PROGRAM_LOCATOR)
    enabled = getattr(button, "is_enabled", None)
    if callable(enabled) and not enabled():
        seconds = spinner_seconds(started, time.monotonic())
        raise RuntimeError(
            programs_spinner_timeout_error(
                f"primary New program is not enabled; logged {seconds}s"
            )
        )
    kpi = page.get_by_text("Programs at risk")
    table = page.get_by_role("table")
    kpi_count = int(kpi.count()) if callable(getattr(kpi, "count", None)) else 0
    table_count = int(table.count()) if callable(getattr(table, "count", None)) else 0
    if kpi_count < 1 and table_count < 1:
        seconds = spinner_seconds(started, time.monotonic())
        raise RuntimeError(
            programs_spinner_timeout_error(
                f"programs table or KPI cards are not present; logged {seconds}s"
            )
        )
    seconds = spinner_seconds(started, time.monotonic())
    leak = require_spinner_seconds_logged(seconds)
    if leak:
        raise RuntimeError(leak)
    return {
        "seconds": seconds,
        "locator": NEW_PROGRAM_LOCATOR,
        "primary_enabled": True,
        "kpi_or_table": True,
    }


def wait_create_new_url(
    page: Any, *, timeout_ms: int = PROGRAMS_READY_TIMEOUT_MS
) -> str:
    """After clicking primary New program, follow /create/new. Not follow-tab."""
    waiter = getattr(page, "wait_for_url", None)
    if callable(waiter):
        waiter(CREATE_URL_RE, timeout=timeout_ms)
    url = str(getattr(page, "url", "") or "")
    leak = require_create_new_url(url)
    if leak:
        raise RuntimeError(leak)
    return url


def wait_create_form_ready(
    page: Any, *, timeout_ms: int = CREATE_FORM_READY_TIMEOUT_MS
) -> dict[str, Any]:
    """After /create/new, wait until unique Import document is visible. Log seconds."""
    started = time.monotonic()
    button = _import_document_target(page)
    try:
        button.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        seconds = spinner_seconds(started, time.monotonic())
        raise RuntimeError(
            create_form_timeout_error(f"{exc}; logged {seconds}s")
        ) from exc
    require_unique_locator(button, locator=IMPORT_DOCUMENT_LOCATOR)
    enabled = getattr(button, "is_enabled", None)
    if callable(enabled) and not enabled():
        seconds = spinner_seconds(started, time.monotonic())
        raise RuntimeError(
            create_form_timeout_error(
                f"Import document is not enabled; logged {seconds}s"
            )
        )
    seconds = spinner_seconds(started, time.monotonic())
    leak = require_create_form_seconds_logged(seconds)
    if leak:
        raise RuntimeError(leak)
    return {
        "seconds": seconds,
        "locator": IMPORT_DOCUMENT_LOCATOR,
        "primary": IMPORT_DOCUMENT_ACCESSIBLE_NAME,
        "primary_enabled": True,
    }


def _field_value(target: Any) -> str:
    for name in ("input_value", "inner_text", "text_content"):
        reader = getattr(target, name, None)
        if callable(reader):
            try:
                return str(reader() or "")
            except Exception:  # noqa: BLE001 — try the next reader
                continue
    get_attr = getattr(target, "get_attribute", None)
    if callable(get_attr):
        try:
            return str(get_attr("value") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _count(target: Any) -> int:
    counter = getattr(target, "count", None)
    if callable(counter):
        try:
            return int(counter())
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _visible_option_names(page: Any) -> list[str]:
    """Read every open listbox option. Do not use .first/.nth/.last."""
    options = page.get_by_role("option")
    reader = getattr(options, "all_inner_texts", None)
    if callable(reader):
        try:
            return [str(item or "").strip() for item in reader() if str(item or "").strip()]
        except Exception:  # noqa: BLE001
            pass
    evaluate = getattr(page, "eval_on_selector_all", None)
    if callable(evaluate):
        try:
            found = evaluate(
                '[role="option"]',
                "els => els.map(e => (e.innerText || e.textContent || '').trim())",
            )
            return [str(item or "").strip() for item in found or [] if str(item or "").strip()]
        except Exception:  # noqa: BLE001
            return []
    return []


def _close_open_listbox(page: Any) -> None:
    keyboard = getattr(page, "keyboard", None)
    press = getattr(keyboard, "press", None) if keyboard is not None else None
    if callable(press):
        try:
            press("Escape")
        except Exception:  # noqa: BLE001
            return


def _find_labeled_control(page: Any, aliases: tuple[str, ...]) -> tuple[Any | None, str]:
    for label in aliases:
        target = page.get_by_label(label)
        if _count(target) == 1:
            return target, _label_text(label)
    return None, _label_text(aliases[0] if aliases else "")


def audit_live_comboboxes(page: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Open each create-form combobox. Unique intended option or FAIL/HITL."""
    from .ascend_sender_roles import requested_by_from_payload, roles_for_requested_by

    roles = roles_for_requested_by(requested_by_from_payload(payload))
    blob = dict(payload)
    if roles.get("option"):
        blob["producer"] = roles["option"]
        blob["account_manager"] = roles["option"]
    elif roles.get("resolved"):
        blob.setdefault("producer", roles["resolved"])
        blob.setdefault("account_manager", roles["resolved"])
    reports: list[dict[str, Any]] = []
    blocked: list[str] = []
    for field in COMBOBOX_FIELDS:
        intended = intended_option_for_field(field, blob)
        target, locator = _find_labeled_control(page, tuple(field["aliases"]))
        if target is None:
            report = classify_listbox_options(
                field=str(field["label"]),
                intended=intended,
                options=[],
                exact=True,
                field_present=False,
            )
            reports.append(report)
            continue
        require_unique_locator(target, locator=locator)
        click = getattr(target, "click", None)
        if callable(click):
            click()
        names = _visible_option_names(page)
        report = classify_listbox_options(
            field=str(field["label"]),
            intended=intended,
            options=names,
            exact=True,
            field_present=True,
        )
        chosen = str(report.get("intended") or intended)
        report["locator"] = option_locator(chosen, exact=True) if chosen else locator
        reports.append(report)
        if report.get("blocked_field"):
            blocked.append(str(report["blocked_field"]))
        _close_open_listbox(page)
    return {
        "ok": not blocked,
        "blocked_fields": blocked,
        "fields": reports,
        "logged": True,
    }


def _visible_text(page: Any) -> str:
    inner = getattr(page, "inner_text", None)
    if callable(inner):
        try:
            return str(inner("body") or "")
        except Exception:  # noqa: BLE001
            pass
    content = getattr(page, "content", None)
    if callable(content):
        try:
            return str(content() or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


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
            lambda: _import_document_target(page),
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
        "customer_type": (
            lambda: page.get_by_role(
                "radio",
                name=(
                    "Commercial customer"
                    if resolve_customer_type(payload).get("resolved") == COMMERCIAL
                    else "Personal customer"
                ),
            ),
            resolve_customer_type(payload).get("locator") or COMMERCIAL_LOCATOR,
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


def _select_unique_option(page: Any, target: Any, intended: str, *, field: str) -> dict[str, Any]:
    """Click the combobox, then the unique exact option. Log the field on block."""
    click = getattr(target, "click", None)
    if callable(click):
        click()
    names = _visible_option_names(page)
    report = classify_listbox_options(
        field=field,
        intended=intended,
        options=names,
        exact=True,
        field_present=True,
    )
    if report.get("blocked_field"):
        if report.get("hitl_required"):
            raise RuntimeError(report.get("hitl_text") or unique_option_hitl(
                field=field, intended=intended, match_count=0
            ))
        raise UniqueLocatorError(
            report.get("error")
            or unique_option_block_reason(
                field=field,
                intended=intended,
                match_count=int(report.get("match_count") or 0),
            )
        )
    intended = str(report.get("intended") or intended)
    option = page.get_by_role("option", name=intended, exact=True)
    require_unique_locator(option, locator=option_locator(intended, exact=True))
    option_click = getattr(option, "click", None)
    if callable(option_click):
        option_click()
    return report


def _act(page: Any, spec_id: str, target: Any, payload: dict[str, Any]) -> dict[str, Any]:
    require_unique_locator(target)
    if spec_id == "wait_programs_ready":
        return wait_programs_ready(page)
    if spec_id == "stop_before_save":
        return {"stop_before": ["Save program", "Send email", "Copy checkout", "payment", "bind"]}
    if spec_id == "new_program":
        if locator_is_new_program_caret(NEW_PROGRAM_LOCATOR):
            raise UniqueLocatorError("refusing split-menu caret")
        wait_programs_ready(page)
        target.click()
        url = wait_create_new_url(page)
        return {"url": url, "wait_for_url": WAIT_FOR_URL, "path": CREATE_PATH}
    if spec_id == "customer_type":
        decision = resolve_customer_type(payload)
        if decision.get("hitl_required") or not decision.get("resolved"):
            raise RuntimeError(unknown_lob_hitl(lob=str(decision.get("lob") or "")))
        require_unique_locator(target, locator=str(decision.get("locator") or ""))
        checked = getattr(target, "is_checked", None)
        if callable(checked) and checked():
            return {"radio": decision.get("radio")}
        target.check() if hasattr(target, "check") else target.click()
        return {"radio": decision.get("radio")}
    if spec_id == "import_document":
        labels = classify_document_labels(_visible_text(page))
        import_count = _count(_import_document_target(page))
        upload_count = _count(
            page.get_by_role("button", name=UPLOAD_DOCUMENT_ACCESSIBLE_NAME, exact=True)
        )
        labels["import_document"] = labels["import_document"] or import_count >= 1
        labels["upload_document"] = labels["upload_document"] or upload_count >= 1
        labels["labels"] = [
            name
            for name, present in (
                ("Import document", labels["import_document"]),
                ("Upload document", labels["upload_document"]),
                ("dropzone", labels["dropzone"]),
            )
            if present
        ]
        if not labels["import_document"] and not labels["upload_document"] and not labels["dropzone"]:
            raise RuntimeError(unclear_document_label_hitl(labels=labels["labels"]))
        return labels
    if spec_id in {"producer_role", "account_manager_role"}:
        roles = roles_for_requested_by(requested_by_from_payload(payload))
        current = _field_value(target)
        logged = log_role_defaults(
            requested_by=str(roles.get("requested_by") or ""),
            producer=current if spec_id == "producer_role" else current,
            account_manager=current if spec_id == "account_manager_role" else current,
        )
        if roles.get("hitl_required") or not roles.get("resolved"):
            raise RuntimeError(unknown_sender_hitl(requested_by=roles.get("requested_by") or ""))
        target_option = str(roles.get("option") or roles.get("resolved") or "")
        if should_overwrite_role(current, target_option):
            field = "Producer" if spec_id == "producer_role" else "Account Manager"
            _select_unique_option(page, target, target_option, field=field)
        after = _field_value(target)
        leak = log_role_defaults(
            requested_by=str(roles.get("requested_by") or ""),
            producer=after if spec_id == "producer_role" else str(roles.get("producer") or after),
            account_manager=(
                after if spec_id == "account_manager_role" else str(roles.get("account_manager") or after)
            ),
        ).get("error")
        if leak:
            raise RuntimeError(leak)
        return {
            "producer_default" if spec_id == "producer_role" else "account_manager_default": current,
            "set_value": target_option,
            "requested_by": roles.get("requested_by"),
            "prefill_logged": logged.get("logged"),
        }
    if spec_id == "address_autocomplete":
        if str(payload.get("exact_address") or ""):
            target.click()
        return {}
    if spec_id == "agency_fee":
        count = _count(target)
        if count > 1:
            raise UniqueLocatorError(
                f"strict mode violation: Agency Fee locator resolved to {count} elements"
            )
        present = count == 1
        default = _field_value(target) if present else None
        leak = refuse_unexpected_agency_fee_default(default, field_present=present)
        if leak:
            raise RuntimeError(leak)
        set_value = None
        if should_set_test_agency_fee(field_present=present):
            fill = getattr(target, "fill", None)
            set_value = str(payload.get("test_agency_fee") or test_agency_fee_set_value())
            if callable(fill):
                fill(set_value)
        return {
            "default": default,
            "set_value": set_value,
            "field_present": present,
        }
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
        "agency_fee": str(payload.get("test_agency_fee") or TEST_AGENCY_FEE),
    }.get(spec_id)
    if spec_id in {"carrier", "wholesaler", "coverage_type"} and value:
        field = {
            "carrier": "Carrier",
            "wholesaler": "Wholesaler",
            "coverage_type": "Coverage type",
        }[spec_id]
        report = _select_unique_option(page, target, value, field=field)
        return {"set_value": value, "listbox": report}
    if callable(fill) and value is not None:
        fill(value)
    return {"set_value": value} if value is not None else {}


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
                        observed = wait_programs_ready(page)
                        steps.append(
                            pass_step(
                                spec_id,
                                locator=PROGRAMS_KPI_LOCATOR + "|" + PROGRAMS_TABLE_LOCATOR,
                                description=f"programs spinner {observed.get('seconds')}s",
                                observed=observed,
                            )
                        )
                        continue
                    if spec_id == "import_document":
                        observed = wait_create_form_ready(page)
                        labels = _act(page, spec_id, _import_document_target(page), payload)
                        observed = {**observed, **(labels or {})}
                        steps.append(
                            pass_step(
                                spec_id,
                                locator=IMPORT_DOCUMENT_LOCATOR,
                                description=(
                                    f"create form ready {observed.get('seconds')}s; "
                                    f"labels {observed.get('labels')}"
                                ),
                                observed=observed,
                            )
                        )
                        continue
                    if spec_id == "unique_listbox_options":
                        observed = audit_live_comboboxes(page, payload)
                        locator = spec.get("locator") or option_locator(
                            "intended", exact=True
                        )
                        if not observed.get("ok"):
                            blocked = (
                                ", ".join(observed.get("blocked_fields") or [])
                                or "unknown"
                            )
                            first = next(
                                (
                                    item
                                    for item in (observed.get("fields") or [])
                                    if item.get("blocked_field")
                                ),
                                {},
                            )
                            error = (
                                first.get("error")
                                or first.get("hitl_text")
                                or unique_option_block_reason(
                                    field=blocked,
                                    intended=str(first.get("intended") or ""),
                                    match_count=int(first.get("match_count") or 0),
                                )
                            )
                            steps.append(
                                fail_step(
                                    spec_id,
                                    error,
                                    locator=locator,
                                    observed=observed,
                                )
                            )
                            return steps
                        steps.append(
                            pass_step(
                                spec_id,
                                locator=locator,
                                description=(
                                    f"create/new comboboxes unique "
                                    f"({len(observed.get('fields') or [])})"
                                ),
                                observed=observed,
                            )
                        )
                        continue
                    target, locator = _resolve(page, spec_id, payload)
                    if spec_id != "agency_fee":
                        require_unique_locator(target, locator=locator)
                    if spec_id == "stop_before_save":
                        steps.append(
                            pass_step(
                                spec_id,
                                locator=locator,
                                observed={
                                    "stop_before": [
                                        "Save program",
                                        "Send email",
                                        "Copy checkout",
                                        "payment",
                                        "bind",
                                    ]
                                },
                            )
                        )
                        continue
                    observed = _act(page, spec_id, target, payload)
                    steps.append(
                        pass_step(spec_id, locator=locator, observed=observed)
                    )
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
