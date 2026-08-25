"""Independent destination verifiers for structured Google Chat actions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from .models import VerificationEvidence, VerificationResult


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FilesystemSkillUpdateVerifier:
    """Reread an allowlisted SKILL.md and verify exact bytes plus freshness."""

    def __init__(self, allowed_roots: Iterable[str | Path]):
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
        if not self.allowed_roots:
            raise ValueError("at least one allowlisted Skill root is required")

    def _target(self, value: str) -> Path:
        target = Path(value).expanduser().resolve(strict=True)
        if target.name != "SKILL.md":
            raise PermissionError("filesystem.skill_update may verify only SKILL.md")
        if not any(target.is_relative_to(root) for root in self.allowed_roots):
            raise PermissionError("Skill target is outside the allowlisted roots")
        return target

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        expected_content = destination.get("expected_content", job["payload"].get("expected_content"))
        expected_sha256 = str(
            destination.get("expected_sha256", job["payload"].get("expected_sha256")) or ""
        ).lower()
        target_value = str(destination.get("target_path", job["payload"].get("target_path")) or "")
        expected = {
            "target_path": target_value,
            "content": expected_content,
            "sha256": expected_sha256,
        }
        try:
            target = self._target(target_value)
            data = target.read_bytes()
            decoded = data.decode("utf-8")
            stat = target.stat()
            observed = {
                "target_path": str(target),
                "content": decoded,
                "sha256": hashlib.sha256(data).hexdigest(),
                "mtime_ns": stat.st_mtime_ns,
            }
            action_time = datetime.fromisoformat(
                str(action.get("detail", {}).get("written_at") or job["created_at"]).replace("Z", "+00:00")
            ).timestamp()
            # Some deployed filesystems expose whole-second mtime granularity.
            # Keep the freshness gate while allowing only that documented
            # rounding window; exact path, bytes, and SHA-256 must still match.
            action_time_ns = int(action_time * 1_000_000_000)
            fresh = stat.st_mtime_ns >= action_time_ns - 1_000_000_000
            verified = (
                isinstance(expected_content, str)
                and bool(expected_sha256)
                and decoded == expected_content
                and observed["sha256"] == expected_sha256
                and str(target) == target_value
                and fresh
            )
            error = None if verified else "Skill file content, hash, path, or fresh timestamp does not match"
        except Exception as exc:
            observed = {"target_path": target_value, "error": f"{type(exc).__name__}: {exc}"}
            verified = False
            error = "Skill file destination could not be reread safely"
        evidence = VerificationEvidence(
            method="EXACT_FILESYSTEM_REREAD",
            source="allowlisted-skill-filesystem",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_utc_now(),
            locator=target_value or None,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)


class EzlynxSubmissionReadback(Protocol):
    def fresh_authenticated_structured_read(self, scope: dict[str, Any]) -> dict[str, Any]: ...


class EzlynxSubmissionAuditVerifier:
    """Verify a Submission Center audit from a fresh authenticated Playwright read."""

    def __init__(self, readback: EzlynxSubmissionReadback):
        self.readback = readback

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        resource_id = str(destination.get("resource_id") or job["payload"].get("resource_id") or "")
        scope = dict(destination.get("scope") or job["payload"].get("scope") or {})
        postcondition = dict(
            destination.get("expected_postcondition")
            or job["payload"].get("expected_postcondition")
            or {}
        )
        expected = {
            "resource_id": resource_id,
            "authenticated": True,
            "scope": scope,
            "postcondition": postcondition,
        }
        try:
            raw = dict(self.readback.fresh_authenticated_structured_read(scope))
            observed = {
                "resource_id": str(raw.get("resource_id") or ""),
                "authenticated": raw.get("authenticated") is True,
                "scope": dict(raw.get("scope") or {}),
                "postcondition": dict(raw.get("postcondition") or {}),
                "engine": raw.get("engine"),
                "fresh_navigation": raw.get("fresh_navigation") is True,
            }
            verified = all(
                (
                    resource_id,
                    postcondition,
                    observed["authenticated"],
                    observed["fresh_navigation"],
                    observed["engine"] == "playwright",
                    observed["resource_id"] == resource_id,
                    observed["scope"] == scope,
                    observed["postcondition"] == postcondition,
                )
            )
            error = None if verified else "fresh authenticated EZLynx Playwright read-back did not match"
        except Exception as exc:
            observed = {"resource_id": resource_id, "error": f"{type(exc).__name__}: {exc}"}
            verified = False
            error = "EZLynx Playwright destination could not be reread"
        evidence = VerificationEvidence(
            method="EZLYNX_PLAYWRIGHT_FRESH_READBACK",
            source="ezlynx-authenticated-playwright",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_utc_now(),
            locator=resource_id or None,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)
