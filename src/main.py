"""Main CLI Entrypoint for Daily Insurance Renewal Automation."""

import sys
import argparse
import asyncio
import logging
from datetime import datetime, date, timedelta
from rich.console import Console

from pathlib import Path
from src.config import settings
from src.scheduler.daily_runner import DailyRenewalOrchestrator
from src.email_outreach.auth_setup import interactive_authorize_inbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("renewal_main")
console = Console()

def parse_args():
    parser = argparse.ArgumentParser(description="Autonomous Daily Manual Renewal Engine for EZLynx")
    parser.add_argument("--run-today", action="store_true", help="Execute daily renewal check for today")
    parser.add_argument("--date", type=str, default=None, help="Execute for specific date (YYYY-MM-DD)")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode (executes daily at 07:00 AM)")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending live emails or API calls")
    parser.add_argument("--auth-robie", action="store_true", help="Authorize Robie inbox (robie@streetsmart.insurance) for outreach")
    parser.add_argument("--auth-hello", action="store_true", help="Authorize Hello inbox (hello@streetsmart.insurance) for carrier quote polling")
    parser.add_argument("--auth-gmail", action="store_true", help="Authorize both Robie and Hello Gmail inboxes")
    parser.add_argument("--list-carriers", action="store_true", help="Display Carrier Fulfillment Directory & Routing Matrix")
    parser.add_argument("--update-carrier", type=str, default=None, help="Carrier Name to update in directory")
    parser.add_argument("--channel", type=str, default="EMAIL", choices=["PORTAL", "EMAIL", "EMAIL_ASK_PORTAL"], help="Channel type")
    parser.add_argument("--url", type=str, default=None, help="Portal URL")
    parser.add_argument("--set-login", action="store_true", help="Save credentials for a service/carrier to macOS Keychain")
    parser.add_argument("--get-login", action="store_true", help="Retrieve credentials for a service/carrier")
    parser.add_argument("--test-secrets", action="store_true", help="Test and list discovered credentials across Keychain and GCP")
    parser.add_argument("--test-carrier", type=str, default=None, help="Run test demonstration for a carrier (e.g. 'Trinity Underwriters')")
    parser.add_argument("--test-all", action="store_true", help="Run full test demonstration including quote receipt simulation and CSR escalation")
    parser.add_argument("--dispatch-live", action="store_true", help="Send live emails to carrier underwriter, CC CSR & Jake, pend policies")
    parser.add_argument("--service", type=str, default=None, help="Service/Carrier name (e.g. 'EZLynx', 'Coterie', 'The Hartford')")
    parser.add_argument("--username", type=str, default=None, help="Username / Email for login")
    parser.add_argument("--password", type=str, default=None, help="Password for login")
    return parser.parse_args()

