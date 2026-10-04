"""Fail-closed Gemini helper for a blocked unique-write on any Playwright site.

Vertex/Gemini is already used in this repo as a Hermes fallback provider and
for model-attempt / thought-signature audit. There is no existing job-callable
stuck-field client. This module is the smallest safe hook:

1. A blocked unique-write or unnamed modal on any site Robie drives stops.
2. Only the page host/title and visible labels are sent (secrets redacted).
3. Gemini may return exactly one unique field, or the helper HITLs.
4. Positional ``.first`` / ``.nth()`` / ``.last`` answers are refused.

The unique-write guard stays in force. This helper never writes a field and
never authorizes bind or payment.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from .gemini_video import stamp_video_processing
from .playwright_write_guard import locator_is_positional_guess
from .secrets import redact_text


PLAYWRIGHT_BLOCKED = "PLAYWRIGHT_BLOCKED"
HITL_OPERATOR = "Carlo"
DEFAULT_VERTEX_LOCATION = "us-central1"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
_POSITIONAL_MARKERS = (".first", ".nth", ".last", "nth=", " >> nth")
_SECRET_LABEL = re.compile(
    r"\b(password|passwd|pwd|mfa|otp|totp|one[- ]time|secret|token|ssn|fein)\b",
    re.IGNORECASE,
)


class GeminiFieldClient(Protocol):
    """Minimal generate-content port. Tests inject a fake."""

    def generate_unique_field(self, prompt: str) -> str: ...

    def generate_content(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class UniqueFieldDecision:
    action: str
    field_label: str | None
    locator: str | None
    reason: str
    gemini_asked: bool
    hitl_operator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "field_label": self.field_label,
            "locator": self.locator,
            "reason": self.reason,
            "gemini_asked": self.gemini_asked,
            "hitl_operator": self.hitl_operator,
        }


def _hitl(reason: str, *, gemini_asked: bool) -> UniqueFieldDecision:
    return UniqueFieldDecision(
        action="HITL",
        field_label=None,
        locator=None,
        reason=f"{PLAYWRIGHT_BLOCKED}: {reason}",
        gemini_asked=gemini_asked,
        hitl_operator=HITL_OPERATOR,
    )


def _safe_label(value: str) -> str | None:
    text = redact_text(str(value or "")).strip()
    if not text or _SECRET_LABEL.search(text):
        return None
    if text == "[REDACTED]":
        return None
    return text[:160]


def _safe_page_host(page_url: str = "") -> str:
    """Host only. Drop userinfo, path, and query so tokens never leave the page."""
    text = redact_text(str(page_url or "")).strip()
    if not text or text == "[REDACTED]":
        return ""
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").strip()
    return _safe_label(host) or ""


def _current_page_name(*, dialog_title: str, page_url: str = "") -> str:
    host = _safe_page_host(page_url)
    title = str(dialog_title or "").strip()
    if host and title:
        return f"{host} ({title})"
    return host or title or "the current page"


def describe_blocked_dialog(
    *,
    dialog_title: str,
    visible_labels: Iterable[str],
    block_reason: str = "",
) -> dict[str, Any]:
    """Return the only payload Gemini may see for a stuck field."""
    labels = []
    seen: set[str] = set()
    for raw in visible_labels:
        label = _safe_label(raw)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    title = _safe_label(dialog_title) or ""
    return {
        "dialog_title": title,
        "visible_labels": labels,
        "block_reason": redact_text(str(block_reason or ""))[:1_000],
    }


def _locator_is_forbidden(locator: str) -> bool:
    text = str(locator or "")
    if not text.strip():
        return True
    folded = text.casefold()
    if any(marker in folded for marker in _POSITIONAL_MARKERS):
        return True
    return locator_is_positional_guess(text)


def _label_is_visible(field_label: str, visible_labels: Iterable[str]) -> bool:
    wanted = str(field_label or "").strip().casefold()
    if not wanted:
        return False
    return any(str(label).strip().casefold() == wanted for label in visible_labels)


def _parse_gemini_payload(raw: str) -> dict[str, Any] | None:
    text = redact_text(str(raw or "")).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def build_gemini_unique_field_prompt(
    *,
    dialog_title: str,
    visible_labels: Iterable[str],
    block_reason: str = "",
    page_url: str = "",
) -> str:
    description = describe_blocked_dialog(
        dialog_title=dialog_title,
        visible_labels=visible_labels,
        block_reason=block_reason,
    )
    page_name = _current_page_name(
        dialog_title=description["dialog_title"],
        page_url=page_url,
    )
    return (
        f"A Playwright write on {page_name} was PLAYWRIGHT_BLOCKED because "
        "the field could not be uniquely named. Unique-write stays fail-closed. "
        "Do not guess. Do not use .first, .nth(), or .last.\n\n"
        f"Dialog title: {description['dialog_title'] or '(none)'}\n"
        "Visible labels:\n"
        + ("\n".join(f"- {label}" for label in description["visible_labels"]) or "- (none)")
        + "\n"
        f"Block reason: {description['block_reason']}\n\n"
        "Reply with JSON only, no markdown:\n"
        '{"decision":"unique","field_label":"<exact visible label>",'
        '"locator":"<role/label locator for that one field>"}\n'
        "or\n"
        '{"decision":"unsure","reason":"<why no unique field>"}\n'
        "field_label must be copied exactly from Visible labels. "
        "If zero or more than one field could match, decision must be unsure."
    )


class VertexGeminiFieldClient:
    """Smallest Vertex generateContent client. Fail closed on any error."""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        model: str | None = None,
        opener: Callable[[urllib.request.Request, float | None], Any] | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.project = (
            project.strip()
            if project is not None
            else (
                os.environ.get("ROBIE_GEMINI_PROJECT", "").strip()
                or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
            )
        )
        self.location = (
            location.strip()
            if location is not None
            else (
                os.environ.get("ROBIE_GEMINI_LOCATION", "").strip()
                or DEFAULT_VERTEX_LOCATION
            )
        )
        model_name = (
            model.strip()
            if model is not None
            else (
                os.environ.get("ROBIE_GEMINI_MODEL", "").strip()
                or DEFAULT_GEMINI_MODEL
            )
        )
        self.model = model_name.rsplit("/", 1)[-1]
        self._opener = opener
        self._token_provider = token_provider

    def configured(self) -> bool:
        return bool(self.project and self.location and self.model)

    def _access_token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        import google.auth
        from google.auth.transport.requests import Request

        credentials, project = google.auth.default(
            scopes=("https://www.googleapis.com/auth/cloud-platform",)
        )
        if not self.project and project:
            self.project = str(project)
        credentials.refresh(Request())
        token = getattr(credentials, "token", "") or ""
        if not token:
            raise RuntimeError("Vertex credentials did not yield an access token")
        return str(token)

    def generate_unique_field(self, prompt: str) -> str:
        if not self.configured():
            raise RuntimeError("Vertex Gemini project is not configured")
        token = self._access_token()
        url = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/"
            f"projects/{self.project}/locations/{self.location}/"
            f"publishers/google/models/{self.model}:generateContent"
        )
        body = json.dumps(
            stamp_video_processing(
                {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "maxOutputTokens": 256,
                        "responseMimeType": "application/json",
                    },
                }
            )
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vertex generateContent failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(
                f"Vertex generateContent failed: {type(exc).__name__}"
            ) from exc
        payload = json.loads(raw.decode("utf-8"))
        parts = (
            (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
            or []
        )
        texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
        text = "\n".join(item for item in texts if item).strip()
        if not text:
            raise RuntimeError("Vertex generateContent returned no text")
        return text

    def generate_content(self, prompt: str) -> str:
        """Free-text generation for HITL suggestions (no JSON constraint)."""
        if not self.configured():
            raise RuntimeError("Vertex Gemini project is not configured")
        token = self._access_token()
        url = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/"
            f"projects/{self.project}/locations/{self.location}/"
            f"publishers/google/models/{self.model}:generateContent"
        )
        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 512,
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vertex generateContent failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(
                f"Vertex generateContent failed: {type(exc).__name__}"
            ) from exc
        payload = json.loads(raw.decode("utf-8"))
        parts = (
            (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
            or []
        )
        texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
        text = "\n".join(item for item in texts if item).strip()
        if not text:
            raise RuntimeError("Vertex generateContent returned no text")
        return text


def default_gemini_field_client() -> GeminiFieldClient | None:
    """Return a Vertex client only when a project is configured."""
    client = VertexGeminiFieldClient()
    if not client.configured():
        return None
    return client


def ask_gemini_unique_field(
    *,
    dialog_title: str,
    visible_labels: Iterable[str],
    block_reason: str = "",
    page_url: str = "",
    client: GeminiFieldClient | None = None,
) -> UniqueFieldDecision:
    """Ask Gemini for one unique field, then APPLY or HITL. Fail closed."""
    description = describe_blocked_dialog(
        dialog_title=dialog_title,
        visible_labels=visible_labels,
        block_reason=block_reason,
    )
    if not description["visible_labels"]:
        return _hitl(
            "no safe visible labels to send to Gemini; HITL Carlo",
            gemini_asked=False,
        )
    active = client if client is not None else default_gemini_field_client()
    if active is None:
        return _hitl(
            "Gemini/Vertex is not configured for stuck-field help; HITL Carlo",
            gemini_asked=False,
        )
    prompt = build_gemini_unique_field_prompt(
        dialog_title=description["dialog_title"],
        visible_labels=description["visible_labels"],
        block_reason=description["block_reason"],
        page_url=page_url,
    )
    try:
        raw = active.generate_unique_field(prompt)
    except Exception as exc:
        return _hitl(
            f"Gemini request failed ({type(exc).__name__}); HITL Carlo",
            gemini_asked=True,
        )
    payload = _parse_gemini_payload(raw)
    if not payload:
        return _hitl("Gemini returned an unreadable field suggestion; HITL Carlo", gemini_asked=True)
    decision = str(payload.get("decision") or "").strip().casefold()
    if decision in {"unsure", "hitl", "unknown", ""}:
        detail = redact_text(str(payload.get("reason") or "Gemini is unsure"))
        return _hitl(f"{detail}; HITL Carlo", gemini_asked=True)
    if decision != "unique":
        return _hitl("Gemini did not name exactly one unique field; HITL Carlo", gemini_asked=True)
    field_label = _safe_label(str(payload.get("field_label") or ""))
    locator = redact_text(str(payload.get("locator") or "")).strip()
    if not field_label or not _label_is_visible(field_label, description["visible_labels"]):
        return _hitl(
            "Gemini field was not one of the visible labels; HITL Carlo",
            gemini_asked=True,
        )
    if _locator_is_forbidden(locator):
        return _hitl(
            "Gemini locator was positional or not unique; refuse to guess; HITL Carlo",
            gemini_asked=True,
        )
    extra_labels = payload.get("field_labels") or payload.get("alternates")
    if extra_labels:
        return _hitl("Gemini named more than one field; HITL Carlo", gemini_asked=True)
    return UniqueFieldDecision(
        action="APPLY",
        field_label=field_label,
        locator=locator,
        reason="Gemini named one unique visible field; unique-write still required",
        gemini_asked=True,
    )


def resolve_blocked_unique_write(
    *,
    block_reason: str,
    dialog_title: str,
    visible_labels: Iterable[str],
    page_url: str = "",
    client: GeminiFieldClient | None = None,
) -> UniqueFieldDecision:
    """Hook for a PLAYWRIGHT_BLOCKED write: Gemini once, then APPLY or HITL."""
    safe_reason = redact_text(str(block_reason or PLAYWRIGHT_BLOCKED))
    if PLAYWRIGHT_BLOCKED not in safe_reason:
        safe_reason = f"{PLAYWRIGHT_BLOCKED}: {safe_reason}"
    return ask_gemini_unique_field(
        dialog_title=dialog_title,
        visible_labels=visible_labels,
        block_reason=safe_reason,
        page_url=page_url,
        client=client,
    )


@dataclass(frozen=True)
class LiveOptionDecision:
    """Gemini pick of one live dropdown option. Never guess; never invent."""

    action: str
    option: str | None
    reason: str
    gemini_asked: bool
    hitl_operator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "option": self.option,
            "reason": self.reason,
            "gemini_asked": self.gemini_asked,
            "hitl_operator": self.hitl_operator,
        }


def _option_hitl(reason: str, *, gemini_asked: bool) -> LiveOptionDecision:
    return LiveOptionDecision(
        action="HITL",
        option=None,
        reason=f"{PLAYWRIGHT_BLOCKED}: {reason}",
        gemini_asked=gemini_asked,
        hitl_operator=HITL_OPERATOR,
    )


def normalize_option_text(value: str) -> str:
    """Strip and casefold. Matching only; never rewrite a live label."""
    return str(value or "").strip().casefold()


def exact_live_option(wanted: str, live_options: Iterable[str]) -> str | None:
    """Return the live option whose text equals wanted, or None.

    Exact after strip/casefold only. No substring. No alias table.
    Unique match required.
    """
    wanted_norm = normalize_option_text(wanted)
    if not wanted_norm:
        return None
    hits: list[str] = []
    seen: set[str] = set()
    for raw in live_options:
        text = str(raw or "").strip()
        if not text:
            continue
        key = normalize_option_text(text)
        if key != wanted_norm or key in seen:
            continue
        seen.add(key)
        hits.append(text)
    if len(hits) == 1:
        return hits[0]
    return None


def _safe_live_options(live_options: Iterable[str]) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()
    for raw in live_options:
        label = _safe_label(raw)
        if not label:
            continue
        key = normalize_option_text(label)
        if key in seen:
            continue
        seen.add(key)
        options.append(label)
    return options


def build_gemini_live_option_prompt(
    *,
    widget_name: str,
    wanted: str,
    live_options: Iterable[str],
) -> str:
    options = _safe_live_options(live_options)
    wanted_text = _safe_label(wanted) or "(empty)"
    widget = _safe_label(widget_name) or "dropdown"
    return (
        f"The {widget} widget has no exact text match for {wanted_text!r}. "
        "That is expected. Do not answer unsure just because the wanted "
        "value is not copied verbatim in the list.\n"
        "Pick the ONE live option that is the same choice as the wanted "
        "value. Copy that option's text exactly. Do not invent a label.\n\n"
        f"Widget: {widget}\n"
        f"Wanted value: {wanted_text}\n"
        "Live options from THAT widget only:\n"
        + ("\n".join(f"- {option}" for option in options) or "- (none)")
        + "\n\n"
        "Reply with JSON only, no markdown:\n"
        '{"decision":"unique","option":"<exact live option text>"}\n'
        "or\n"
        '{"decision":"unsure","reason":"<why you cannot name exactly one>"}\n'
        "option must be copied exactly from Live options. "
        "Use unsure only when you cannot name exactly one option from the list."
    )


def named_live_option_from_gemini(raw: str, live_options: Iterable[str]) -> str | None:
    """Return a live option Gemini named, or None. Never invent."""
    options = [str(item).strip() for item in live_options if str(item).strip()]
    if not options:
        return None
    text = redact_text(str(raw or "")).strip()
    if not text:
        return None
    payload = _parse_gemini_payload(text)
    candidates: list[str] = []
    if payload:
        for key in ("option", "field_label", "choice"):
            value = _safe_label(str(payload.get(key) or ""))
            if value:
                candidates.append(value)
    stripped = text.strip().strip("\"'")
    if stripped:
        candidates.append(stripped)
        first_line = stripped.splitlines()[0].strip().strip("\"'")
        if first_line:
            candidates.append(first_line)
    for candidate in candidates:
        hit = exact_live_option(candidate, options)
        if hit is not None:
            return hit
    return None


def ask_gemini_live_option(
    *,
    widget_name: str,
    wanted: str,
    live_options: Iterable[str],
    client: GeminiFieldClient | None = None,
) -> LiveOptionDecision:
    """Ask Gemini which ONE live option to select. Apply only if it is in the list."""
    options = _safe_live_options(live_options)
    exact = exact_live_option(wanted, options)
    if exact is not None:
        return LiveOptionDecision(
            action="APPLY",
            option=exact,
            reason="wanted value is an exact live option",
            gemini_asked=False,
        )
    if not options:
        return _option_hitl(
            "no safe live options to send to Gemini; HITL Carlo",
            gemini_asked=False,
        )
    active = client if client is not None else default_gemini_field_client()
    if active is None:
        return _option_hitl(
            "Gemini/Vertex is not configured for live-option help; HITL Carlo",
            gemini_asked=False,
        )
    prompt = build_gemini_live_option_prompt(
        widget_name=widget_name,
        wanted=wanted,
        live_options=options,
    )
    raw = ""
    try:
        raw = active.generate_unique_field(prompt)
    except Exception:
        generate_content = getattr(active, "generate_content", None)
        if generate_content is None:
            return _option_hitl(
                "Gemini request failed; HITL Carlo",
                gemini_asked=True,
            )
        try:
            raw = generate_content(prompt)
        except Exception as exc:
            return _option_hitl(
                f"Gemini request failed ({type(exc).__name__}); HITL Carlo",
                gemini_asked=True,
            )
    live = named_live_option_from_gemini(raw, options)
    if live is not None:
        return LiveOptionDecision(
            action="APPLY",
            option=live,
            reason="Gemini named one live option",
            gemini_asked=True,
        )
    generate_content = getattr(active, "generate_content", None)
    if generate_content is not None and raw:
        retry_prompt = (
            f"Wanted: {wanted}\n"
            "Name exactly one option from this live list. Reply with that "
            "option text only.\n"
            + "\n".join(f"- {option}" for option in options)
        )
        try:
            retry_raw = generate_content(retry_prompt)
        except Exception:
            retry_raw = ""
        live = named_live_option_from_gemini(retry_raw, options)
        if live is not None:
            return LiveOptionDecision(
                action="APPLY",
                option=live,
                reason="Gemini named one live option",
                gemini_asked=True,
            )
    payload = _parse_gemini_payload(raw)
    if payload and str(payload.get("decision") or "").strip().casefold() in {
        "unsure",
        "hitl",
        "unknown",
        "",
    }:
        extra = payload.get("options") or payload.get("alternates")
        if extra:
            return _option_hitl("Gemini named more than one option; HITL Carlo", gemini_asked=True)
        detail = redact_text(str(payload.get("reason") or "Gemini is unsure"))
        return _option_hitl(f"{detail}; HITL Carlo", gemini_asked=True)
    return _option_hitl(
        "Gemini did not name one live option; HITL Carlo",
        gemini_asked=True,
    )
