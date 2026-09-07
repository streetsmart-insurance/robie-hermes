"""Read-only Magellan collection for the daily accountability report.

The collector uses a dedicated persistent Chrome profile and Secret Manager
references.  It never collects transcripts or changes handled status.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page


DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DEFAULT_PROJECT = "streetsmart-hermes-poc"
DASHBOARD_URL = "https://app.magellan.insure/dashboard"
LOGIN_URL = "https://app.magellan.insure/login"
ROW_SELECTOR = 'tr[data-testid^="call-success-row-"]'


def _secret(name: str, project: str = DEFAULT_PROJECT) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}/secrets/{name}"
    versions = list(client.list_secret_versions(request={"parent": parent, "filter": "state:ENABLED"}))
    if not versions:
        raise RuntimeError(f"No enabled version exists for required secret {name}")
    newest = max(versions, key=lambda item: item.create_time)
    response = client.access_secret_version(request={"name": newest.name})
    return response.payload.data.decode("utf-8").strip()


def _page(browser: "Browser") -> "Page":
    pages = [page for context in browser.contexts for page in context.pages]
    if pages:
        return pages[-1]
    if browser.contexts:
        return browser.contexts[0].new_page()
    raise RuntimeError("INCONCLUSIVE: persistent Magellan browser has no context")


def _authenticated(page: "Page") -> bool:
    return page.url.startswith("https://app.magellan.insure/") and page.locator(
        'a[href="/dashboard"], a[href$="/dashboard"]'
    ).count() == 1 and page.get_by_text(re.compile(r"Welcome,\s+.+!", re.I)).count() == 1


def _authenticate(page: "Page", *, project: str) -> None:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1_000)
    if _authenticated(page):
        return
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.get_by_label("Your email", exact=True).fill(_secret("magellan-username", project))
    page.get_by_label("Password", exact=True).fill(_secret("magellan-password", project))
    page.get_by_role("button", name="Login", exact=True).click()
    page.wait_for_url(re.compile(r"/dashboard(?:$|[/?#])"), timeout=20_000)
    page.wait_for_timeout(1_000)
    if not _authenticated(page):
        raise RuntimeError("Magellan authentication did not reach the verified dashboard")


def _seconds(value: str) -> int:
    parts = [int(part) for part in value.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    raise ValueError(f"unsupported Magellan duration: {value!r}")


def _row_value(row: Any, token: str) -> str:
    locator = row.locator(f'td[data-testid*="-{token}-"]')
    if locator.count() != 1:
        raise RuntimeError(f"Magellan row requires one {token} cell")
    return " ".join(locator.inner_text().split())


def parse_visible_rows(page: "Page", target_date: date) -> tuple[list[dict[str, Any]], bool]:
    """Return target-day rows and whether the page contains an older boundary."""
    records: list[dict[str, Any]] = []
    older_seen = False
    for row in page.locator(ROW_SELECTOR).all():
        occurred = datetime.strptime(_row_value(row, "date-time"), "%m/%d/%Y %I:%M %p")
        if occurred.date() < target_date:
            older_seen = True
            continue
        if occurred.date() != target_date:
            continue
        row_id = str(row.get_attribute("data-testid") or "").removeprefix("call-success-row-")
        from_link = row.locator('td[data-testid*="-call-from-"] a[href^="tel:"]')
        to_link = row.locator('td[data-testid*="-call-to-"] a[href^="tel:"]')
        if from_link.count() != 1 or to_link.count() != 1:
            raise RuntimeError("Magellan call row requires one From and one To telephone link")
        sentiment_icon = row.locator('td[data-testid*="-sentiment-"] [aria-label]')
        sentiment_label = str(sentiment_icon.get_attribute("aria-label") or "unknown") if sentiment_icon.count() == 1 else "unknown"
        tags_cell = row.locator('td[data-testid*="-summary-items-"]')
        tags = [value.strip() for value in tags_cell.locator("span.ant-typography").all_inner_texts() if value.strip()]
        records.append(
            {
                "call_id": row_id,
                "occurred_at": occurred.isoformat(),
                "from_number": str(from_link.get_attribute("href") or "").removeprefix("tel:"),
                "to_number": str(to_link.get_attribute("href") or "").removeprefix("tel:"),
                "duration_seconds": _seconds(_row_value(row, "duration")),
                "sentiment": "Sad" if sentiment_label.casefold() == "frown" else sentiment_label.title(),
                "tags": list(dict.fromkeys(tags)),
                "is_handled": row.get_by_role("button", name="Mark as Handled", exact=True).count() == 0,
            }
        )
    return records, older_seen


def _next_enabled(page: "Page") -> bool:
    next_button = page.locator("li.ant-pagination-next button, li.ant-pagination-next a")
    return next_button.count() == 1 and next_button.is_enabled()


def _ensure_sad_filter(page: "Page") -> None:
    """Select and verify Magellan's Sad sentiment scope deterministically."""
    sad = page.get_by_role("radio", name=re.compile(r"\bSad\b", re.I))
    if sad.count() != 1:
        raise RuntimeError("Magellan dashboard requires one identifiable Sad sentiment filter")
    if not sad.is_checked():
        sad.check()
        page.wait_for_timeout(750)
    if not sad.is_checked():
        raise RuntimeError("Magellan Sad sentiment filter could not be verified")


def collect_magellan_snapshot(
    *,
    target_date: date,
    output_path: Path,
    cdp_url: str = DEFAULT_CDP_URL,
    project: str = DEFAULT_PROJECT,
    max_pages: int = 40,
) -> Path:
    from playwright.sync_api import sync_playwright

    output_path = output_path.expanduser().resolve()
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        page = _page(browser)
        page.set_default_timeout(20_000)
        _authenticate(page, project=project)
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        _ensure_sad_filter(page)
        page.wait_for_selector(ROW_SELECTOR)
        collected: list[dict[str, Any]] = []
        older_boundary = False
        pages_read = 0
        seen_ids: set[str] = set()
        while pages_read < max_pages:
            pages_read += 1
            page_records, older_boundary = parse_visible_rows(page, target_date)
            for record in page_records:
                if record["call_id"] not in seen_ids:
                    seen_ids.add(record["call_id"])
                    collected.append(record)
            if older_boundary or not _next_enabled(page):
                break
            first_id = page.locator(ROW_SELECTOR).first.get_attribute("data-testid")
            page.locator("li.ant-pagination-next button, li.ant-pagination-next a").click()
            page.wait_for_function(
                "([selector, prior]) => { const row = document.querySelector(selector); "
                "return row !== null && row.getAttribute('data-testid') !== prior; }",
                arg=[ROW_SELECTOR, first_id],
            )
        if not older_boundary and _next_enabled(page):
            raise RuntimeError("Magellan pagination limit reached before the target-date boundary")
    snapshot = {
        "source_status": "available",
        "source": "Magellan authenticated dashboard",
        "target_date": target_date.isoformat(),
        "records_reviewed": len(collected),
        "at_risk_calls": len(collected),
        "records": collected,
        "sad_calls": [
            {
                **item,
                "caller_phone_masked": item["from_number"],
                "callback_status": "UNVERIFIED until RingCentral reconciliation",
            }
            for item in collected
        ],
        "transcripts_collected": False,
        "pages_read": pages_read,
        "older_boundary_verified": older_boundary,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    temporary.replace(output_path)
    return output_path
