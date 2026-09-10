"""Unified Daily Pipeline Runner & Handoff Reporter for StreetSmart Insurance.

Executes:
1. Scheduled Intake Report Ingestion from robie@ inbox
2. 45-25 Day Renewal Processing Cycle (Portal Crawls, Outreach, EZLynx Notes)
3. Dynamic DB Query & Markdown Handoff Report Generation
4. Email Delivery to Leadership (carlo@, jake@, gabrielac@, sandy@, ashley@)
"""

import os
import sys
import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.database.session import SessionLocal, init_db
from src.database.models import PolicyRenewal, RenewalStatus, AuditNoteLog
from src.scheduler.daily_runner import DailyRenewalOrchestrator
from src.intake.email_ingest import ingest_reports_from_email
from src.database.integrity_check import DatabaseIntegrityValidator
from src.reporting.daily_handoff import DailyHandoffReporter
from src.reporting.email_handoff import send_daily_handoff_email, HANDOFF_RECIPIENT_LIST

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("daily_pipeline")


def build_handoff_data_from_db(target_date: date) -> Dict[str, Any]:
    """Queries renewals.db to build the structured dataset for the Daily Handoff Report."""
    init_db()
    db = SessionLocal()
    try:
        all_policies = db.query(PolicyRenewal).all()

        active_processed: List[Dict[str, Any]] = []
        excluded_inactive: List[Dict[str, Any]] = []
        upcoming_accounts: List[Dict[str, Any]] = []
        portal_logins_needed: List[Dict[str, Any]] = []

        window_start = target_date + timedelta(days=20)
        window_end = target_date + timedelta(days=45)

        for p in all_policies:
            status_val = p.status.value if hasattr(p.status, "value") else str(p.status)

            # Check if excluded/inactive
            if p.status == RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT:
                excluded_inactive.append({
                    "applicant_id": p.applicant_id,
                    "insured_name": p.insured_name,
                    "policy_number": p.policy_number,
                    "carrier_name": p.carrier_name,
                    "cancellation_date": str(p.expiration_date),
                    "reason": "Policy cancelled mid-term / inactive in EZLynx"
                })
                continue

            # Check if active processed
            if p.status in [
                RenewalStatus.COMPLETED,
                RenewalStatus.PORTAL_QUOTE_FOUND,
                RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
                RenewalStatus.FOLLOWUP_SENT,
                RenewalStatus.ESCALATED_MANUAL,
                RenewalStatus.READY_FOR_AGENT_REVIEW,
                RenewalStatus.QUOTE_RECEIVED,
                RenewalStatus.REPLY_RECEIVED,
                RenewalStatus.UPLOADED_TO_EZLYNX
            ]:
                latest_note = db.query(AuditNoteLog).filter(
                    AuditNoteLog.policy_id == p.id
                ).order_by(AuditNoteLog.created_at.desc()).first()

                tracking_tag = f"RENEWAL-REQ-{p.applicant_id or p.id}"

                active_processed.append({
                    "applicant_id": p.applicant_id or "",
                    "insured_name": p.insured_name or "Unknown",
                    "policy_number": p.policy_number or "N/A",
                    "carrier_name": p.carrier_name or "N/A",
                    "line_of_business": p.line_of_business or "Commercial",
                    "expiration_date": str(p.expiration_date) if p.expiration_date else "N/A",
                    "expiring_premium": float(p.expiring_premium or 0.0),
                    "renewal_premium": float(p.renewal_premium or 0.0) if p.renewal_premium else None,
                    "assigned_csr": p.assigned_agent or "Sandy Santana",
                    "status": status_val,
                    "discussion_title": p.discussion_title or "Renewal Discussion",
                    "tracking_ref": tracking_tag,
                    "underwriter_email": p.underwriter_email or "Underwriter Portal / Outreach",
                    "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
                    "screenshot_path": getattr(p, "audit_screenshot_path", "") or ""
                })

            # Check upcoming queue (expiring in 35-50 days)
            if p.expiration_date and (p.expiration_date > window_end or p.status == RenewalStatus.PENDING_EVALUATION):
                upcoming_accounts.append({
                    "applicant_id": p.applicant_id or "",
                    "insured_name": p.insured_name or "Unknown",
                    "policy_number": p.policy_number or "N/A",
                    "carrier_name": p.carrier_name or "N/A",
                    "expiration_date": str(p.expiration_date) if p.expiration_date else "N/A",
                    "expiring_premium": float(p.expiring_premium or 0.0),
                    "days_until_expiration": (p.expiration_date - target_date).days if p.expiration_date else 0,
                    "scheduled_outreach_date": str(p.expiration_date - timedelta(days=45)) if p.expiration_date else "N/A"
                })

        # Rolling 50-day pipeline across all active manual policies
        rolling_pipeline = []
        rolling_query = db.query(PolicyRenewal).filter(
            PolicyRenewal.status != RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT,
            PolicyRenewal.status != RenewalStatus.EXCLUDED_TEST_ACCOUNT,
            (PolicyRenewal.source == "Manual") | (PolicyRenewal.source == None),
            PolicyRenewal.expiration_date >= target_date,
            PolicyRenewal.expiration_date <= (target_date + timedelta(days=50))
        ).order_by(PolicyRenewal.expiration_date.asc()).all()

        for p in rolling_query:
            days_to_exp = (p.expiration_date - target_date).days if p.expiration_date else None
            doc_info = "Awaiting terms"
            if p.documents:
                latest_doc = p.documents[-1]
                doc_folder = "Renewal Offers"
                if "loss" in latest_doc.file_name.lower():
                    doc_folder = "Loss Runs"
                elif "non" in latest_doc.file_name.lower() or "cancel" in latest_doc.file_name.lower():
                    doc_folder = "Cancellations"
                doc_info = f"`{latest_doc.file_name}` ({doc_folder})"

            if p.status == RenewalStatus.READY_FOR_AGENT_REVIEW:
                next_act = "AM to review renewal quote & present to client"
            elif p.status == RenewalStatus.ESCALATED_MANUAL:
                next_act = "URGENT: AM direct outreach (<=25d to exp)"
            elif p.status == RenewalStatus.NON_RENEWAL_DECLINED:
                next_act = "CRITICAL: Re-market to secondary carriers"
            elif p.status == RenewalStatus.INFO_REQUESTED:
                next_act = "AM to provide requested underwriting items"
            elif p.status == RenewalStatus.FOLLOWUP_SENT:
                next_act = "Cadence follow-up sent; awaiting reply"
            elif p.status == RenewalStatus.EMAIL_SENT_AWAITING_REPLY:
                next_act = "Initial outreach sent; awaiting reply"
            elif p.status == RenewalStatus.PENDING_EVALUATION:
                next_act = "Queued for portal crawl / outreach"
            else:
                next_act = "In automated cadence"

            rolling_pipeline.append({
                "applicant_id": p.applicant_id or "",
                "insured_name": p.insured_name or "Unknown",
                "policy_number": p.policy_number or "N/A",
                "carrier_name": p.carrier_name or "N/A",
                "line_of_business": p.line_of_business or "Commercial",
                "expiration_date": str(p.expiration_date) if p.expiration_date else "N/A",
                "days_to_exp": days_to_exp,
                "expiring_premium": float(p.expiring_premium or 0.0),
                "renewal_premium": float(p.renewal_premium) if p.renewal_premium else None,
                "delta_pct": float(p.premium_change_pct) if p.premium_change_pct else None,
                "channel": "PORTAL" if p.portal_supported else "EMAIL",
                "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                "doc_info": doc_info,
                "assigned_csr": p.assigned_agent or "Unassigned",
                "next_action": next_act
            })

        return {
            "active_processed": active_processed,
            "excluded_inactive": excluded_inactive,
            "upcoming_accounts": upcoming_accounts,
            "portal_logins_needed": portal_logins_needed,
            "rolling_pipeline": rolling_pipeline
        }
    finally:
        db.close()


