#!/usr/bin/env python3
"""
E01 Synchronous Diagnostic — bypasses the email queue entirely.

Triggers the exact policy-setup fill + Save & Continue Edit path and captures
unconditional evidence (screenshot + DOM) after every action.

Usage (on hermes-poc-01):
    python3 e01_diagnostic.py --mode=full        # Attach to existing CDP, drive E01
    python3 e01_diagnostic.py --mode=hitl-test   # Force a failure, verify HITL (Gemini + email + Chat)

Chrome must already be running. This script connects over CDP
(http://127.0.0.1:9222) and reuses a page/context. It never launches,
restarts, or kills Chrome.

Locked target (write allowlist):
    applicant 220250093 / policy TEST-HO-20260911-E01 / policy id 83669533
    The script refuses any other applicant. It does not mint a new E01.

Output: /tmp/e01-diagnostic/<timestamp>/ with:
    - step-NN-<action>.png (screenshot after every action)
    - step-NN-<action>.html (DOM dump after every action)
    - evidence.json (structured log of all steps)
    - hitl-test.json (HITL verification results, in hitl-test mode)

No bind, no money, no client email.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


PRODUCTION_ENGINE_ROOT = "/opt/streetsmart-hermes/releases/current"
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
FORMENTRY_WAIT_CAPTURE_MS = 5000
ALLOWED_APPLICANT_ID = "220250093"
ALLOWED_POLICY_ID = "83669533"
ALLOWED_POLICY_NUMBER = "TEST-HO-20260911-E01"

E01 = {
    "applicant_id": ALLOWED_APPLICANT_ID,
    "policy_number": ALLOWED_POLICY_NUMBER,
    "policy_id": ALLOWED_POLICY_ID,
    "writing_company": "10048",
    "master_company": 13585,
}


def _install_sys_path() -> None:
    """Prefer the live zip on poc-01. Do not use a nested release subfolder."""
    for root in (PRODUCTION_ENGINE_ROOT, _REPO_ROOT):
        if root and root not in sys.path:
            sys.path.insert(0, root)


_install_sys_path()


def require_e01_applicant(applicant_id: object) -> str:
    """Refuse every applicant except the locked E01 test account."""
    from robie_job_engine.ezlynx_write_scope import (
        normalize_applicant_id,
        require_allowed_ezlynx_write_applicant,
    )

    applicant = normalize_applicant_id(applicant_id)
    if applicant != ALLOWED_APPLICANT_ID:
        display = applicant or "<missing>"
        raise RuntimeError(
            f"REFUSED: e01_diagnostic may only target applicant "
            f"{ALLOWED_APPLICANT_ID}, got {display}"
        )
    return require_allowed_ezlynx_write_applicant(applicant)


def require_e01_policy_id(policy_id: object) -> str:
    pid = str(policy_id or "").strip()
    if pid != ALLOWED_POLICY_ID:
        raise RuntimeError(
            f"REFUSED: e01_diagnostic may only use existing policy "
            f"{ALLOWED_POLICY_ID} ({ALLOWED_POLICY_NUMBER}), got {pid or '<missing>'}"
        )
    return pid


def e01_edit_url(
    applicant_id: object = ALLOWED_APPLICANT_ID,
    policy_id: object = ALLOWED_POLICY_ID,
) -> str:
    applicant = require_e01_applicant(applicant_id)
    pid = require_e01_policy_id(policy_id)
    return (
        "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
        f"{applicant}/{pid}"
    )


def require_e01_url(url: object) -> str:
    """Refuse navigation to any EZLynx applicant except 220250093."""
    from robie_job_engine.ezlynx_write_scope import applicant_id_from_ezlynx_url

    raw = str(url or "").strip()
    extracted = applicant_id_from_ezlynx_url(raw)
    if extracted:
        require_e01_applicant(extracted)
    return raw


class EvidenceCollector:
    """Captures screenshot + DOM unconditionally after every action."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.steps: list[dict[str, Any]] = []
        self.counter = 0
        self.extra: dict[str, Any] = {}

    async def capture(self, page, action: str, detail: str = ""):
        """Capture screenshot + DOM. Never fails the run."""
        self.counter += 1
        prefix = f"step-{self.counter:02d}-{action}"
        step_info: dict[str, Any] = {
            "step": self.counter,
            "action": action,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": None,
            "title": None,
            "screenshot": None,
            "dom": None,
            "error": None,
        }
        try:
            step_info["url"] = page.url
            step_info["title"] = await page.title()
        except Exception as e:
            step_info["error"] = f"page info: {e}"

        try:
            shot_path = os.path.join(self.output_dir, f"{prefix}.png")
            await page.screenshot(path=shot_path)
            step_info["screenshot"] = shot_path
        except Exception as e:
            step_info["error"] = (step_info["error"] or "") + f" screenshot: {e}"

        try:
            dom_path = os.path.join(self.output_dir, f"{prefix}.html")
            html = await page.content()
            with open(dom_path, "w") as f:
                f.write(html)
            step_info["dom"] = dom_path
            step_info["dom_bytes"] = len(html)
        except Exception as e:
            step_info["error"] = (step_info["error"] or "") + f" dom: {e}"

        self.steps.append(step_info)
        print(f"  [{self.counter:02d}] {action}: {detail} | url={step_info['url']}", flush=True)
        return step_info

    def save(self):
        path = os.path.join(self.output_dir, "evidence.json")
        payload = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "code_version": self._code_version(),
            "applicant_id": ALLOWED_APPLICANT_ID,
            "policy_id": ALLOWED_POLICY_ID,
            "policy_number": ALLOWED_POLICY_NUMBER,
            "cdp_url": DEFAULT_CDP_URL,
            "steps": self.steps,
        }
        payload.update(self.extra)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nEvidence saved: {path}", flush=True)
        print(f"  {len(self.steps)} steps, screenshots + DOM in {self.output_dir}", flush=True)
        return path

    def _code_version(self):
        try:
            from robie_job_engine.ezlynx_policy_setup import CODE_VERSION
            return CODE_VERSION
        except Exception:
            return "unknown"


