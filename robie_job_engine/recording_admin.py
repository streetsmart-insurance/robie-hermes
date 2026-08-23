from __future__ import annotations

import argparse
import json

from .recording import RecordingStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Review ROBIE diagnostic recordings")
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    reference = commands.add_parser("approve-reference")
    reference.add_argument("recording_id")
    reference.add_argument("--reviewer", required=True)
    reference.add_argument("--notes", required=True)
    reference.add_argument("--redacted", action="store_true", required=True)
    training = commands.add_parser("approve-training")
    training.add_argument("recording_id")
    training.add_argument("--reviewer", required=True)
    commands.add_parser("manifest")
    args = parser.parse_args()
    store = RecordingStore(args.db)
    if args.command == "approve-reference":
        result = store.approve_reference(
            args.recording_id, approved_by=args.reviewer,
            notes=args.notes, redacted=args.redacted,
        )
    elif args.command == "approve-training":
        result = store.approve_training(args.recording_id, approved_by=args.reviewer)
    else:
        result = store.reference_manifest()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
