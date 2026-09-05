"""Main CLI Entrypoint for Daily Insurance Renewal Automation."""

import sys
import argparse
import asyncio
import time
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
    parser.add_argument("--send-report", action="store_true", help="Email the comprehensive daily handoff report to team leads")
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
    parser.add_argument("--ezlynx-test-auth", action="store_true", help="Test live authentication against EZLynx Classic & OAuth2 APIs")
    parser.add_argument("--ezlynx-search-applicant", type=str, default=None, metavar="QUERY", help="Search applicant by ID, Name, or Policy Number")
    parser.add_argument("--ezlynx-view-sessions", action="store_true", help="View health and status of EZLynx API & browser sessions")
    parser.add_argument("--ezlynx-quote-session", type=str, default=None, metavar="QUOTE_ID", help="View completed quote results for a rating session")
    parser.add_argument(
        "--file-uw-replies",
        action="store_true",
        help="Poll robie@ + hello@ for underwriter replies and file them onto titled EZLynx discussion cards",
    )
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
    if args.ezlynx_test_auth:
        from src.ezlynx.api_client import EZLynxApiClient
        client = EZLynxApiClient()
        console.print("\n[bold cyan]🔐 Testing EZLynx API Live Connectivity...[/bold cyan]\n")

        # 1. Classic API
        t0 = time.time()
        classic_ok = client.authenticate_classic(force_refresh=True)
        classic_latency = int((time.time() - t0) * 1000)

        # 2. Modern OAuth2 Gateway
        t0 = time.time()
        oauth_ok = client.authenticate_oauth(force_refresh=True)
        oauth_latency = int((time.time() - t0) * 1000)

        table = Table(title="EZLynx Dual-Subsystem API Status", show_header=True, header_style="bold magenta")
        table.add_column("Subsystem", style="bold cyan")
        table.add_column("Endpoint", style="white")
        table.add_column("Account / Client", style="yellow")
        table.add_column("Latency", justify="right", style="green")
        table.add_column("Status", justify="center", style="bold")

        table.add_row(
            "Classic Web Services",
            client.services_url,
            client.username,
            f"{classic_latency}ms",
            "[bold green]✅ Authenticated (EZToken)[/bold green]" if classic_ok else "[bold red]❌ Failed[/bold red]"
        )
        table.add_row(
            "Modern OAuth2 Gateway",
            client.connect_token_url,
            client.client_id,
            f"{oauth_latency}ms",
            "[bold green]✅ Authenticated (Bearer)[/bold green]" if oauth_ok else "[bold red]❌ Failed[/bold red]"
        )
        console.print(table)
        if oauth_ok:
            console.print(f"[dim]Authorized Scopes: {', '.join(client._oauth_scopes)}[/dim]\n")
        return

    if args.ezlynx_search_applicant:
        from src.ezlynx.api_client import EZLynxApiClient
        client = EZLynxApiClient()
        query = args.ezlynx_search_applicant
        console.print(f"\n[bold cyan]🔍 Searching EZLynx Applicants for query:[/bold cyan] '[yellow]{query}[/yellow]'...\n")

        matches = client.search_applicants(query)
        if not matches:
            console.print(f"[bold red]No matching applicant found for '{query}'.[/bold red]\n")
            return

        table = Table(title=f"EZLynx Applicant Search Results ({len(matches)} match{'es' if len(matches) > 1 else ''})", show_header=True, header_style="bold magenta")
        table.add_column("Applicant ID", style="bold cyan")
        table.add_column("Name / Business", style="bold white")
        table.add_column("Type", style="yellow")
        table.add_column("Assigned Agent", style="green")
        table.add_column("Email", style="blue")
        table.add_column("Phone", style="magenta")
        table.add_column("Address", style="dim")

        for m in matches:
            addr_obj = m.get("address") or {}
            addr_str = f"{addr_obj.get('AddressLine1', '')}, {addr_obj.get('City', '')} {addr_obj.get('State', '')}".strip(" ,") if isinstance(addr_obj, dict) else str(addr_obj)
            table.add_row(
                str(m.get("applicant_id")),
                str(m.get("name") or "N/A"),
                str(m.get("type") or "N/A"),
                str(m.get("assigned_to") or "N/A"),
                str(m.get("email") or "N/A"),
                str(m.get("phone") or "N/A"),
                addr_str or "N/A"
            )
        console.print(table)

        # If single match or queried by ID, also display active policies!
        target_id = matches[0].get("applicant_id")
        if target_id and str(target_id).isdigit():
            pol_res = client.get_applicant_policies(str(target_id))
            policies = pol_res.get("policies", [])
            if policies:
                pol_table = Table(title=f"📋 Active Policies for Applicant #{target_id} ({len(policies)} policies)", show_header=True, header_style="bold cyan")
                pol_table.add_column("Policy #", style="bold green")
                pol_table.add_column("Line of Business", style="yellow")
                pol_table.add_column("Carrier / Company", style="white")
                pol_table.add_column("Effective", style="dim")
                pol_table.add_column("Expiration", style="bold red")

                for p in policies:
                    eff = str(p.get("EffectiveDate", ""))[:10]
                    exp = str(p.get("ExpirationDate", ""))[:10]
                    pol_table.add_row(
                        str(p.get("PolicyNumber") or "N/A"),
                        str(p.get("LOB") or "N/A"),
                        str(p.get("Company") or p.get("Carrier") or "N/A"),
                        eff,
                        exp
                    )
                console.print(pol_table)
        return

    if args.ezlynx_view_sessions:
        from src.ezlynx.api_client import EZLynxApiClient
        client = EZLynxApiClient()
        console.print("\n[bold cyan]🔍 Checking Health and State of All EZLynx Sessions...[/bold cyan]\n")

        overview = client.get_session_overview()

        # 1. API Sessions Table
        api_table = Table(title="🌐 EZLynx API Subsystem Sessions", show_header=True, header_style="bold magenta")
        api_table.add_column("Session Layer", style="bold cyan")
        api_table.add_column("Configured", style="yellow")
        api_table.add_column("Active / Auth", style="bold green")
        api_table.add_column("Identity / Account", style="white")
        api_table.add_column("Details", style="dim")

        classic = overview["classic_api"]
        api_table.add_row(
            "Classic REST API",
            "✅ Yes" if classic["configured"] else "❌ No",
            "[bold green]✅ Connected[/bold green]" if classic["authenticated"] else "[bold red]❌ Offline[/bold red]",
            classic["username"],
            "Token cached in memory" if classic["token_cached"] else "No active token"
        )

        oauth = overview["oauth_gateway"]
        api_table.add_row(
            "OAuth2 Gateway",
            "✅ Yes" if oauth["configured"] else "❌ No",
            "[bold green]✅ Connected[/bold green]" if oauth["authenticated"] else "[bold red]❌ Offline[/bold red]",
            oauth["client_id"],
            f"Scopes: {', '.join(oauth['scopes'])} (Expires in {oauth['token_expires_in_seconds']}s)"
        )
        console.print(api_table)

        # 2. Browser Automation Session Table
        br = overview["browser_session"]
        br_table = Table(title="🖥️  EZLynx Browser Automation Session State", show_header=True, header_style="bold cyan")
        br_table.add_column("Component", style="bold cyan")
        br_table.add_column("Status", style="bold")
        br_table.add_column("Path / Endpoint", style="white")
        br_table.add_column("Details", style="dim")

        br_table.add_row(
            "Storage State JSON",
            "[bold green]✅ Present[/bold green]" if br["exists"] else "[yellow]⚠️ Missing (Fresh login needed)[/yellow]",
            br["storage_state_file"],
            f"{br['cookie_count']} cookies stored (Last modified: {br['last_modified'] or 'Never'})"
        )
        br_table.add_row(
            "Chrome CDP Remote Port",
            "[bold green]✅ Active[/bold green]" if br["cdp_connected"] else "[dim yellow]⏳ Inactive / Disconnected[/dim yellow]",
            br["cdp_endpoint"],
            "Ready for non-eviction automation" if br["cdp_connected"] else "Start Chrome with --remote-debugging-port=9222"
        )
        console.print(br_table)
        return

    if args.ezlynx_quote_session:
        from src.ezlynx.api_client import EZLynxApiClient
        client = EZLynxApiClient()
        quote_id = args.ezlynx_quote_session
        console.print(f"\n[bold cyan]📑 Fetching Completed Rating Quote Session for ID:[/bold cyan] '[yellow]{quote_id}[/yellow]'...\n")
        res = client.get_completed_quote(quote_id)
        if res.get("status") != "success":
            console.print(f"[bold red]Failed to retrieve quote session: {res.get('error')}[/bold red]\n")
            return

        qd = res.get("quote_data", {})
        console.print(f"[bold green]Applicant ID:[/bold green] {qd.get('ApplicantId')} | [bold green]Rating State:[/bold green] {qd.get('RatingState')}")
        results = qd.get("QuoteResults", [])
        if not results:
            console.print("[yellow]No individual carrier quote results found in this session.[/yellow]\n")
            return

        table = Table(title=f"Comparative Rating Results ({len(results)} carriers)", show_header=True, header_style="bold magenta")
        table.add_column("Carrier Name", style="bold cyan")
        table.add_column("LOB", style="yellow")
        table.add_column("Status", style="bold")
        table.add_column("Premium", justify="right", style="bold green")
        table.add_column("Term", justify="center", style="white")
        table.add_column("Description", style="dim")

        for r in results:
            prem = f"${r.get('Premium', 0):,.2f}" if r.get('Premium') is not None else "N/A"
            st_color = "green" if r.get("Status") == "Succeeded" else "yellow"
            table.add_row(
                str(r.get("CarrierName") or "N/A"),
                str(r.get("LOB") or "N/A"),
                f"[{st_color}]{r.get('Status')}[/{st_color}]",
                prem,
                str(r.get("PremiumTerm") or "12") + " mos",
                str(r.get("Description") or "")
            )
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

    if args.file_uw_replies:
        from src.email_outreach.uw_reply_filer import run_uw_reply_filing

        console.print(
            "[bold cyan]Filing underwriter replies from robie@ + hello@ onto titled EZLynx cards...[/bold cyan]"
        )
        summary = run_uw_reply_filing(dry_run=args.dry_run)
        console.print(
            f"[bold green]filed={summary.get('filed')}[/bold green] "
            f"skipped={summary.get('skipped')} errors={summary.get('errors')} "
            f"dry_run={summary.get('dry_run')}"
        )
        return

    orchestrator = DailyRenewalOrchestrator()

    ref_date = date.today()
    if args.date:
        ref_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    send_report = args.send_report or args.daemon
    if args.daemon:
        console.print("[bold green]Starting Renewal Automation in Daily Daemon Mode...[/bold green]")
        while True:
            try:
                results = await orchestrator.run_daily_cycle(date.today(), send_report_email=True)
                orchestrator.print_summary_dashboard(results)
            except Exception as e:
                logger.error(f"Error in daemon cycle: {e}")
            # Sleep 24 hours (86400s)
            await asyncio.sleep(86400)
    else:
        results = await orchestrator.run_daily_cycle(ref_date, send_report_email=send_report)
        orchestrator.print_summary_dashboard(results)

def cli_main():
    asyncio.run(async_main())

if __name__ == "__main__":
    cli_main()
