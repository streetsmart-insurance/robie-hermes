"""Read-only client lookup by name.

Typing into EZLynx global search is not a write. One matching client is
bound onto the job. More than one match asks a plain question. The write
allowlist is unchanged for every other control.
"""

from __future__ import annotations

import re
from typing import Any

_LOOKUP = re.compile(
    r"\b(?:policy\s+numbers?|carriers?)\b",
    re.IGNORECASE,
)
_WRITE = re.compile(
    r"\b(?:change|update|add|delete|remove|set|file|bind|cancel|endorse)\b",
    re.IGNORECASE,
)
_NAME = re.compile(
    r"\bfor\s+([A-Za-z][A-Za-z' .-]{1,80}?)\s*[.?!]?$",
    re.IGNORECASE,
)


_SEARCH_BOX = re.compile(
    r"applicantsearch"
    r"|(?:^|[#.\[\]\s\"'(=])search(?:$|[^a-z0-9])"
    r"|searchbox",
    re.IGNORECASE,
)


def is_readonly_client_search(method_name: str, selector: object) -> bool:
    """True for an applicant search box on any page, not a form field.

    The dashboard is not a client account page. Typing a name into its
    search box is still a read.
    """
    method = str(method_name or "").strip().casefold()
    if method not in {"fill", "type", "press_sequentially", "click"}:
        return False
    text = " ".join(str(selector or "").split())
    if not text:
        return False
    compact = text.casefold().replace(" ", "")
    if "applicantsearch" in compact:
        return True
    return _SEARCH_BOX.search(text) is not None


def job_is_named_lookup(job: dict[str, Any] | None) -> bool:
    """True when the original ask is a policy-fact lookup by client name."""
    payload = dict((job or {}).get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        if client_name_from_lookup(str(payload.get(key) or "")):
            return True
    return False


def client_name_from_lookup(text: str) -> str | None:
    """The client named in a policy-fact question, or None when this is a write."""
    raw = " ".join(str(text or "").split())
    if not raw or _WRITE.search(raw) or not _LOOKUP.search(raw):
        return None
    match = _NAME.search(raw)
    if not match:
        return None
    name = " ".join(match.group(1).split()).strip(" .?")
    return name or None


def bind_named_client(
    store: Any,
    job_id: str,
    name: str,
    matches: list[dict[str, Any]],
) -> str:
    """Bind one client and answer. Several matches ask which one."""
    who = " ".join(str(name or "").split())
    titled = who.title() if who else "That client"
    ids: list[str] = []
    for row in matches:
        applicant = str((row or {}).get("applicant_id") or "").strip()
        if applicant and applicant not in ids:
            ids.append(applicant)
    if len(ids) > 1:
        return f"I found more than one {titled}. Which one should I use?"
    if len(ids) != 1:
        return f"I couldn't find a client named {titled}."
    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    payload["applicant_id"] = ids[0]
    if who:
        payload["client_name"] = who
    store.update_payload(job_id, payload)
    chosen = next(
        row for row in matches if str(row.get("applicant_id") or "").strip() == ids[0]
    )
    number = str(chosen.get("policy_number") or "").strip()
    carrier = str(chosen.get("carrier") or "").strip()
    line = str(chosen.get("line") or "GL").strip() or "GL"
    if number and carrier:
        return f"{titled}'s {line} policy number is {number} with {carrier}."
    return f"I found {titled}."
