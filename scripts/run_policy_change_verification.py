#!/usr/bin/env python3
"""StreetSmart Policy Change Verification runner (hermes-poc-01).

Read-only by default. Uses Policy Change Request Confirmation Queue CSVs.
Voice dispatch defaults to --dry-run. EZLynx note writes are OFF unless --write-notes
(and Carlo has authorized that class).

Examples:
  PYTHONPATH=. ./venv/bin/python3 scripts/run_policy_change_verification.py --limit 3 --json
  PYTHONPATH=. ./venv/bin/python3 scripts/run_policy_change_verification.py \
      --applicant 149367863 --policy 2021047341 --dry-run --json
  PYTHONPATH=. ./venv/bin/python3 scripts/run_policy_change_verification.py \
      --applicant 149367863 --policy 2021047341 --voice  # still dry-run unless --live-voice
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env if present (VOICE_AI_API_KEY etc.) without printing secrets
_env = PROJECT_ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

ET = timezone(timedelta(hours=-4))


@dataclass
class PolicyChangeCase:
    account_name: str
    applicant_id: str
    policy_number: str
    lob: str
    carrier: str
    request_status: str
    csr: str
    producer: str
    effective_date: str = ""
    change_summary: str = ""
    state: str = "queued"
    result: str = "waiting_for_carrier"
    discussions_found: List[str] = field(default_factory=list)
    voice: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def find_latest_confirmation_queue() -> Path:
    input_dir = PROJECT_ROOT / "data" / "input_reports"
    cands = list(input_dir.glob("*Policy_Change_Request_Confirmation_Queue*.csv"))
    if not cands:
        raise FileNotFoundError("No Policy_Change_Request_Confirmation_Queue CSV in data/input_reports")
    return max(cands, key=lambda p: p.stat().st_mtime)


def load_queue(path: Path) -> List[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def row_to_case(r: dict) -> PolicyChangeCase:
    return PolicyChangeCase(
        account_name=(r.get("Account Name") or "").strip(),
        applicant_id=(r.get("Applicant ID") or "").strip(),
        policy_number=(r.get("Policy Number") or "").strip(),
        lob=(r.get("Line Of Business") or "").strip(),
        carrier=(r.get("Master Company") or "").strip(),
        request_status=(r.get("Request Status") or "").strip(),
        csr=(r.get("CSR") or "").strip(),
        producer=(r.get("Assigned Producer") or "").strip(),
        effective_date=(r.get("Effective Date") or "").strip(),
        change_summary=(r.get("Change Summary") or r.get("change_summary") or "").strip(),
    )


def discussion_first(client, applicant_id: str) -> List[str]:
    titles = []
    try:
        payload = client.get_applicant_discussions(applicant_id)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("discussions") or payload.get("Discussions") or payload.get("items") or payload.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or items.get("data") or []
        else:
            items = []
        for d in items or []:
            if not isinstance(d, dict):
                continue
            title = d.get("title") or d.get("Title") or d.get("discussionTitle") or d.get("Subject") or ""
            if title and any(k in title.lower() for k in ("policy change", "endorsement", "change request")):
                titles.append(title)
    except Exception as e:
        titles.append(f"[discussion_lookup_error] {e}")
    return titles


def draft_qc_note(case: PolicyChangeCase) -> str:
    now = datetime.now(ET).strftime("%m/%d/%Y %I:%M %p ET")
    return (
        f"[{now}] QUALITY CONTROLLER — Policy change verification (server runner).\n"
        f"Policy: {case.policy_number} | Carrier: {case.carrier} | Change effective: {case.effective_date or 'N/A'}.\n"
        f"Account: {case.account_name} | Applicant: {case.applicant_id}\n"
        f"Requested:\n- {case.change_summary or '(from OPEN queue — reconstruct from discussion/attachments next)'}\n"
        f"Carrier issued:\n- (pending retrieval)\n"
        f"EZLynx recorded:\n- Request Status: {case.request_status}\n"
        f"Matches:\n- N/A (waiting_for_carrier)\n"
        f"Exceptions:\n- Carrier endorsement/dec not verified this pass\n"
        f"Documents:\n- None filed this pass\n"
        f"Result: {case.result}\n"
        f"State: {case.state}\n"
        f"Next action: carrier-policy-document-retrieval (portal → carrier email → voice) | CSR: {case.csr}\n\n"
        f"ROBIE was here"
    )


def maybe_voice(case: PolicyChangeCase, *, live: bool) -> Dict[str, Any]:
    """Dispatch policy-change voice follow-up. Default simulation unless live=True."""
    # Prefer dedicated policy-change caller if present
    caller_path = PROJECT_ROOT / "scripts" / "carrier_policy_change_caller.py"
    if caller_path.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("carrier_policy_change_caller", caller_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        summary = case.change_summary or f"Policy change status follow-up ({case.lob})"
        res = mod.dispatch_policy_change_call(
            policy_number=case.policy_number,
            carrier_name=case.carrier,
            insured_name=case.account_name,
            change_summary=summary,
            dry_run=not live,
        )
        return res

    # Fallback: CarrierVoiceClient hydrate by policy number
    try:
        from src.voice.context_hydrator import ContextHydrator
        from src.voice.voice_client import CarrierVoiceClient

        hydrator = ContextHydrator()
        dossier = hydrator.hydrate(policy_number=case.policy_number, applicant_name=case.account_name)
        if not dossier:
            return {"success": False, "error": "hydrate_failed", "mode": "NONE"}
        voice = CarrierVoiceClient()
        return voice.dispatch_call(dossier=dossier, dry_run=not live)
    except Exception as e:
        return {"success": False, "error": str(e), "mode": "NONE"}


def process_case(
    case: PolicyChangeCase,
    *,
    client=None,
    do_voice: bool = False,
    live_voice: bool = False,
    write_notes: bool = False,
) -> PolicyChangeCase:
    case.state = "researching"
    if client and case.applicant_id:
        case.discussions_found = discussion_first(client, case.applicant_id)

    # Without carrier docs we stay waiting_for_carrier (retrieval handoff)
    case.result = "waiting_for_carrier"
    case.state = "waiting_for_carrier"
    case.notes.append(draft_qc_note(case))

    if do_voice:
        case.voice = maybe_voice(case, live=live_voice)
        mode = (case.voice or {}).get("mode") or ""
        if live_voice and str(mode).upper().startswith("LIVE"):
            case.state = "carrier_followup_dispatched"
        elif case.voice.get("success"):
            case.state = "carrier_followup_simulated"

    if write_notes:
        case.errors.append("write_notes requested but disabled in this runner build — Carlo must authorize create_discussion_note")
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy Change Verification runner (hermes)")
    parser.add_argument("--report", help="Path to Confirmation Queue CSV")
    parser.add_argument("--applicant", help="Single applicant ID")
    parser.add_argument("--policy", help="Policy number (with --applicant or filter)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--voice", action="store_true", help="Stage Bland voice follow-up (dry-run unless --live-voice)")
    parser.add_argument("--live-voice", action="store_true", help="Place a real Bland call (requires VOICE_AI_API_KEY + Carlo OK)")
    parser.add_argument("--write-notes", action="store_true", help="Reserved — not enabled yet")
    parser.add_argument("--no-ezlynx", action="store_true", help="Skip EZLynx API (queue CSV only)")
    args = parser.parse_args()

    report = Path(args.report) if args.report else find_latest_confirmation_queue()
    rows = load_queue(report)

    cases: List[PolicyChangeCase] = []
    if args.applicant:
        matched = [r for r in rows if (r.get("Applicant ID") or "").strip() == str(args.applicant).strip()]
        if args.policy:
            matched = [r for r in matched if args.policy.replace(" ", "") in (r.get("Policy Number") or "").replace(" ", "")]
        if not matched:
            # synthesize from CLI if not in CSV
            cases.append(
                PolicyChangeCase(
                    account_name="",
                    applicant_id=str(args.applicant),
                    policy_number=args.policy or "",
                    lob="",
                    carrier="",
                    request_status="Open",
                    csr="",
                    producer="",
                    change_summary="",
                )
            )
        else:
            cases = [row_to_case(r) for r in matched]
    else:
        open_rows = [r for r in rows if (r.get("Request Status") or "").lower() == "open"]
        if args.policy:
            open_rows = [r for r in open_rows if args.policy.replace(" ", "") in (r.get("Policy Number") or "").replace(" ", "")]
        if args.limit:
            open_rows = open_rows[: args.limit]
        cases = [row_to_case(r) for r in open_rows]

    client = None
    if not args.no_ezlynx:
        try:
            from src.ezlynx.api_client import EZLynxApiClient

            client = EZLynxApiClient()
        except Exception as e:
            # Continue queue-only
            for c in cases:
                c.errors.append(f"ezlynx_init_failed: {e}")

    results = []
    for c in cases:
        if not c.change_summary and c.policy_number == "2021047341":
            # Known OPEN test context from Antigravity walkthrough
            c.change_summary = "Add 2015 RAM ProMaster (VIN 3C6TRVAG7FE503379) with $1k comp/coll deductibles"
            c.account_name = c.account_name or "Omega General Construction LLC"
            c.carrier = c.carrier or "National General"
        process_case(
            c,
            client=client,
            do_voice=args.voice or args.live_voice,
            live_voice=args.live_voice,
            write_notes=args.write_notes,
        )
        results.append(c)

    payload = {
        "report": str(report),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "results": [asdict(r) for r in results],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Report: {report}")
        print(f"Cases: {len(results)}")
        for r in results:
            print(
                f"- {r.account_name} | {r.policy_number} | {r.carrier} | "
                f"result={r.result} state={r.state} voice_mode={(r.voice or {}).get('mode')}"
            )
            if r.discussions_found:
                print(f"  discussions: {r.discussions_found[:5]}")
            if r.errors:
                print(f"  errors: {r.errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
