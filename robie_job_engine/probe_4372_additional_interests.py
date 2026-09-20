"""Read-only Test probe of the 4372 Additional Interests table.

Attaches existing SSRobie Chrome via CDP. Does **not** invent policy
search navigation — open the policy FormEntry page first. Never writes
notes/docs. Never Production. Never real-client writes.

Dusty — run on hermes-test-01 (see docs/MORTGAGEE_ENRICHMENT_4372.md).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from .mortgagee_enrichment import (
    ProductionClientRefused,
    bind_test_browser_port,
    read_additional_interests_source,
    EnrichmentPorts,
)
from .runtime_env import PRODUCTION_ENV_NAMES, current_robie_env


def refuse_production_host(hostname: str | None = None) -> None:
    host = (hostname or socket.gethostname() or "").strip().casefold()
    if "hermes-poc-01" in host or host.startswith("hermes-poc"):
        raise ProductionClientRefused(
            "4372 Additional Interests probe refuses Production host hermes-poc-01"
        )


def probe_additional_interests(
    *,
    policy_number: str,
    applicant_id: str = "",
    ports=None,
    hostname: str | None = None,
    page: Any = None,
) -> dict[str, Any]:
    """Read the Additional Interests table. Structured cells only."""
    refuse_production_host(hostname)
    env = current_robie_env()
    if env in PRODUCTION_ENV_NAMES:
        raise ProductionClientRefused(
            "4372 Additional Interests probe is Test-only; ROBIE_ENV is Production"
        )
    if env != "TEST":
        raise ProductionClientRefused(
            "ROBIE_ENV must be TEST to run the 4372 Additional Interests "
            f"probe (got {env or 'unset'})"
        )
    number = str(policy_number or "").strip()
    if not number:
        raise ProductionClientRefused(
            "policy number is required (open that policy in SSRobie first)"
        )
    if ports is None:
        ports = EnrichmentPorts(
            browser=bind_test_browser_port(page=page),
        )
    snap = read_additional_interests_source(ports, number, applicant_id)
    return {
        "environment": env,
        "hostname": hostname or socket.gethostname(),
        "policy_number": number,
        "applicant_id": applicant_id,
        "available": snap.available,
        "explicit_empty": snap.explicit_empty,
        "status_hint": (
            "proven_zero" if snap.explicit_empty
            else "ready" if snap.mortgages
            else "incomplete"
        ),
        "mortgage_count": len(snap.mortgages),
        "mortgages": [
            {
                "lender_name": m.lender_name,
                "loan_number": m.loan_number,
                "interest_type": m.interest_type,
                "source": m.source,
            }
            for m in snap.mortgages
        ],
        "conflict": snap.conflict,
        "error": snap.error,
        "read_back": snap.read_back,
        "writes": False,
        "downloads": False,
        "pdf_scrape": False,
        "cdp_launched_chrome": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Additional Interests table probe (Test / SSRobie)",
    )
    parser.add_argument(
        "--policy-number",
        required=True,
        help="Policy already open in SSRobie Chrome (no search nav invented)",
    )
    parser.add_argument("--applicant-id", default="", help="optional; not written")
    args = parser.parse_args(argv)
    try:
        summary = probe_additional_interests(
            policy_number=args.policy_number,
            applicant_id=args.applicant_id,
        )
    except ProductionClientRefused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("available") or summary.get("explicit_empty") else 1


if __name__ == "__main__":
    raise SystemExit(main())