async def async_main():
    args = parse_args()
    from src.portals.carrier_routing import CarrierRoutingMatrix
    from src.security.secrets_manager import secrets_mgr
    from rich.table import Table

    if args.set_login:
        if not args.service or not args.username or not args.password:
            console.print("[bold red]Error: --set-login requires --service, --username, and --password[/bold red]")
            return
        success = secrets_mgr.save_to_macos_keychain(args.service, args.username, args.password)
        if success:
            console.print(f"[bold green]✅ Successfully saved login for '{args.service}' ({args.username}) in macOS Keychain![/bold green]")
        else:
            console.print(f"[bold red]❌ Failed to save login for '{args.service}' in macOS Keychain.[/bold red]")
        return

    if args.get_login:
        if not args.service:
            console.print("[bold red]Error: --get-login requires --service[/bold red]")
            return
        pair = secrets_mgr.get_login_pair(args.service)
        masked_pwd = ("*" * len(pair['password'])) if pair['password'] else "NOT FOUND"
        console.print(f"[bold cyan]Credentials for '{args.service}':[/bold cyan]")
        console.print(f"• [yellow]Username:[/yellow] {pair['username'] or 'NOT FOUND'}")
        console.print(f"• [yellow]Password:[/yellow] {masked_pwd}")
        return

    if args.test_secrets:
        services_to_test = ["EZLynx", "Coterie", "The Hartford", "Travelers", "Cover Whale", "Markel", "Geico", "TAPCO"]
        table = Table(title="🔐 Discovered Secrets & Portal Credentials", show_header=True, header_style="bold magenta")
        table.add_column("Service / Portal", style="bold cyan")
        table.add_column("Username / Account", style="green")
        table.add_column("Password Status", style="bold yellow")

        for svc in services_to_test:
            pair = secrets_mgr.get_login_pair(svc)
            has_pwd = "✅ Present" if pair["password"] else "❌ Missing"
            table.add_row(svc, pair["username"] or "N/A", has_pwd)

        console.print(table)
        return

    if args.test_carrier:
        from scripts.test_trinity_renewals import (
            get_carrier_renewals, print_policies_table, preview_outreach_emails,
            simulate_quote_receipt_and_ingestion, simulate_csr_escalation, dispatch_live_outreach
        )
        from src.database.session import SessionLocal, init_db

        init_db()
        db = SessionLocal()
        carrier_name = args.test_carrier
        policies = get_carrier_renewals(db, carrier_name=carrier_name)
        if not policies:
            console.print(f"[bold red]No policies found for carrier matching '{carrier_name}'.[/bold red]")
            return

        if args.dispatch_live:
            dispatch_live_outreach(db, carrier_name=carrier_name)
            db.close()
            return

        print_policies_table(policies, carrier_title=carrier_name)
        preview_outreach_emails(policies)

        if args.test_all:
            first_pol = policies[0]
            simulate_quote_receipt_and_ingestion(db, policy_id=first_pol.id)
            if len(policies) > 1:
                simulate_csr_escalation(db, policy_id=policies[1].id)

        db.close()
        return
        from src.database.session import SessionLocal
        from src.database.models import PolicyRenewal, RenewalStatus, DocumentRecord, AuditNoteLog, ActionType
        from src.portals.carrier_agents import get_carrier_crawler
        from src.ezlynx.note_builder import EZLynxNoteBuilder
        from src.ezlynx.api_client import EZLynxApiClient
        from src.extractor.quote_parser import QuoteDocumentParser
        from src.config import settings

        db = SessionLocal()
        ezlynx = EZLynxApiClient()
        parser = QuoteDocumentParser()

        target_carriers = ["Coterie", "The Hartford", "TAPCO Underwriters Inc.", "TAPCO"]
        policies = db.query(PolicyRenewal).filter(
            PolicyRenewal.carrier_name.in_(target_carriers),
            PolicyRenewal.policy_number != "13WECAN7A7K"
        ).all()

        console.print(f"[bold cyan]🔍 Executing Portal Automated Crawls for {len(policies)} policies (Coterie, The Hartford, TAPCO)...[/bold cyan]\n")
        table = Table(title="🌐 Carrier Portal Execution Results", show_header=True, header_style="bold magenta")
        table.add_column("Carrier", style="bold cyan")
        table.add_column("Policy #", style="green")
        table.add_column("Insured Name", style="white")
        table.add_column("Status / Outcome", style="bold yellow")

        for pol in policies:
            crawler = get_carrier_crawler(pol.carrier_name, pol.portal_url or "https://agent.portal")
            res = await crawler.check_renewal_quote(
                policy_number=pol.policy_number,
                insured_name=pol.insured_name,
                download_dir=settings.downloads_path
            )

            status_desc = "✅ Quote Downloaded" if res.renewal_ready else "⏳ Checked - Offer Pending"
            if not res.success:
                status_desc = f"⚠️ {res.status_message}"

            table.add_row(pol.carrier_name, pol.policy_number, pol.insured_name, f"{status_desc}\n[dim]{res.status_message}[/dim]")

            if res.renewal_ready and res.document_path:
                pol.status = RenewalStatus.READY_FOR_AGENT_REVIEW
                pol.renewal_premium = res.extracted_premium or pol.expiring_premium
                
                # Upload doc & log note
                ezlynx.upload_document(pol.applicant_id, res.document_path, "Renewals")
                note_text = EZLynxNoteBuilder.format_portal_check_note(pol, success=True, details=res.status_message, downloaded_file=res.document_path.name)
                ezlynx.add_note_to_discussion(pol.applicant_id, pol.discussion_title, note_text, pol.policy_number)
                ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"Review Renewal Quote: {pol.insured_name} ({pol.carrier_name})",
                    description=f"Renewal quote downloaded from {pol.carrier_name} portal.\nPolicy #{pol.policy_number}\nDocument: {res.document_path.name}",
                    assigned_user=pol.assigned_agent
                )

        db.commit()
        db.close()
        console.print(table)
        return

    if args.list_carriers:
        directory = CarrierRoutingMatrix.load_directory()
        table = Table(title="🏢 Carrier Directory & Renewal Document Routing Matrix", show_header=True, header_style="bold magenta")
        table.add_column("Carrier / MGA Name", style="bold cyan")
        table.add_column("Retrieval Channel", justify="center", style="bold yellow")
        table.add_column("Portal URL / Email", style="green")
        table.add_column("Operational Notes", style="italic")

        for carrier, conf in sorted(directory.items()):
            ch = conf.get("channel", "EMAIL")
            dest = conf.get("portal_url") if ch == "PORTAL" else conf.get("underwriter_email", "N/A")
            table.add_row(carrier, ch, dest or "N/A", conf.get("notes", ""))

        console.print(table)
        return

    if args.update_carrier:
        updated = CarrierRoutingMatrix.update_carrier(
            carrier_name=args.update_carrier,
            channel=args.channel,
            portal_url=args.url,
            underwriter_email=args.email,
            notes=args.notes
        )
        console.print(f"[bold green]✅ Updated Carrier Directory for '{args.update_carrier}':[/bold green] {updated}")
        return

    if args.auth_robie or (args.auth_gmail and not args.auth_hello):
        console.print("[bold yellow]Starting Gmail API OAuth2 Authentication for Robie (robie@streetsmart.insurance)...[/bold yellow]")
        creds = interactive_authorize_inbox(Path(settings.gmail_token_file), "Robie (robie@streetsmart.insurance)")
        if creds and creds.valid:
            console.print("[bold green]✅ Robie Outreach Inbox Authenticated! Token saved.[/bold green]")

    if args.auth_hello or args.auth_gmail:
        console.print("[bold yellow]Starting Gmail API OAuth2 Authentication for Hello (hello@streetsmart.insurance)...[/bold yellow]")
        creds = interactive_authorize_inbox(Path(settings.gmail_hello_token_file), "Hello (hello@streetsmart.insurance)")
        if creds and creds.valid:
            console.print("[bold green]✅ Hello Polling Inbox Authenticated! Token saved.[/bold green]")

    if args.auth_robie or args.auth_hello or args.auth_gmail:
        return

    orchestrator = DailyRenewalOrchestrator()

    ref_date = date.today()
    if args.date:
        ref_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    if args.daemon:
        console.print("[bold green]Starting Renewal Automation in Daily Daemon Mode...[/bold green]")
        while True:
            try:
                results = await orchestrator.run_daily_cycle(date.today())
                orchestrator.print_summary_dashboard(results)
            except Exception as e:
                logger.error(f"Error in daemon cycle: {e}")
            # Sleep 24 hours (86400s)
            await asyncio.sleep(86400)
    else:
        results = await orchestrator.run_daily_cycle(ref_date)
        orchestrator.print_summary_dashboard(results)

def cli_main():
    asyncio.run(async_main())

if __name__ == "__main__":
    cli_main()
