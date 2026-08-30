"""Self-checks for the immutable non-Ascend runtime release."""

from __future__ import annotations

import json
from pathlib import Path

from .job_schema import EXECUTABLE_SKILL_CONTRACTS, BOUNDED_JOB_SCHEMAS
from .request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION, classify_request


ASCEND_REQUEST_FIXTURES = (
    "Create a program in Ascend",
    "Run the Ascend locator artifact audit",
    "Open useascend and inspect premium finance",
    "Use PAWIVA account 221398001",
)


def verify_non_ascend_release(root: str | Path | None = None) -> dict[str, object]:
    base = Path(root or Path(__file__).resolve().parents[1]).resolve()
    forbidden = [
        *base.glob("robie_job_engine/ascend*.py"),
        base / "robie_job_engine/locators/ascend.json",
        *base.glob("deploy/hermes/skills/ascend-*"),
        *base.glob("skills/ascend-*"),
        *base.glob("scripts/*ascend*"),
    ]
    present = sorted(str(path.relative_to(base)) for path in forbidden if path.exists())
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
    routing = {
        text: {
            "action_type": result.action_type,
            "hold_status": result.hold_status,
        }
        for text in ASCEND_REQUEST_FIXTURES
        for result in (classify_request(text),)
    }
    route_failures = [
        text
        for text, result in routing.items()
        if result != {"action_type": "hermes.unavailable", "hold_status": "FAILED"}
    ]
    ok = not present and not registered and not route_failures
    return {
        "ok": ok,
        "profile": "NON_ASCEND",
        "forbidden_paths_present": present,
        "ascend_registrations": registered,
        "routing": routing,
    }


def main() -> int:
    result = verify_non_ascend_release()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
