"""Database & State Integrity Validator for StreetSmart Insurance Renewal System.

Executes pre-flight and post-flight health assertions across:
1. SQLite PRAGMA integrity & foreign keys
2. Policy state invariant compliance (premiums, file paths, statuses)
3. Policy number alias relational integrity
4. Duplicate / conflicting active term detection
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config import BASE_DIR
from src.database.session import SessionLocal, init_db
from src.database.models import PolicyRenewal, PolicyNumberAlias, AuditNoteLog, RenewalStatus

logger = logging.getLogger("integrity_check")


class DatabaseIntegrityValidator:
    """Performs deep verification of renewals.db health and business state invariants."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or (BASE_DIR / "data" / "renewals.db")

    def run_all_checks(self) -> Tuple[bool, Dict[str, Any]]:
        """Runs complete verification suite. Returns (passed: bool, report: dict)."""
        init_db()
        db = SessionLocal()
        report: Dict[str, Any] = {
            "sqlite_integrity": False,
            "foreign_keys_valid": False,
            "policy_count": 0,
            "alias_count": 0,
            "invariants_passed": True,
            "violations": [],
            "warnings": [],
            "stats": {}
        }

        try:
            # 1. SQLite PRAGMA integrity check
            pragma_result = db.execute(text("PRAGMA integrity_check")).fetchall()
            report["sqlite_integrity"] = len(pragma_result) == 1 and pragma_result[0][0] == "ok"
            if not report["sqlite_integrity"]:
                report["violations"].append(f"PRAGMA integrity_check failed: {pragma_result}")

            # 2. Foreign key check
            fk_result = db.execute(text("PRAGMA foreign_key_check")).fetchall()
            report["foreign_keys_valid"] = len(fk_result) == 0
            if not report["foreign_keys_valid"]:
                report["violations"].append(f"Foreign key violations found: {fk_result}")

            # 3. Policy records audit
            policies = db.query(PolicyRenewal).all()
            report["policy_count"] = len(policies)

            aliases = db.query(PolicyNumberAlias).all()
            report["alias_count"] = len(aliases)

            status_counts: Dict[str, int] = {}
            seen_combos = set()

            for p in policies:
                st = p.status.value if hasattr(p.status, "value") else str(p.status)
                status_counts[st] = status_counts.get(st, 0) + 1

                # Invariant: Must have policy_number and applicant_id
                if not p.policy_number:
                    report["violations"].append(f"Policy ID {p.id} is missing policy_number")
                if not p.applicant_id:
                    report["warnings"].append(f"Policy ID {p.id} ({p.policy_number}) is missing applicant_id")

                # Invariant: RENEWAL_OFFER_RECEIVED must have premium > 0
                if p.status == RenewalStatus.RENEWAL_OFFER_RECEIVED:
                    if not p.renewal_premium or p.renewal_premium <= 0:
                        report["violations"].append(
                            f"Policy ID {p.id} ({p.policy_number}) has status RENEWAL_OFFER_RECEIVED but renewal_premium is {p.renewal_premium}"
                        )
                    # Check associated documents
                    if p.documents:
                        for doc in p.documents:
                            doc_file = Path(doc.file_path)
                            if not doc_file.is_absolute():
                                doc_file = BASE_DIR / doc_file
                            if not doc_file.exists():
                                report["warnings"].append(
                                    f"Policy ID {p.id} ({p.policy_number}) document file missing on disk: {doc.file_path}"
                                )
                    else:
                        report["warnings"].append(
                            f"Policy ID {p.id} ({p.policy_number}) has status RENEWAL_OFFER_RECEIVED but no DocumentRecord attached"
                        )

                # Invariant: Duplicate active renewal terms detection
                if p.status not in [
                    RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT,
                    RenewalStatus.EXCLUDED_TEST_ACCOUNT,
                    RenewalStatus.EXCLUDED_CANCELLATION_PENDING
                ]:
                    combo_key = (str(p.applicant_id), str(p.line_of_business).upper(), str(p.expiration_date))
                    if p.applicant_id and combo_key in seen_combos:
                        report["warnings"].append(
                            f"Potential duplicate active policy row for applicant {p.applicant_id}, LOB {p.line_of_business}, exp {p.expiration_date}"
                        )
                    seen_combos.add(combo_key)

            # 4. Alias integrity
            for a in aliases:
                if not a.policy_id:
                    report["violations"].append(f"Alias ID {a.id} has no policy_id")
                elif a.policy:
                    if a.alias_number.strip().lower() == a.policy.policy_number.strip().lower():
                        report["violations"].append(
                            f"Alias ID {a.id} ({a.alias_number}) is identical to canonical policy number {a.policy.policy_number}"
                        )

            report["stats"] = status_counts
            report["invariants_passed"] = len(report["violations"]) == 0
            all_passed = report["sqlite_integrity"] and report["foreign_keys_valid"] and report["invariants_passed"]

            return all_passed, report

        finally:
            db.close()


def print_integrity_report(passed: bool, report: Dict[str, Any]) -> None:
    """Formats and prints summary to stdout."""
    status_icon = "✅" if passed else "❌"
    print(f"\n{status_icon} [Database State & Integrity Report] Status: {'PASSED' if passed else 'FAILED'}")
    print(f"  • SQLite PRAGMA Integrity : {'OK' if report['sqlite_integrity'] else 'FAILED'}")
    print(f"  • Foreign Key Invariants  : {'OK' if report['foreign_keys_valid'] else 'VIOLATIONS'}")
    print(f"  • Total Policies Audited  : {report['policy_count']}")
    print(f"  • Active Aliases Tracked  : {report['alias_count']}")
    print("  • Status Breakdown:")
    for st, cnt in report["stats"].items():
        print(f"      - {st}: {cnt}")

    if report["violations"]:
        print(f"\n  🚨 Critical Violations ({len(report['violations'])}):")
        for v in report["violations"]:
            print(f"      * {v}")

    if report["warnings"]:
        print(f"\n  ⚠️ Warnings ({len(report['warnings'])}):")
        for w in report["warnings"][:10]:
            print(f"      * {w}")
        if len(report["warnings"]) > 10:
            print(f"      * ... and {len(report['warnings']) - 10} more warnings")
    print()


def main():
    validator = DatabaseIntegrityValidator()
    passed, report = validator.run_all_checks()
    print_integrity_report(passed, report)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
