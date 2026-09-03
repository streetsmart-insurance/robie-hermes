"""Daily Master Orchestrator for Manual Renewal Automation."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.config import settings
from src.database.session import SessionLocal, init_db
from src.database.models import (
    PolicyRenewal, OutreachThread, AuditNoteLog, DocumentRecord,
    RenewalStatus, ThreadStatus, ActionType
)
from src.intake.report_ingestor import ReportIngestor
from src.portals.carrier_agents import get_carrier_crawler
from src.email_outreach.thread_tracker import OutreachCadenceManager
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.ezlynx.api_client import EZLynxApiClient
from src.ezlynx.report_downloader import EZLynxReportDownloader
from src.extractor.quote_parser import QuoteDocumentParser

logger = logging.getLogger("daily_orchestrator")
console = Console()

class DailyRenewalOrchestrator:
    """Executes the daily automated renewal lifecycle across intake, portals, email, and EZLynx."""

    def __init__(self):
        init_db()
        self.ingestor = ReportIngestor()
        self.ezlynx = EZLynxApiClient()
        self.downloader = EZLynxReportDownloader()
        self.gmail = GmailRenewalClient()
        self.classifier = UnderwriterIntentClassifier()
        self.cadence_mgr = OutreachCadenceManager(
            gmail_client=self.gmail,
            ezlynx_api=self.ezlynx,
            classifier=self.classifier
        )
        self.quote_parser = QuoteDocumentParser()

    async def run_daily_cycle(self, reference_date: Optional[date] = None) -> Dict[str, Any]:
        """Executes the full end-to-end daily renewal automation run."""
        ref_date = reference_date or date.today()
        logger.info(f"=== Starting Daily Renewal Cycle for Date: {ref_date} ===")

        # 0. If Robie's EZLynx credentials are provided, auto-export report 28 headlessly
        if settings.ezlynx_username and settings.ezlynx_password:
            try:
                await self.downloader.download_policy_expiration_report(ref_date, days_ahead=settings.renewal_window_max_days)
            except Exception as e:
                logger.warning(f"Could not auto-download EZLynx report via UI: {e}. Proceeding with existing reports.")

        # Ensure sample input report exists if directory is empty
        self.ingestor.generate_sample_report_if_empty(ref_date)

        results = {
            "run_date": str(ref_date),
            "new_policies_ingested": 0,
            "total_in_window": 0,
            "portal_quotes_downloaded": 0,
            "initial_emails_sent": 0,
            "followups_sent": 0,
            "inbox_replies_processed": 0,
            "quotes_ready_for_review": 0,
            "notes_logged_to_ezlynx": 0
        }

        db = SessionLocal()
        try:
            # Step 1: Intake & 30-45 Day Scan
            new_count, in_win = self.ingestor.sync_to_database(db, ref_date)
            results["new_policies_ingested"] = new_count
            results["total_in_window"] = in_win

            # Step 2: Carrier Portal Check
            portal_policies = db.query(PolicyRenewal).filter(
                PolicyRenewal.status == RenewalStatus.PENDING_EVALUATION,
                PolicyRenewal.portal_supported == True
            ).all()

            for pol in portal_policies:
                pol.status = RenewalStatus.CHECKING_PORTAL
                crawler = get_carrier_crawler(pol.carrier_name, pol.portal_url)
                search_res = await crawler.check_renewal_quote(
                    policy_number=pol.policy_number,
                    insured_name=pol.insured_name,
                    download_dir=settings.downloads_path
                )

                if search_res.success and search_res.renewal_ready and search_res.document_path:
                    # Quote found on portal!
                    parsed_data = self.quote_parser.parse_pdf(search_res.document_path)
                    pol.renewal_premium = parsed_data.renewal_premium or search_res.extracted_premium or pol.expiring_premium
                    if pol.expiring_premium and pol.renewal_premium:
                        pol.premium_change_pct = ((pol.renewal_premium - pol.expiring_premium) / pol.expiring_premium) * 100

                    pol.status = RenewalStatus.READY_FOR_AGENT_REVIEW
                    results["portal_quotes_downloaded"] += 1
                    results["quotes_ready_for_review"] += 1

                    # Record document
                    doc = DocumentRecord(
                        policy_id=pol.id,
                        file_name=search_res.document_path.name,
                        file_path=str(search_res.document_path),
                        source="CARRIER_PORTAL",
                        extracted_premium=pol.renewal_premium,
                        extracted_summary=f"Premium: ${pol.renewal_premium:,.2f}" if pol.renewal_premium else ""
                    )
                    db.add(doc)

                    # Post EZLynx note under Discussion Title
                    portal_note = EZLynxNoteBuilder.format_portal_check_note(
                        policy=pol,
                        success=True,
                        details=search_res.status_message,
                        downloaded_file=search_res.document_path.name
                    )
                    db.add(AuditNoteLog(
                        policy_id=pol.id,
                        applicant_id=pol.applicant_id,
                        discussion_title=pol.discussion_title,
                        action_type=ActionType.PORTAL_CHECK,
                        note_text=portal_note
                    ))
                    self.ezlynx.add_note_to_discussion(
                        applicant_id=pol.applicant_id,
                        discussion_title=pol.discussion_title,
                        note_text=portal_note,
                        policy_number=pol.policy_number
                    )

                    # Upload doc to EZLynx & create review task
                    self.ezlynx.upload_document(
                        applicant_id=pol.applicant_id,
                        file_path=search_res.document_path,
                        folder_name="Renewals"
                    )
                    task_note = EZLynxNoteBuilder.format_quote_ready_task_note(
                        policy=pol,
                        renewal_premium=pol.renewal_premium,
                        expiring_premium=pol.expiring_premium,
                        document_path=search_res.document_path.name
                    )
                    self.ezlynx.create_user_task(
                        applicant_id=pol.applicant_id,
                        title=f"Review Renewal Quote: {pol.insured_name} ({pol.carrier_name})",
                        description=task_note,
                        assigned_user=pol.assigned_agent
                    )
                else:
                    # Portal not ready or unsupported -> fall back to underwriter outreach
                    pol.status = RenewalStatus.OUTREACH_PENDING
                    portal_note = EZLynxNoteBuilder.format_portal_check_note(
                        policy=pol,
                        success=False,
                        details=search_res.status_message + " (Switching to Underwriter Email Outreach)"
                    )
                    db.add(AuditNoteLog(
                        policy_id=pol.id,
                        applicant_id=pol.applicant_id,
                        discussion_title=pol.discussion_title,
                        action_type=ActionType.PORTAL_CHECK,
                        note_text=portal_note
                    ))
                    self.ezlynx.add_note_to_discussion(
                        applicant_id=pol.applicant_id,
                        discussion_title=pol.discussion_title,
                        note_text=portal_note,
                        policy_number=pol.policy_number
                    )

            db.commit()

            # Step 3: Underwriter Outreach (Initial Emails)
            init_sent = self.cadence_mgr.process_pending_outreach(db, ref_date)
            results["initial_emails_sent"] = init_sent

            # Step 4: 5-7 Day Follow-Up Cadence
            followups = self.cadence_mgr.process_due_followups(db, ref_date)
            results["followups_sent"] = followups

            # Step 5: Gmail Inbox Poller & Reply Processing
            replies = self.cadence_mgr.process_incoming_inbox_replies(db)
            results["inbox_replies_processed"] = replies

            # Step 6: 20-25 Day CSR Escalation Check
            # If no renewal quote received by day 20-25, assign task to CSR in existing discussion title
            pending_escalation = db.query(PolicyRenewal).filter(
                PolicyRenewal.status.in_([
                    RenewalStatus.PENDING_EVALUATION,
                    RenewalStatus.OUTREACH_PENDING,
                    RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
                    RenewalStatus.FOLLOWUP_SENT,
                    RenewalStatus.INFO_REQUESTED
                ]),
                PolicyRenewal.expiration_date <= (ref_date + timedelta(days=25))
            ).all()

            escalated_count = 0
            for pol in pending_escalation:
                days_rem = (pol.expiration_date - ref_date).days
                pol.status = RenewalStatus.ESCALATED_MANUAL
                escalated_count += 1

                note_text = EZLynxNoteBuilder.format_csr_escalation_note(
                    policy=pol,
                    days_to_expiration=days_rem,
                    reason=f"No renewal quote received by {days_rem} days prior to expiration."
                )

                db.add(AuditNoteLog(
                    policy_id=pol.id,
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=note_text
                ))

                self.ezlynx.add_note_to_discussion(
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    note_text=note_text,
                    policy_number=pol.policy_number
                )

                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"URGENT Review Renewal ({days_rem}d to Exp): {pol.insured_name} ({pol.carrier_name})",
                    description=f"Policy expires on {pol.expiration_date} ({days_rem} days remaining). No renewal proposal has been received from carrier. Please review existing discussion '{pol.discussion_title}' and contact underwriter directly.",
                    assigned_user=pol.assigned_agent
                )

            results["escalated_to_csr"] = escalated_count
            db.commit()

            # Count total notes logged
            total_notes = db.query(AuditNoteLog).count()
            results["notes_logged_to_ezlynx"] = total_notes

        finally:
            db.close()

        return results

    def print_summary_dashboard(self, results: Dict[str, Any]):
        """Renders rich terminal UI dashboard for daily run status."""
        panel_content = (
            f"[bold green]Daily Renewal Cycle Executed Successfully[/bold green]\n"
            f"[cyan]Run Date:[/cyan] {results['run_date']} | [cyan]Renewal Window:[/cyan] {settings.renewal_window_min_days}-{settings.renewal_window_max_days} Days Out\n"
            f"[cyan]Follow-up Cadence:[/cyan] {settings.followup_cadence_min_days}-{settings.followup_cadence_max_days} Days"
        )
        console.print(Panel(panel_content, title="🚀 Insurance Renewal Automation", expand=False))

        table = Table(title="Daily Execution Metrics", show_header=True, header_style="bold magenta")
        table.add_column("Pipeline Stage", style="cyan")
        table.add_column("Count / Status", justify="right", style="bold green")

        table.add_row("Active Policies in 30-45d Window", str(results["total_in_window"]))
        table.add_row("New Expiring Policies Ingested", str(results["new_policies_ingested"]))
        table.add_row("Portal Quotes Retrieved", str(results["portal_quotes_downloaded"]))
        table.add_row("Initial Underwriter Emails Sent", str(results["initial_emails_sent"]))
        table.add_row("5-7d Cadence Follow-ups Sent", str(results["followups_sent"]))
        table.add_row("Inbox Replies & Quotes Processed", str(results["inbox_replies_processed"]))
        table.add_row("Quotes Ready for AM Review", str(results["quotes_ready_for_review"]))
        table.add_row("Escalated to CSR (20-25d No Quote)", str(results.get("escalated_to_csr", 0)))
        table.add_row("EZLynx Discussion Notes Logged", str(results["notes_logged_to_ezlynx"]))

        console.print(table)

        # Print current active policy table
        db = SessionLocal()
        try:
            policies = db.query(PolicyRenewal).all()
            if policies:
                pol_table = Table(title="Policy Renewal Registry & Statuses", show_header=True, header_style="bold blue")
                pol_table.add_column("ID", justify="center")
                pol_table.add_column("Policy #", style="bold")
                pol_table.add_column("Insured Name")
                pol_table.add_column("Carrier")
                pol_table.add_column("Exp Date", justify="center")
                pol_table.add_column("Discussion Title", style="italic")
                pol_table.add_column("Status", style="bold yellow")

                for p in policies:
                    pol_table.add_row(
                        str(p.id),
                        p.policy_number,
                        p.insured_name,
                        p.carrier_name,
                        str(p.expiration_date),
                        p.discussion_title,
                        str(p.status.value if hasattr(p.status, "value") else p.status)
                    )
                console.print(pol_table)
        finally:
            db.close()
