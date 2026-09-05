#!/usr/bin/env python3
"""
Robie Carrier Voice Engine CLI.
Provides commands to hydrate calling context, dispatch outbound phone calls,
simulate completed call webhooks, listen for incoming webhooks, and poll emails for call commands.
"""

import argparse
import json
import logging
import sys
from typing import Optional

from src.voice.context_hydrator import ContextHydrator
from src.voice.voice_client import CarrierVoiceClient
from src.voice.email_dispatcher import EmailCallDispatcher
from src.voice.webhook_server import handle_completed_call, run_webhook_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("carrier_caller_cli")


def cmd_hydrate(args):
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(
        policy_number=args.query,
        applicant_name=args.query if not args.policy_only else None,
        phone_override=args.phone,
        instructions=args.instructions,
    )
    if not dossier:
        print(f"❌ Could not resolve policy or carrier context for '{args.query}'")
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"📞 CALLING DOSSIER: {dossier.policy_number} ({dossier.insured_name})")
    print("=" * 60)
    for k, v in dossier.to_dict().items():
        print(f"  {k:20s}: {v}")
    print("=" * 60 + "\n")


def cmd_call(args):
    hydrator = ContextHydrator()
    voice = CarrierVoiceClient()

    dossier = hydrator.hydrate(
        policy_number=args.query,
        applicant_name=args.query if not args.policy_only else None,
        phone_override=args.phone,
        instructions=args.instructions,
    )
    if not dossier:
        print(f"❌ Could not resolve context for '{args.query}'")
        sys.exit(1)

    print(f"\n🚀 Initiating outbound call for {dossier.policy_number} -> {dossier.carrier_name} ({dossier.carrier_phone})...")
    res = voice.dispatch_call(dossier=dossier, webhook_url=args.webhook_url, dry_run=args.dry_run)
    print("\nResult:")
    print(json.dumps(res, indent=2))


def cmd_simulate_webhook(args):
    print(f"\n📥 Simulating call completion webhook for Policy #{args.policy_number}...")
    payload = {
        "call_id": args.call_id or f"sim_call_{args.policy_number}",
        "recording_url": args.recording_url,
        "summary": args.summary,
        "concatenated_transcript": f"Simulated call transcript for {args.policy_number}.",
        "metadata": {
            "policy_number": args.policy_number,
            "carrier_name": args.carrier,
            "assigned_csr_email": args.csr_email,
        },
    }
    res = handle_completed_call(payload)
    print("\nProcessed Webhook Result:")
    print(json.dumps(res, indent=2))


def cmd_listen(args):
    print(f"🎧 Starting Robie Voice Webhook Server on port {args.port}...")
    run_webhook_server(host=args.host, port=args.port)


def cmd_poll_emails(args):
    print(f"📬 Polling robie@streetsmart.insurance for incoming CSR call requests...")
    dispatcher = EmailCallDispatcher()
    dispatched = dispatcher.process_inbound_call_requests(dry_run=args.dry_run)
    print(f"Processed {len(dispatched)} call requests.")
    for d in dispatched:
        print(f"  - {d.get('sender')}: {d.get('policy_number')} ({d.get('carrier')}) -> {d.get('dispatch_result', {}).get('status')}")


def main():
    parser = argparse.ArgumentParser(description="Robie Autonomous Carrier Voice Engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # hydrate
    p_hydrate = subparsers.add_parser("hydrate", help="Inspect resolved calling dossier for a policy or client")
    p_hydrate.add_argument("query", help="Policy number or client name")
    p_hydrate.add_argument("--phone", help="Override carrier phone number")
    p_hydrate.add_argument("--instructions", help="Specific CSR instructions")
    p_hydrate.add_argument("--policy-only", action="store_true", help="Match query only as policy number")
    p_hydrate.set_defaults(func=cmd_hydrate)

    # call
    p_call = subparsers.add_parser("call", help="Dispatch an outbound call to a carrier")
    p_call.add_argument("query", help="Policy number or client name")
    p_call.add_argument("--phone", help="Override carrier phone number")
    p_call.add_argument("--instructions", help="Specific instructions to convey")
    p_call.add_argument("--webhook-url", help="Callback URL for call completion")
    p_call.add_argument("--dry-run", action="store_true", help="Run in mock simulation mode without placing a real call")
    p_call.add_argument("--policy-only", action="store_true", help="Match query only as policy number")
    p_call.set_defaults(func=cmd_call)

    # simulate-webhook
    p_sim = subparsers.add_parser("simulate-webhook", help="Simulate a call completion webhook to test EZLynx note auto-threading")
    p_sim.add_argument("policy_number", help="Policy number")
    p_sim.add_argument("--summary", default="Representative confirmed renewal quote was issued and posted to portal.", help="Call summary")
    p_sim.add_argument("--recording-url", default="https://api.bland.ai/recordings/sample_call.mp3", help="Audio recording URL")
    p_sim.add_argument("--carrier", default="Carrier Underwriting", help="Carrier name")
    p_sim.add_argument("--csr-email", default="carlo@streetsmart.insurance", help="CSR email to notify")
    p_sim.add_argument("--call-id", help="Custom call ID")
    p_sim.set_defaults(func=cmd_simulate_webhook)

    # listen
    p_listen = subparsers.add_parser("listen", help="Run the webhook HTTP listener")
    p_listen.add_argument("--port", type=int, default=8088, help="Port to listen on")
    p_listen.add_argument("--host", default="0.0.0.0", help="Host interface")
    p_listen.set_defaults(func=cmd_listen)

    # poll-emails
    p_poll = subparsers.add_parser("poll-emails", help="Poll robie@streetsmart.insurance for incoming CSR call requests")
    p_poll.add_argument("--dry-run", action="store_true", help="Process emails and acknowledge, but simulate the voice call")
    p_poll.set_defaults(func=cmd_poll_emails)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
