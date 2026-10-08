"""Close tabs a carrier worker opened, and leave the one it is still using.

Other carriers' pre-existing tabs are not closed. A tab created during this
run is closed when the run finishes, except the page the worker keeps.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def page_host(page: Any) -> str:
    return (urlsplit(page_url(page)).hostname or "").lower()


def context_of(page: Any) -> Any | None:
    context = getattr(page, "context", None)
    if callable(context):
        try:
            context = context()
        except Exception:
            return None
    return context


def context_pages(page: Any) -> list[Any]:
    context = context_of(page)
    if context is None:
        return []
    pages = getattr(context, "pages", None)
    if not isinstance(pages, list):
        return []
    return list(pages)


def snapshot_ids(page: Any) -> set[int]:
    return {id(item) for item in context_pages(page)}


def close_page(page: Any) -> bool:
    closer = getattr(page, "close", None)
    if not callable(closer):
        return False
    try:
        closer(run_before_unload=False)
        return True
    except TypeError:
        try:
            closer()
            return True
        except Exception:
            return False
    except Exception:
        return False


def close_new_pages(page: Any, before: set[int], *, keep: Any = None) -> int:
    """Close pages that were not open at ``before``, except ``keep``."""
    closed = 0
    for other in context_pages(page):
        if other is keep or other is page:
            continue
        if id(other) in before:
            continue
        if close_page(other):
            closed += 1
    return closed


def close_listed(pages: list[Any], *, keep: Any = None) -> int:
    closed = 0
    for page in pages:
        if page is keep:
            continue
        if close_page(page):
            closed += 1
    return closed
