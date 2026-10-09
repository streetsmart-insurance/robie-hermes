"""Phase 1 carrier adapters: read-only portal document pullers.

Each adapter module exposes ``ADAPTER`` (an :class:`AdapterSpec`) and a
``download`` function. Adapters run against an injected ``BrowserPort``
(production: Playwright; tests: a fake) — they never construct a browser
themselves, so CI never touches a live portal.

Read-only contract (enforced by ``phase1_doc_pull.run_pilot``):
every adapter's ``allowed_actions`` must be a subset of
``READ_ONLY_ACTIONS = {"portal_login", "portal_download", "api_read"}``.
Anything else (send, upload, call, write) is refused before it runs.

Runtime routing (from repo AGENTS.md reachability lessons):
- box    = hermes-poc-01, ROBIE's browser via residential proxy
           9.142.10.166:5822. Required where the sandbox egress is blocked.
- sandbox = this sandbox's browser. Required where the box is blocked
           (Hartford EBC, Selective agent portal — neither is in this pilot).
- api     = no browser at all (EZLynx PolicyApi from the box venv).

Anything marked UNVERIFIED below is DESIGNED, not TEST VERIFIED: the
portal URL, selector, and navigation steps come from the carrier's known
agent-portal layout and must be confirmed with a live read-only smoke
test before the pilot runs. Nothing here has performed a live login.
"""

from __future__ import annotations

from .base import AdapterSpec, BrowserPort, DownloadResult, PolicyRef

__all__ = ["AdapterSpec", "BrowserPort", "DownloadResult", "PolicyRef"]