class _EvidenceLocator:
    """Locator proxy that captures after fill / select_option / click."""

    def __init__(self, locator: Any, owner: "EvidenceBoundPage", label: str = ""):
        self._locator = locator
        self._owner = owner
        self._label = label

    def __getattr__(self, name: str) -> Any:
        return getattr(self._locator, name)

    @property
    def first(self) -> "_EvidenceLocator":
        return _EvidenceLocator(self._locator.first, self._owner, self._label)

    def nth(self, index: int) -> "_EvidenceLocator":
        return _EvidenceLocator(self._locator.nth(index), self._owner, self._label)

    def locator(self, *args: Any, **kwargs: Any) -> "_EvidenceLocator":
        return _EvidenceLocator(
            self._locator.locator(*args, **kwargs),
            self._owner,
            self._label or (str(args[0]) if args else ""),
        )

    async def fill(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._locator.fill(*args, **kwargs)
        await self._owner._after("fill", self._label or f"fill {args[:1]}")
        return result

    async def select_option(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._locator.select_option(*args, **kwargs)
        await self._owner._after("fill", f"select {self._label or args or kwargs}")
        return result

    async def click(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._locator.click(*args, **kwargs)
        await self._owner._after("click", self._label or "click")
        return result


class EvidenceBoundPage:
    """Page proxy: capture after navigate / fill / click, and every ~5s after click."""

    def __init__(
        self,
        page: Any,
        ev: EvidenceCollector,
        wait_interval_ms: int = FORMENTRY_WAIT_CAPTURE_MS,
    ):
        self._page = page
        self._ev = ev
        self._wait_interval_ms = wait_interval_ms
        self._wait_accum_ms = 0
        self._after_click = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)

    @property
    def url(self) -> Any:
        return self._page.url

    @property
    def context(self) -> Any:
        return self._page.context

    @property
    def frames(self) -> Any:
        return self._page.frames

    async def goto(self, url: str, **kwargs: Any) -> Any:
        require_e01_url(url)
        result = await self._page.goto(url, **kwargs)
        await self._ev.capture(self._page, "navigate", url)
        return result

    async def wait_for_timeout(self, timeout: int) -> Any:
        result = await self._page.wait_for_timeout(timeout)
        if self._after_click:
            self._wait_accum_ms += int(timeout or 0)
            while self._wait_accum_ms >= self._wait_interval_ms:
                self._wait_accum_ms -= self._wait_interval_ms
                await self._ev.capture(
                    self._page,
                    "wait",
                    f"FormEntry poll {self._wait_interval_ms}ms",
                )
        return result

    def locator(self, *args: Any, **kwargs: Any) -> _EvidenceLocator:
        label = str(args[0]) if args else ""
        return _EvidenceLocator(self._page.locator(*args, **kwargs), self, label)

    def get_by_role(self, *args: Any, **kwargs: Any) -> _EvidenceLocator:
        label = kwargs.get("name") or (args[1] if len(args) > 1 else args[0] if args else "role")
        return _EvidenceLocator(self._page.get_by_role(*args, **kwargs), self, str(label))

    async def _after(self, action: str, detail: str) -> None:
        if action == "click":
            self._after_click = True
        await self._ev.capture(self._page, action, str(detail))


def select_existing_page(browser: Any) -> Any:
    """Reuse an already-open page. Never launch Chrome or open a foreign tab."""
    from robie_job_engine.ezlynx_write_scope import applicant_id_from_ezlynx_url

    contexts = list(getattr(browser, "contexts", None) or [])
    if not contexts:
        raise RuntimeError(
            "REFUSED: CDP has no browser context; will not launch Chrome"
        )
    pages = []
    for ctx in contexts:
        pages.extend(list(getattr(ctx, "pages", None) or []))
    if not pages:
        raise RuntimeError(
            "REFUSED: Persistent EZLynx browser has no page; will not launch Chrome"
        )

    reusable: list[Any] = []
    for page in pages:
        url = getattr(page, "url", "") or ""
        extracted = applicant_id_from_ezlynx_url(url)
        if extracted and extracted != ALLOWED_APPLICANT_ID:
            continue
        reusable.append(page)
    if not reusable:
        raise RuntimeError(
            "REFUSED: CDP pages are on other applicants; "
            "will not navigate a foreign tab or launch Chrome"
        )

    edit_needle = (
        f"/Policy/Actions/Edit/{ALLOWED_APPLICANT_ID}/{ALLOWED_POLICY_ID}"
    ).lower()
    for page in reusable:
        url = (getattr(page, "url", "") or "").lower()
        if edit_needle in url:
            return page
    for page in reusable:
        url = getattr(page, "url", "") or ""
        if applicant_id_from_ezlynx_url(url) == ALLOWED_APPLICANT_ID:
            return page
    return reusable[0]


async def attach_existing_cdp(
    playwright: Any = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    """Attach to the already-running Chrome. Never launch a browser."""
    own_playwright = False
    if playwright is None:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        own_playwright = True

    chromium = playwright.chromium
    connect = getattr(chromium, "connect_over_cdp", None)
    if connect is None:
        raise RuntimeError(
            "REFUSED: chromium has no connect_over_cdp; will not launch Chrome"
        )
    browser = await connect(cdp_url)
    page = select_existing_page(browser)
    return {
        "playwright": playwright,
        "browser": browser,
        "page": page,
        "own_playwright": own_playwright,
        "cdp_url": cdp_url,
    }


def _load_setup_page_cls() -> type:
    from robie_job_engine.ezlynx_policy_setup import EzlynxPolicySetupPage

    return EzlynxPolicySetupPage


async def run_full_diagnostic(
    *,
    playwright: Any = None,
    cdp_url: str = DEFAULT_CDP_URL,
    output_dir: str | None = None,
    setup_cls: type | None = None,
) -> dict[str, Any]:
    """Attach to the live CDP session and drive the existing E01 Edit path."""
    applicant = require_e01_applicant(E01["applicant_id"])
    policy_id = require_e01_policy_id(E01["policy_id"])
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = output_dir or f"/tmp/e01-diagnostic/{ts}"
    ev = EvidenceCollector(outdir)
    ev.extra["mode"] = "full"
    ev.extra["cdp_url"] = cdp_url
    print(f"E01 diagnostic starting. Output: {outdir}", flush=True)
    print(f"Code version: {ev._code_version()}", flush=True)
    print(
        f"Attach-only CDP {cdp_url}; applicant {applicant} policy {policy_id}",
        flush=True,
    )

    session: dict[str, Any] | None = None
    nav: dict[str, Any] = {}
    try:
        session = await attach_existing_cdp(playwright=playwright, cdp_url=cdp_url)
        bound = EvidenceBoundPage(session["page"], ev)
        cls = setup_cls or _load_setup_page_cls()
        setup = cls(bound, job_id=f"diagnostic-{ts}")
        setup.applicant_id = applicant
        print(f"Driving EzlynxPolicySetupPage._mint_formentry on {e01_edit_url()}", flush=True)
        nav = await setup._mint_formentry(policy_id, applicant)
        ev.extra["mint"] = nav
        await ev.capture(
            session["page"],
            "done",
            f"formentry_found={nav.get('formentry_found')} via={nav.get('via')}",
        )
        return nav
    except Exception as exc:
        ev.extra["error"] = f"{type(exc).__name__}: {exc}"
        print(f"DIAGNOSTIC ERROR: {type(exc).__name__}: {exc}", flush=True)
        if session and session.get("page") is not None:
            await ev.capture(session["page"], "error", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        ev.save()
        print("\nDIAGNOSTIC COMPLETE — review evidence in", outdir, flush=True)
        if session and session.get("own_playwright"):
            # Disconnect the Playwright client only. Do not close pages or Chrome.
            stop = getattr(session["playwright"], "stop", None)
            if stop is not None:
                await stop()


async def run_hitl_test():
    """Force a failure, verify HITL: Gemini consulted + email + Chat sent."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = f"/tmp/e01-diagnostic/hitl-test-{ts}"
    os.makedirs(outdir, exist_ok=True)
    print(f"HITL isolation test starting. Output: {outdir}", flush=True)

    results = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "gemini": {},
        "email": {},
        "chat": {},
    }

    from robie_job_engine.hitl_escalation import HitlRequest, escalate

    # Build a synthetic stuck-state request (forced failure)
    request = HitlRequest(
        job_id=f"hitl-test-{ts}",
        phase="hitl_isolation_test",
        error="FORCED TEST FAILURE: bad selector '.nonexistent-button-xyz' not found after 5 strategies",
        page_state={
            "url": e01_edit_url(),
            "title": "Edit Policy (test)",
            "buttons": ["Save & Continue Edit", "Cancel"],
        },
        attempted=["css_selector", "xpath", "text_content", "role_button", "all_elements"],
        applicant_id=ALLOWED_APPLICANT_ID,
        policy_id=ALLOWED_POLICY_ID,
        screenshot_path=None,
    )

    print("\n1. Calling escalate() (Gemini first, then Carlo)...", flush=True)
    try:
        response = escalate(request, {})
        results["escalate"] = {
            "source": response.source,
            "actionable": response.actionable,
            "suggestion": response.suggestion[:500] if response.suggestion else None,
        }
        print(f"   Source: {response.source}", flush=True)
        print(f"   Actionable: {response.actionable}", flush=True)
        print(f"   Suggestion: {(response.suggestion or '')[:200]}", flush=True)

        # Check if Gemini was actually consulted
        if response.source == "gemini":
            results["gemini"] = {"consulted": True, "actionable": response.actionable}
            print("   ✅ Gemini was consulted", flush=True)
        else:
            results["gemini"] = {"consulted": False, "reason": response.suggestion}
            print(f"   ⚠️  Gemini NOT consulted: {response.suggestion}", flush=True)

    except Exception as e:
        results["escalate_error"] = f"{type(e).__name__}: {e}"
        print(f"   ❌ escalate() raised: {e}", flush=True)

    # Do not treat Gemini answering as a sent notification or a continuing job.
    suggestion = results.get("escalate", {}).get("suggestion", "") or ""
    if "Could not send" in suggestion:
        print(f"\n2. Notification FAILED: {suggestion}", flush=True)
        results["notification_sent"] = False
    else:
        print(
            f"\n2. escalate() returned source={response.source} "
            f"actionable={response.actionable}. That is not proof email/chat "
            "were sent, and not proof a job is continuing.",
            flush=True,
        )
        results["notification_sent"] = "unproven"

    # Save results
    path = os.path.join(outdir, "hitl-test.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved: {path}", flush=True)

    print("\n" + "="*60, flush=True)
    print("HITL TEST SUMMARY:", flush=True)
    print(f"  Gemini consulted: {results['gemini'].get('consulted', 'unknown')}", flush=True)
    print(f"  Notification sent: {results.get('notification_sent')}", flush=True)
    print("  Carlo: check your email and Google Chat for the HITL message.", flush=True)
    print("="*60, flush=True)


def main():
    parser = argparse.ArgumentParser(description="E01 synchronous diagnostic")
    parser.add_argument(
        "--mode",
        choices=["full", "hitl-test"],
        required=True,
        help="full: attach to existing CDP and drive E01 | hitl-test: force failure, verify HITL",
    )
    args = parser.parse_args()

    if args.mode == "full":
        asyncio.run(run_full_diagnostic())
    else:
        asyncio.run(run_hitl_test())


if __name__ == "__main__":
    main()
