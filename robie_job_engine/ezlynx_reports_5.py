"""Fail-closed configuration checks for EZLynx Five / Reports 5.0 sources."""

from __future__ import annotations

from typing import Any, Mapping


REPORTS_5_BASE_URL = "https://app.ezlynx.com/web/looker-reports"


def validate_reports_5_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Reject legacy report fallback and unverified enabled mappings."""

    if not config.get("enabled"):
        return {"source_status": "disabled"}
    if str(config.get("version") or "").strip() != "5.0":
        raise ValueError("EZLynx accountability collection requires Reports 5.0")
    if config.get("legacy_saved_reports_enabled"):
        raise ValueError("legacy EZLynx Saved Reports fallback must remain disabled")
    mappings = dict(config.get("mappings") or {})
    required = [str(item) for item in config.get("required_mappings", [])]
    missing = [name for name in required if not dict(mappings.get(name) or {}).get("verified")]
    if missing:
        raise ValueError(f"unverified EZLynx Reports 5.0 mappings: {', '.join(sorted(missing))}")
    return {
        "source_status": "verified",
        "version": "5.0",
        "base_url": str(config.get("base_url") or REPORTS_5_BASE_URL),
        "mappings": mappings,
    }
