"""Self-checks for the immutable, environment-gated runtime release."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .job_schema import EXECUTABLE_SKILL_CONTRACTS, BOUNDED_JOB_SCHEMAS
from .request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION, classify_request


# Real Ascend work only. A bare "Ascend locator artifact audit" mention is
# a weak name hit and must not hold after the strong/weak split (264a708f).
ASCEND_REQUEST_FIXTURES = (
    "Create a program in Ascend",
    "Run ascend-locator-artifact-audit",
    "Open useascend and inspect premium finance",
    "Use PAWIVA account 221398001",
)


def _routing(*, ascend_enabled: bool) -> dict[str, dict[str, str | None]]:
    previous = os.environ.get("ROBIE_ASCEND_API_ENABLED")
    try:
        if ascend_enabled:
            os.environ["ROBIE_ASCEND_API_ENABLED"] = "true"
        else:
            os.environ.pop("ROBIE_ASCEND_API_ENABLED", None)
        return {
            text: {
                "action_type": result.action_type,
                "hold_status": result.hold_status,
            }
            for text in ASCEND_REQUEST_FIXTURES
            for result in (classify_request(text),)
        }
    finally:
        if previous is None:
            os.environ.pop("ROBIE_ASCEND_API_ENABLED", None)
        else:
            os.environ["ROBIE_ASCEND_API_ENABLED"] = previous


def verify_non_ascend_release(root: str | Path | None = None) -> dict[str, object]:
    """Verify that bundled Ascend sources remain inert unless explicitly enabled.

    The historical function name is retained for callers. Official releases now
    intentionally contain Ascend source files, so source presence is evidence,
    not a failure. The release still fails if Ascend bypasses the environment
    gate or registers a direct Job Engine action/schema/worker.
    """
    base = Path(root or Path(__file__).resolve().parents[1]).resolve()
    ascend_sources = [
        *base.glob("robie_job_engine/ascend*.py"),
        base / "robie_job_engine/locators/ascend.json",
        *base.glob("deploy/hermes/skills/ascend-*"),
        *base.glob("skills/ascend-*"),
        *base.glob("scripts/*ascend*"),
    ]
    present = sorted(str(path.relative_to(base)) for path in ascend_sources if path.exists())
    registered = sorted(
        key
        for source in (
            WORKER_FOR_ACTION,
            BOUNDED_ENGINE_ACTIONS,
            EXECUTABLE_SKILL_CONTRACTS,
            BOUNDED_JOB_SCHEMAS,
        )
        for key in source
        if str(key).startswith("ascend.")
    )
    disabled_routing = _routing(ascend_enabled=False)
    disabled_route_failures = [
        text
        for text, result in disabled_routing.items()
        if result != {"action_type": "hermes.unavailable", "hold_status": "FAILED"}
    ]
    enabled_routing = _routing(ascend_enabled=True)
    enabled_route_failures = [
        text
        for text, result in enabled_routing.items()
        if result != {"action_type": "hermes.google_chat_task", "hold_status": None}
    ]
    ok = not registered and not disabled_route_failures and not enabled_route_failures
    return {
        "ok": ok,
        "profile": "ENV_GATED_ASCEND",
        "ascend_source_paths_present": present,
        "source_presence_allowed": True,
        "ascend_registrations": registered,
        "disabled_routing": disabled_routing,
        "enabled_routing": enabled_routing,
    }


def main() -> int:
    result = verify_non_ascend_release()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
