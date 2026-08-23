from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str


class AllModelsFailed(RuntimeError):
    pass


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}".lower()


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    text = _error_text(exc)
    for code in (400, 408, 409, 429, 500, 502, 503, 504):
        if f"http {code}" in text or f"status {code}" in text:
            return code
    return None


def is_invalid_thought_signature(exc: Exception) -> bool:
    """Identify Gemini history that cannot be replayed to the selected endpoint."""
    text = _error_text(exc)
    return "invalid thought signature" in text or "invalid_thought_signature" in text


def is_retryable_provider_failure(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return _status_code(exc) in {408, 409, 429, 500, 502, 503, 504}


def strip_gemini_thought_signatures(value: Any) -> Any:
    """Return a copy safe to replay after a Gemini thought-signature rejection.

    Thought signatures are endpoint/model scoped. They must never be copied from
    a prior response into a different model or compatibility endpoint request.
    Ordinary message content and tool results are preserved.
    """
    if isinstance(value, list):
        return [strip_gemini_thought_signatures(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        normalized = key.replace("_", "").replace("-", "").lower()
        if normalized == "thoughtsignature":
            continue
        if normalized == "signature" and any(
            sibling.replace("_", "").replace("-", "").lower().startswith("thought")
            for sibling in value
        ):
            continue
        cleaned[key] = strip_gemini_thought_signatures(item)
    return cleaned


def execute_with_fallback(
    targets: Iterable[ModelTarget],
    call: Callable[[ModelTarget], Any],
    record: Callable[[ModelTarget, int, str, Exception | None], None],
    *,
    repair_invalid_thought: Callable[[ModelTarget, Exception], None] | None = None,
    authorize_call: Callable[[ModelTarget, int], None] | None = None,
) -> Any:
    """Use fallback only for provider/model failures, never action uncertainty."""
    errors: list[str] = []
    for ordinal, target in enumerate(targets, start=1):
        try:
            if authorize_call is not None:
                authorize_call(target, ordinal)
            result = call(target)
            record(target, ordinal, "SUCCESS", None)
            return result
        except Exception as exc:
            if is_invalid_thought_signature(exc):
                record(target, ordinal, "REPAIRABLE_CONTEXT_FAILURE", exc)
                if repair_invalid_thought is not None:
                    repair_invalid_thought(target, exc)
                    try:
                        if authorize_call is not None:
                            authorize_call(target, ordinal)
                        result = call(target)
                        record(target, ordinal, "SUCCESS_AFTER_CONTEXT_REPAIR", None)
                        return result
                    except Exception as repaired_exc:
                        exc = repaired_exc
            if not is_retryable_provider_failure(exc):
                raise
            record(target, ordinal, "RETRYABLE_FAILURE", exc)
            errors.append(f"{target.provider}/{target.model}: {type(exc).__name__}")
    raise AllModelsFailed("; ".join(errors) or "no model targets configured")
