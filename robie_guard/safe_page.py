"""SafePage -- Playwright wrapper that enforces CR-1 at the browser layer.

Two independent stops, because one is never enough:

  1. In-page shim (browser_shim.js) installed as an init script. Catches clicks
     and DELETE-verb fetch/XHR even when a free-form agent is driving the page
     over CDP and never calls into this class.
  2. Playwright route interception. Catches the request at the transport layer,
     which is the last place it can be stopped before it reaches EZLynx.

Use safe_click() from Python call sites; it adds a third stop by classifying the
resolved element BEFORE clicking, with its DOM ancestry as evidence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .cardinal import CardinalViolation
from .destructive_guard import (
    ActionIntent,
    Decision,
    classify_intent,
    looks_destructive,
)

logger = logging.getLogger("robie.guard.browser")

SHIM_PATH = Path(__file__).with_name("browser_shim.js")

_ELEMENT_PROBE = """
(el) => {
  const attrs = {};
  for (const a of el.attributes || []) attrs[a.name] = a.value;
  const ancestry = [];
  let n = el, d = 0;
  while (n && d++ < 8) {
    if (n.nodeType === 1) {
      ancestry.unshift((n.tagName || '') + (n.id ? '#' + n.id : '') +
                       (n.className && n.className.toString ?
                        '.' + n.className.toString().split(/\\s+/).slice(0,3).join('.') : ''));
    }
    n = n.parentNode;
  }
  return {
    text: (el.innerText || el.textContent || '').trim().slice(0, 300),
    attrs: attrs,
    ancestry: ancestry
  };
}
"""


class SafePage:
    """Wraps a Playwright Page. Unknown attributes pass through to the page."""

    def __init__(self, page: Any):
        self._page = page
        self.blocked: list[dict[str, Any]] = []
        self.decisions: list[Decision] = []

    # -- construction ------------------------------------------------------
    @classmethod
    async def attach(cls, page: Any) -> "SafePage":
        self = cls(page)
        shim = SHIM_PATH.read_text()
        await page.add_init_script(shim)
        # The page may already be loaded; install into the live document too.
        try:
            await page.evaluate(shim)
        except Exception as exc:  # noqa: BLE001 - page may be mid-navigation
            logger.warning("shim live-injection skipped: %s", exc)
        await page.route("**/*", self._route_handler)
        logger.info("[ROBIE-GUARD] SafePage attached -- CR-1 active on %s", page.url)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)

    # -- transport-layer stop ---------------------------------------------
    async def _route_handler(self, route: Any, request: Any) -> None:
        intent = ActionIntent(
            kind="http",
            url=request.url,
            method=request.method,
            object_class=None,
        )
        if not looks_destructive(intent):
            await route.continue_()
            return

        decision = classify_intent(intent)
        self.decisions.append(decision)
        if decision.allowed:
            logger.warning("[ROBIE-GUARD] permitting destructive request: %s %s",
                           request.method, request.url)
            await route.continue_()
            return

        entry = {"method": request.method, "url": request.url,
                 "rule_id": decision.rule_id, "reason": decision.reason,
                 "at": decision.at}
        self.blocked.append(entry)
        logger.error("[ROBIE-GUARD] ABORTED %s %s :: %s",
                     request.method, request.url, decision.reason)
        await route.abort("blockedbyclient")

    # -- call-site stop ----------------------------------------------------
    async def safe_click(
        self,
        selector: str,
        *,
        intent: str | None = None,
        object_class: str | None = None,
        applicant_id: str | None = None,
        policy_number: str | None = None,
        unlock_token: str | None = None,
        transaction_type: str | None = None,
        transaction_status: str | None = None,
        duplicate_count: int | None = None,
        created_by: str | None = None,
        timeout: int = 15000,
    ) -> None:
        """Click only after classifying the element that the selector resolves to."""
        locator = self._page.locator(selector)
        count = await locator.count()
        if count == 0:
            raise CardinalViolation("CR-1", f"selector {selector!r} matched nothing", {})

        handle = await locator.first.element_handle(timeout=timeout)
        probe = await self._page.evaluate(_ELEMENT_PROBE, handle)

        action = ActionIntent(
            kind="click",
            intent=intent,
            object_class=object_class,
            target_text=probe.get("text"),
            target_attrs=probe.get("attrs") or {},
            dom_ancestry=probe.get("ancestry") or [],
            applicant_id=applicant_id,
            policy_number=policy_number,
            transaction_type=transaction_type,
            transaction_status=transaction_status,
            matched_row_count=count,
            duplicate_count=duplicate_count,
            created_by=created_by,
            unlock_token=unlock_token,
            scope_confirmed=bool(unlock_token) and count == 1
            and _looks_like_transaction_row(probe.get("ancestry") or [], probe.get("text") or ""),
        )

        decision = classify_intent(action)
        self.decisions.append(decision)
        if not decision.allowed:
            self.blocked.append({"selector": selector, "text": action.target_text,
                                 "rule_id": decision.rule_id, "reason": decision.reason,
                                 "at": decision.at})
        decision.raise_if_denied()
        await locator.first.click(timeout=timeout)

    # -- unlock plumbing for the one exception -----------------------------
    async def unlock_transaction_delete(self, token: str, seconds: int = 60) -> None:
        """Hand the in-page shim a short-lived unlock. Call inside
        guarded_transaction_delete() only."""
        await self._page.evaluate(
            """([tok, ms]) => {
                 if (!window.__ROBIE_GUARD) throw new Error('guard shim not installed');
                 window.__ROBIE_GUARD.unlock = tok;
                 window.__ROBIE_GUARD.unlockUntil = Date.now() + ms;
               }""",
            [token, seconds * 1000],
        )

    async def relock(self) -> None:
        await self._page.evaluate(
            """() => { if (window.__ROBIE_GUARD) {
                 window.__ROBIE_GUARD.unlock = null;
                 window.__ROBIE_GUARD.unlockUntil = 0; } }"""
        )

    async def page_blocks(self) -> list[dict[str, Any]]:
        """What the in-page shim stopped. Belongs in every job receipt."""
        try:
            return await self._page.evaluate(
                "() => (window.__ROBIE_GUARD && window.__ROBIE_GUARD.blocked) || []"
            )
        except Exception:  # noqa: BLE001
            return []

    async def assert_guard_installed(self) -> None:
        ok = await self._page.evaluate(
            "() => !!(window.__ROBIE_GUARD && window.__ROBIE_GUARD.installed)"
        )
        if not ok:
            raise CardinalViolation(
                "CR-1", "guard shim not present in page -- refusing to operate on EZLynx", {}
            )


def _looks_like_transaction_row(ancestry: list[str], text: str) -> bool:
    hay = (" ".join(ancestry) + " " + text).lower()
    row_like = any(tag in hay for tag in ("tr", "role=row", "transaction", "history"))
    return row_like and "rwl" in hay and "pending" in hay
