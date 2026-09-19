"""Read-only Test probe of 4372 mortgagee metadata keys.

Applicant **220250093** (ROBIE Test LLC) only. ``ROBIE_ENV=TEST`` only.
Never Production. Never writes notes/docs/labels. Never downloads PDFs
for field fill. Prints **key names**, not values.

Dusty — run on hermes-test-01 (see docs/MORTGAGEE_ENRICHMENT_4372.md):

    hostname   # must print hermes-test-01
    export ROBIE_ENV=TEST
    set -a
    source /etc/streetsmart-hermes-test/robie-verification.env
    set +a
    PYTHONPATH=. python3 scripts/probe_4372_mortgagee_metadata.py

If live keys that look like mortgagee/loan are missing from the
allowlist, reply with the literal ``unknown`` list from this JSON.
Do not invent Production keys from memory.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

from .mortgagee_enrichment import (
    ROBIE_TEST_APPLICANT_ID,
    ProductionClientRefused,
    bind_test_enrichment_ports,
    classify_metadata_keys,
    collect_payload_keys,
    looks_like_declaration,
)
from .runtime_env import PRODUCTION_ENV_NAMES, current_robie_env


def refuse_production_host(hostname: str | None = None) -> None:
    host = (hostname or socket.gethostname() or "").strip().casefold()
    if "hermes-poc-01" in host or host.startswith("hermes-poc"):
        raise ProductionClientRefused(
            "4372 metadata probe refuses Production host hermes-poc-01"
        )


def require_test_applicant(applicant_id: str) -> str:
    applicant = str(applicant_id or "").strip()
    if applicant != ROBIE_TEST_APPLICANT_ID:
        raise ProductionClientRefused(
            f"4372 metadata probe allows only ROBIE Test applicant "
            f"{ROBIE_TEST_APPLICANT_ID} (got {applicant or 'empty'})"
        )
    return applicant


def probe_metadata(
    *,
    applicant_id: str = ROBIE_TEST_APPLICANT_ID,
    policy_number: str = "",
    ports=None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Search DocumentApi + optional PolicyApi. Keys only. Read-only."""
    refuse_production_host(hostname)
    env = current_robie_env()
    if env in PRODUCTION_ENV_NAMES:
        raise ProductionClientRefused(
            "4372 metadata probe is Test-only; ROBIE_ENV is Production"
        )
    if env != "TEST":
        raise ProductionClientRefused(
            f"ROBIE_ENV must be TEST to run the 4372 metadata probe "
            f"(got {env or 'unset'})"
        )
    applicant = require_test_applicant(applicant_id)
    bound = ports if ports is not None else bind_test_enrichment_ports()
    summary: dict[str, Any] = {
        "environment": env,
        "applicant_id": applicant,
        "policy_number": str(policy_number or "").strip(),
        "writes": False,
        "downloads": False,
        "document_search": {"ok": False},
        "policy_search": {"ok": False, "skipped": not bool(str(policy_number or "").strip())},
        "keys": {"document": [], "policy": [], "combined": []},
        "classification": {},
        "declaration_document_ids": [],
    }
    if bound.documents is not None:
        try:
            payload = bound.documents.search_applicant_documents(applicant)
        except Exception as exc:
            summary["document_search"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            keys = collect_payload_keys(payload)
            dec_ids = []
            rows = payload.get("results") if isinstance(payload, dict) else payload
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and looks_like_declaration(row):
                        raw_id = str(row.get("id") or "").strip()
                        if raw_id.isdigit():
                            dec_ids.append(raw_id)
            summary["document_search"] = {
                "ok": True,
                "result_count": len(rows) if isinstance(rows, list) else 0,
            }
            summary["keys"]["document"] = keys
            summary["declaration_document_ids"] = dec_ids
    if bound.policy is not None and str(policy_number or "").strip():
        try:
            payload = bound.policy.search_policy_by_number(str(policy_number).strip())
        except Exception as exc:
            summary["policy_search"] = {
                "ok": False,
                "skipped": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            keys = collect_payload_keys(payload)
            summary["policy_search"] = {"ok": True, "skipped": False}
            summary["keys"]["policy"] = keys
    combined = sorted(set(summary["keys"]["document"]) | set(summary["keys"]["policy"]))
    summary["keys"]["combined"] = combined
    summary["classification"] = classify_metadata_keys(combined)
    return summary


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    applicant = ROBIE_TEST_APPLICANT_ID
    policy_number = ""
    i = 0
    while i < len(args):
        if args[i] in {"--applicant-id", "-a"} and i + 1 < len(args):
            applicant = args[i + 1]
            i += 2
            continue
        if args[i] in {"--policy-number", "-p"} and i + 1 < len(args):
            policy_number = args[i + 1]
            i += 2
            continue
        print(
            "usage: probe_4372_mortgagee_metadata.py "
            "[--applicant-id 220250093] [--policy-number HO-…]",
            file=sys.stderr,
        )
        return 2
    try:
        summary = probe_metadata(
            applicant_id=applicant, policy_number=policy_number,
        )
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "environment": current_robie_env() or os.environ.get("ROBIE_ENV", ""),
        }, indent=2))
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["document_search"].get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
