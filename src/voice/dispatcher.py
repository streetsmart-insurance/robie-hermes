"""
CLI dispatcher for autonomous carrier voice calls.

Hydrates a CallingDossier from a policy number and dispatches via
CarrierVoiceClient. Does not post EZLynx notes — webhook_server handles
completed-call pushback.

Usage:
  PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher --policy-number "<Policy#>" --dry-run
  PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher --policy-number "<Policy#>"
  PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher --policy-number "<Policy#>" --phone "+1XXXXXXXXXX" --instructions "<Instructions>"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

from src.voice.context_hydrator import (
    CALL_TYPE_CARRIER,
    CallingDossier,
    ContextHydrator,
)
from src.voice.voice_client import CarrierVoiceClient

logger = logging.getLogger("voice_dispatcher")


class VoiceCallDispatcher:
    """Terminal/script entry point parallel to EmailCallDispatcher."""

    def __init__(
        self,
        hydrator: Optional[ContextHydrator] = None,
        voice_client: Optional[CarrierVoiceClient] = None,
    ):
        self.hydrator = hydrator or ContextHydrator()
        self.voice = voice_client or CarrierVoiceClient()

    def dispatch(
        self,
        policy_number: str,
        phone: Optional[str] = None,
        instructions: Optional[str] = None,
        dry_run: bool = False,
        call_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Hydrate policy context, then dispatch (or simulate) the carrier call."""
        if not policy_number or not str(policy_number).strip():
            return _failure_result(
                error="HYDRATE_FAILED",
                policy=policy_number,
                details="Policy number is required.",
            )

        try:
            dossier = self.hydrator.hydrate(
                policy_number=policy_number.strip(),
                phone_override=phone,
                instructions=instructions,
                call_type=call_type or CALL_TYPE_CARRIER,
            )
        except Exception as exc:
            logger.error("Hydrate failed for %s: %s", policy_number, exc)
            return _failure_result(
                error="HYDRATE_FAILED",
                policy=policy_number,
                details=str(exc),
            )

        if dossier is None:
            logger.error("Could not hydrate policy context for %s", policy_number)
            return _failure_result(error="HYDRATE_FAILED", policy=policy_number)

        try:
            self.hydrator.enrich_identity_from_ezlynx(dossier)
        except Exception as exc:
            logger.debug("Producer/first-name enrichment skipped for %s: %s", policy_number, exc)
        # CLI dispatch has no label invoker; warm transfer stays off unless a
        # requestor was already set (HITL-safe — do not use EZLynx Producer).

        try:
            result = self.voice.dispatch_call(dossier=dossier, dry_run=dry_run)
        except Exception as exc:
            logger.error("Dispatch failed for %s: %s", policy_number, exc)
            return _failure_result(
                error="DISPATCH_FAILED",
                policy=dossier.policy_number,
                phone=dossier.carrier_phone,
                carrier=dossier.carrier_name,
                details=str(exc),
            )

        return summarize_dispatch_result(result, dossier)


def summarize_dispatch_result(
    result: Dict[str, Any],
    dossier: Optional[CallingDossier] = None,
) -> Dict[str, Any]:
    """Normalize dispatch output to the documented summary fields."""
    summary: Dict[str, Any] = {
        "success": bool(result.get("success")),
        "mode": result.get("mode"),
        "call_id": result.get("call_id"),
        "phone": result.get("phone_number") or result.get("phone") or (dossier.carrier_phone if dossier else None),
        "carrier": result.get("carrier") or (dossier.carrier_name if dossier else None),
        "policy": result.get("policy_number") or result.get("policy") or (dossier.policy_number if dossier else None),
    }
    if result.get("status"):
        summary["status"] = result["status"]
    if result.get("error"):
        summary["error"] = result["error"]
    if result.get("details"):
        summary["details"] = result["details"]
    if result.get("insured_name"):
        summary["insured_name"] = result["insured_name"]
    elif dossier and dossier.insured_name:
        summary["insured_name"] = dossier.insured_name
    return summary


def _failure_result(
    error: str,
    policy: Optional[str] = None,
    phone: Optional[str] = None,
    carrier: Optional[str] = None,
    details: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "mode": None,
        "call_id": None,
        "phone": phone,
        "carrier": carrier,
        "policy": policy,
        "error": error,
    }
    if details:
        result["details"] = details
    return result


def print_dispatch_result(result: Dict[str, Any]) -> None:
    """Print a human one-liner plus JSON of the dispatch result."""
    status = "SUCCESS" if result.get("success") else "FAILED"
    print(
        f"Voice dispatch {status} | mode={result.get('mode') or '-'} | "
        f"call_id={result.get('call_id') or '-'} | phone={result.get('phone') or '-'} | "
        f"carrier={result.get('carrier') or '-'} | policy={result.get('policy') or '-'}"
    )
    if result.get("error"):
        print(f"Error: {result['error']}")
    print(json.dumps(result, indent=2, default=str))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dispatch an autonomous carrier voice call for a policy number. "
            "Use --dry-run to simulate without placing a live Bland call."
        )
    )
    parser.add_argument(
        "--policy-number",
        required=True,
        help="Policy number to hydrate and call about.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate dispatch only (no live Bland call / $0).",
    )
    parser.add_argument(
        "--phone",
        default=None,
        help="Optional phone override (maps to hydrator phone_override).",
    )
    parser.add_argument(
        "--instructions",
        default=None,
        help="Optional custom instructions for the voice agent.",
    )
    return parser.parse_args(argv)


def main(
    argv: Optional[List[str]] = None,
    dispatcher: Optional[VoiceCallDispatcher] = None,
) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    dispatcher = dispatcher or VoiceCallDispatcher()
    result = dispatcher.dispatch(
        policy_number=args.policy_number,
        phone=args.phone,
        instructions=args.instructions,
        dry_run=args.dry_run,
    )
    print_dispatch_result(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
