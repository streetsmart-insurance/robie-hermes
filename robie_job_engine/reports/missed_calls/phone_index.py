"""Offline phone-index lookup.

Reads the phone index JSON produced by the watchdog's build_phone_index.py
from the daily EZLynx "All Applicants Phone Export - Full Book v2" XLSX
(report ID 4732). Index format contract (keys, lookup states) mirrors that
script exactly.

FAIL-CLOSED RULES (finding 1):
  * Index file missing / unreadable / wrong shape -> every lookup returns
    state "unavailable". Never "No Account".
  * Index older than ``max_age_days`` -> every lookup returns "unavailable"
    with reason "phone index stale". A stale book cannot prove absence, and
    applicants added after the export would be mislabeled "No Account".
  * Number matches 2+ applicants -> "ambiguous". The pipeline never picks one
    (M7 pending Sandeep).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import PhoneLookup
from .normalize import normalize_phone


class PhoneIndex:
    def __init__(
        self,
        path: str | Path,
        max_age_days: int = 7,
        now: Optional[datetime] = None,
    ) -> None:
        self.path = Path(path)
        self.max_age_days = max_age_days
        self._now = now or datetime.now(timezone.utc)
        self._index: Optional[dict[str, Any]] = None
        self._load_error: str = ""
        self._built_at: Optional[datetime] = None
        self._load()

    # -- loading ---------------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._load_error = f"phone index file not found: {self.path}"
            return
        except (OSError, json.JSONDecodeError) as exc:
            self._load_error = f"phone index unreadable: {exc}"
            return
        if not isinstance(payload, dict) or "phone_index" not in payload:
            self._load_error = "phone index has unexpected shape (missing 'phone_index')"
            return
        self._index = payload
        raw_built = payload.get("built_at") or ""
        try:
            self._built_at = datetime.fromisoformat(str(raw_built))
            if self._built_at.tzinfo is None:
                self._built_at = self._built_at.replace(tzinfo=timezone.utc)
        except ValueError:
            self._built_at = None

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def built_at(self) -> Optional[datetime]:
        return self._built_at

    @property
    def age_days(self) -> Optional[float]:
        if self._built_at is None:
            return None
        return (self._now - self._built_at).total_seconds() / 86400.0

    @property
    def stale(self) -> bool:
        """True when the book is too old to prove absence of an account."""
        if self._index is None:
            return True
        age = self.age_days
        if age is None:
            return True  # no trustworthy timestamp -> treat as stale
        return age > self.max_age_days

    @property
    def available(self) -> bool:
        return self._index is not None and not self.stale

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "loaded": self._index is not None,
            "load_error": self._load_error,
            "built_at": self._built_at.isoformat() if self._built_at else None,
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
            "max_age_days": self.max_age_days,
            "stale": self.stale,
            "source_rows": (self._index or {}).get("source_rows"),
            "unique_applicants": (self._index or {}).get("unique_applicants"),
            "unique_phones": (self._index or {}).get("unique_phones"),
        }

    # -- lookup ----------------------------------------------------------

    def lookup(self, raw_phone: object) -> PhoneLookup:
        """Look up a number. Never returns 'verified_no_account' on failure."""
        if self._index is None:
            return PhoneLookup(
                state="unavailable",
                reason=self._load_error or "phone index not loaded",
            )
        if self.stale:
            return PhoneLookup(
                state="unavailable",
                reason=(
                    "phone index stale "
                    f"(built {self._built_at.isoformat() if self._built_at else 'unknown'}, "
                    f"max_age_days={self.max_age_days})"
                ),
            )
        digits = normalize_phone(raw_phone)
        if not digits:
            return PhoneLookup(
                state="not_checked",
                reason=f"could not normalize input: {raw_phone!r}",
            )
        hits = self._index["phone_index"].get(digits, [])
        if len(hits) == 1:
            app = hits[0]
            return PhoneLookup(
                state="matched",
                applicant_id=str(app.get("applicant_id", "")),
                account_name=str(app.get("account_name", "")),
                applicant_type=str(app.get("applicant_type", "")),
                matched_via=list(app.get("matched_via") or []),
            )
        if len(hits) > 1:
            return PhoneLookup(
                state="ambiguous",
                candidates=hits,
                reason=f"number matches {len(hits)} applicants; human must choose",
            )
        return PhoneLookup(
            state="verified_no_account",
            reason="number not found in full applicant book",
        )