async def run_pipeline(target_date: date, skip_email: bool = False, skip_ingest: bool = False) -> str:
    """Executes the full automated cycle and sends the daily handoff report."""
    logger.info(f"=== Starting StreetSmart Renewal Pipeline for {target_date} ===")

    # 1. Ingest scheduled reports from Robie's inbox
    if not skip_ingest:
        try:
            logger.info("Checking for scheduled intake reports in robie@ inbox...")
            ingest_res = ingest_reports_from_email()
            logger.info(f"Report ingestion result: {ingest_res}")
        except Exception as e:
            logger.warning(f"Email intake encountered an error: {e}. Continuing with local queue.")

    # 2. Run daily renewal cycle
    orchestrator = DailyRenewalOrchestrator()
    results = await orchestrator.run_daily_cycle(reference_date=target_date, send_report_email=not skip_email)
    logger.info(f"Pipeline execution metrics: {results}")

    # 3. Build structured report data from DB
    handoff_data = build_handoff_data_from_db(target_date)

    # 4. Run database state & integrity check
    try:
        validator = DatabaseIntegrityValidator()
        passed, health_report = validator.run_all_checks()
        system_health = {**health_report, "passed": passed}
        logger.info(f"Database integrity check result: passed={passed}, policies={health_report.get('policy_count')}")
    except Exception as e:
        logger.warning(f"Integrity check encountered error: {e}")
        system_health = {"passed": False, "policy_count": 0, "alias_count": 0, "violations": [str(e)]}

    # 5. Generate Markdown report
    os.makedirs(PROJECT_ROOT / "reports", exist_ok=True)
    report_file = PROJECT_ROOT / "reports" / f"daily_handoff_{target_date.strftime('%Y_%m_%d')}.md"

    report_md = DailyHandoffReporter.generate_report_markdown(
        report_date=target_date,
        active_processed=handoff_data["active_processed"],
        excluded_inactive=handoff_data["excluded_inactive"],
        portal_logins_needed=handoff_data["portal_logins_needed"],
        upcoming_accounts=handoff_data["upcoming_accounts"],
        rolling_pipeline=handoff_data["rolling_pipeline"],
        output_filepath=str(report_file),
        system_health=system_health
    )
    logger.info(f"Daily Handoff Report saved to {report_file}")

    # 5. Dispatch email to leadership
    if not skip_email:
        logger.info(f"Sending Daily Handoff Email to leadership: {HANDOFF_RECIPIENT_LIST}")
        try:
            email_res = send_daily_handoff_email(
                report_markdown=report_md,
                report_date=target_date,
                recipients=HANDOFF_RECIPIENT_LIST
            )
            logger.info(f"Handoff email dispatched successfully! Result: {email_res}")
        except Exception as e:
            logger.error(f"Failed to dispatch daily handoff email: {e}", exc_info=True)
    else:
        logger.info("Skipping email dispatch (--skip-email specified).")

    return report_md


def main():
    parser = argparse.ArgumentParser(description="StreetSmart Daily Renewal Pipeline & Handoff Reporter")
    parser.add_argument("--date", type=str, help="Reference execution date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--skip-email", action="store_true", help="Generate report without sending handoff email")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip polling email inbox for scheduled reports")
    args = parser.parse_args()

    run_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()

    import asyncio
    asyncio.run(run_pipeline(
        target_date=run_date,
        skip_email=args.skip_email,
        skip_ingest=args.skip_ingest
    ))


if __name__ == "__main__":
    main()
