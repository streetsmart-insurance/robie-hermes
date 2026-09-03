"""Interactive test runner for Trinity Underwriters (5 manual renewals)."""

import os
import sys
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.database.session import SessionLocal, init_db
from src.database.models import (
    PolicyRenewal, OutreachThread, AuditNoteLog, DocumentRecord,
    RenewalStatus, ThreadStatus, ActionType
)
from src.email_outreach.templates import get_outreach_subject, get_initial_outreach_body, get_followup_body
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier
from src.extractor.quote_parser import QuoteDocumentParser
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.ezlynx.api_client import EZLynxApiClient
from src.email_outreach.thread_tracker import OutreachCadenceManager

console = Console()

def get_carrier_renewals(db, carrier_name="Trinity Underwriters"):
    return db.query(PolicyRenewal).filter(
        PolicyRenewal.carrier_name.ilike(f"%{carrier_name}%")
    ).order_by(PolicyRenewal.expiration_date.asc()).all()

def print_policies_table(policies, carrier_title="Trinity Underwriters"):
    table = Table(title=f"🏢 {carrier_title} - Expiring Manual Renewals ({len(policies)} Policies)", show_header=True, header_style="bold magenta")
    table.add_column("ID", justify="center", style="dim")
    table.add_column("Policy #", style="bold cyan")
    table.add_column("Named Insured", style="white")
    table.add_column("LOB", style="yellow")
    table.add_column("Exp. Date", style="bold red")
    table.add_column("Exp. Prem", justify="right", style="green")
    table.add_column("CSR Assigned", style="bold blue")
    table.add_column("EZLynx App ID", style="cyan")
    table.add_column("Discussion Title", style="magenta")
    table.add_column("Current Status", style="bold yellow")

    for p in policies:
        table.add_row(
            str(p.id),
            p.policy_number,
            p.insured_name,
            p.line_of_business or "Auto",
            str(p.expiration_date),
            f"${p.expiring_premium:,.2f}" if p.expiring_premium else "N/A",
            p.assigned_agent or "Unassigned",
            p.applicant_id,
            p.discussion_title,
            p.status.value
        )
    console.print(table)

def preview_outreach_emails(policies):
    from src.portals.carrier_routing import CarrierRoutingMatrix
    from src.email_outreach.thread_tracker import resolve_outreach_cc_list

    console.print("\n[bold cyan]📨 --- Preview of Outbound Renewal Requests for Trinity Underwriters ---[/bold cyan]\n")
    for p in policies:
        subject = get_outreach_subject(p.id, p.insured_name, p.policy_number, p.expiration_date)
        body = get_initial_outreach_body(
            underwriter_name=p.underwriter_name,
            insured_name=p.insured_name,
            policy_num=p.policy_number,
            carrier_name=p.carrier_name,
            line_of_business=p.line_of_business or "Auto (Commercial)",
            expiration_date=p.expiration_date,
            expiring_premium=p.expiring_premium,
            assigned_agent=p.assigned_agent,
            agency_name="StreetSmart Insurance",
            ask_portal=False
        )
        carrier_conf = CarrierRoutingMatrix.get_carrier_config(p.carrier_name)
        dest = p.underwriter_email or carrier_conf.get("underwriter_email", "quotes@trinityunderwriters.net")
        cc_list = resolve_outreach_cc_list(p.assigned_agent, always_cc_jake=True)
        
        panel_content = f"[bold green]To:[/bold green] {dest}\n" \
                        f"[bold green]CC:[/bold green] {', '.join(cc_list)}\n" \
                        f"[bold green]Subject:[/bold green] {subject}\n" \
                        f"[bold green]Discussion Title:[/bold green] {p.discussion_title} (Applicant #{p.applicant_id})\n" \
                        f"[bold green]Assigned CSR:[/bold green] {p.assigned_agent}\n\n" \
                        f"[white]{body}[/white]"
        console.print(Panel(panel_content, title=f"Policy #{p.policy_number} - {p.insured_name}", border_style="cyan"))

def simulate_quote_receipt_and_ingestion(db, policy_id: int = 31):
    """Simulates receiving a quote PDF back from Trinity Underwriters for Edwin Lema (Pol #A23B8960-78760-SSRM NTL)."""
    pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == policy_id).first()
    if not pol:
        console.print(f"[bold red]Policy ID {policy_id} not found.[/bold red]")
        return

    console.print(f"\n[bold yellow]⚡ Simulating Carrier Quote Ingestion for Pol #{pol.policy_number} ({pol.insured_name})...[/bold yellow]\n")

    # 1. Create simulated quote PDF file using reportlab
    mock_pdf_path = settings.downloads_path / f"Trinity_Renewal_Quote_{pol.policy_number}.pdf"
    mock_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(mock_pdf_path), pagesize=letter)
    c.drawString(100, 750, "TRINITY UNDERWRITERS COMMERCIAL AUTO RENEWAL OFFER")
    c.drawString(100, 720, f"Named Insured: {pol.insured_name}")
    c.drawString(100, 700, f"Policy Number: {pol.policy_number}")
    c.drawString(100, 680, f"Line of Business: Non-Trucking Liability (NTL)")
    c.drawString(100, 660, f"Effective Date: {pol.expiration_date.strftime('%m/%d/%Y')}")
    c.drawString(100, 640, f"Expiration Date: {(pol.expiration_date + timedelta(days=365)).strftime('%m/%d/%Y')}")
    c.drawString(100, 620, "Coverage Limits: Combined Single Limit $1,000,000")
    c.drawString(100, 590, "Total Renewal Premium: $525.00")
    c.drawString(100, 560, "Payment Terms: Annual or 4-Pay Option")
    c.save()
    console.print(f"📄 Created simulated quote file: [dim]{mock_pdf_path}[/dim]")

    # 2. Extract Quote Data using QuoteDocumentParser
    parser = QuoteDocumentParser()
    extracted = parser.parse_pdf(mock_pdf_path)
    console.print(f"🔍 [bold green]Extracted Quote Terms:[/bold green] Premium = [bold yellow]${extracted.renewal_premium:,.2f}[/bold yellow] | Effective = [bold cyan]{extracted.effective_date}[/bold cyan]")

    # 3. Classify Underwriter Email Reply
    classifier = UnderwriterIntentClassifier()
    email_subject = f"Re: [RENEWAL-REQ-{pol.id}] Renewal & Loss Runs Request: {pol.insured_name} - Pol #{pol.policy_number} (Exp: {pol.expiration_date.strftime('%m/%d/%Y')})"
    email_body = f"Hello {pol.assigned_agent.split(',')[0] if pol.assigned_agent else 'Team'},\n\nPlease see attached the renewal quote for {pol.insured_name}. Total renewal premium is $525.00.\nLet us know if you want us to bind.\n\nThank you,\nTrinity Underwriters Commercial Auto Team"
    classification = classifier.classify(email_subject, email_body, [mock_pdf_path.name])
    console.print(f"🤖 [bold green]Intent Classification:[/bold green] [bold magenta]{classification.intent}[/bold magenta] ({classification.confidence:.0%}) - {classification.summary}")

    # 4. Update Policy and Thread Status
    pol.renewal_premium = extracted.renewal_premium
    if pol.expiring_premium:
        pol.premium_change_pct = ((pol.renewal_premium - pol.expiring_premium) / pol.expiring_premium) * 100
    pol.status = RenewalStatus.QUOTE_RECEIVED

    thread = db.query(OutreachThread).filter(OutreachThread.policy_id == pol.id).first()
    if thread:
        thread.status = ThreadStatus.RESOLVED
        thread.latest_reply_summary = f"[GMAIL_ROBIE | {classification.intent}] {classification.summary}"

    # 5. Record Document in Database
    doc = DocumentRecord(
        policy_id=pol.id,
        file_name=mock_pdf_path.name,
        file_path=str(mock_pdf_path),
        source="GMAIL_ROBIE",
        extracted_premium=pol.renewal_premium,
        extracted_summary=f"Premium: ${pol.renewal_premium:,.2f}"
    )
    db.add(doc)

    # 6. Build and Record EZLynx Discussion Note
    reply_note = EZLynxNoteBuilder.format_reply_received_note(
        policy=pol,
        tracking_code=thread.tracking_code if thread else f"RENEWAL-REQ-{pol.id}",
        sender_email="underwriting@trinityunderwriters.com",
        intent=classification.intent,
        summary=classification.summary,
        has_attachment=True,
        attachment_name=mock_pdf_path.name
    )
    db.add(AuditNoteLog(
        policy_id=pol.id,
        applicant_id=pol.applicant_id,
        discussion_title=pol.discussion_title,
        action_type=ActionType.UNDERWRITER_REPLIED,
        note_text=reply_note
    ))

    # 7. Post Note and Create Task in EZLynx
    ezlynx = EZLynxApiClient()
    ezlynx.add_note_to_discussion(
        applicant_id=pol.applicant_id,
        discussion_title=pol.discussion_title,
        note_text=reply_note,
        policy_number=pol.policy_number
    )
    ezlynx.upload_document(
        applicant_id=pol.applicant_id,
        file_path=mock_pdf_path,
        folder_name="Renewals"
    )
    task_desc = f"Underwriter emailed renewal proposal for Pol #{pol.policy_number}.\n" \
                f"Expiring: ${pol.expiring_premium:,.2f} | Renewal: ${pol.renewal_premium:,.2f} ({'+' if pol.premium_change_pct >= 0 else ''}{pol.premium_change_pct:.1f}%)\n" \
                f"Document {mock_pdf_path.name} attached in EZLynx Documents tab."
    ezlynx.create_user_task(
        applicant_id=pol.applicant_id,
        title=f"Review Renewal Quote: {pol.insured_name} (Trinity Underwriters)",
        description=task_desc,
        assigned_user=pol.assigned_agent
    )

    db.commit()

    # Display results
    console.print(Panel(
        f"[bold green]✅ Simulation Complete for Pol #{pol.policy_number}![/bold green]\n\n"
        f"• [bold]New Policy Status:[/bold] {pol.status.value}\n"
        f"• [bold]Expiring Premium:[/bold] ${pol.expiring_premium:,.2f}\n"
        f"• [bold]New Renewal Premium:[/bold] ${pol.renewal_premium:,.2f} ({'+' if pol.premium_change_pct >= 0 else ''}{pol.premium_change_pct:.1f}%)\n"
        f"• [bold]EZLynx Discussion:[/bold] '{pol.discussion_title}' (Applicant #{pol.applicant_id})\n"
        f"• [bold]EZLynx Task Assigned To:[/bold] {pol.assigned_agent}\n\n"
        f"[bold cyan]Generated EZLynx Discussion Note:[/bold cyan]\n[dim]{reply_note}[/dim]\n\n"
        f"[bold cyan]Generated EZLynx Task Note:[/bold cyan]\n[dim]{task_desc}[/dim]",
        title=f"🎉 Successfully Ingested Quote for {pol.insured_name}",
        border_style="green"
    ))

def simulate_csr_escalation(db, policy_id: int = 47):
    """Simulates 20-25 day CSR auto-escalation trigger for PEPPEP N SON'S TRUCKING LLC."""
    pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == policy_id).first()
    if not pol:
        console.print(f"[bold red]Policy ID {policy_id} not found.[/bold red]")
        return

    console.print(f"\n[bold yellow]⚠️ Testing 20–25 Day CSR Auto-Escalation for Pol #{pol.policy_number} ({pol.insured_name})...[/bold yellow]\n")

    simulated_today = pol.expiration_date - timedelta(days=22)
    days_rem = (pol.expiration_date - simulated_today).days
    console.print(f"Simulating date: [bold cyan]{simulated_today}[/bold cyan] ({days_rem} days before expiration: {pol.expiration_date})")

    pol.status = RenewalStatus.ESCALATED_MANUAL

    esc_note = (
        f"⚠️ === [CSR ESCALATION - URGENT RENEWAL REVIEW] ===\n"
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Policy #: {pol.policy_number}\n"
        f"Named Insured: {pol.insured_name}\n"
        f"Carrier / MGA: {pol.carrier_name}\n"
        f"Expiration Date: {pol.expiration_date} ({days_rem} days remaining)\n"
        f"Assigned CSR: {pol.assigned_agent}\n"
        f"Reason: No renewal quote received by {days_rem} days prior to expiration.\n"
        f"Action Required: High-priority CSR follow-up with carrier underwriter / portal."
    )

    db.add(AuditNoteLog(
        policy_id=pol.id,
        applicant_id=pol.applicant_id,
        discussion_title=pol.discussion_title,
        action_type=ActionType.STATUS_CHANGE,
        note_text=esc_note
    ))

    ezlynx = EZLynxApiClient()
    ezlynx.add_note_to_discussion(
        applicant_id=pol.applicant_id,
        discussion_title=pol.discussion_title,
        note_text=esc_note,
        policy_number=pol.policy_number
    )
    ezlynx.create_user_task(
        applicant_id=pol.applicant_id,
        title=f"URGENT: Call Trinity Underwriters for {pol.insured_name} Renewal",
        description=f"Policy #{pol.policy_number} is {days_rem} days from expiration ({pol.expiration_date}) with no renewal quote yet.",
        assigned_user=pol.assigned_agent
    )

    db.commit()

    console.print(Panel(
        f"[bold red]🚨 CSR Auto-Escalation Triggered![/bold red]\n\n"
        f"• [bold]Policy:[/bold] #{pol.policy_number} ({pol.insured_name})\n"
        f"• [bold]Days to Expiration:[/bold] {days_rem} days\n"
        f"• [bold]New Status:[/bold] {pol.status.value}\n"
        f"• [bold]EZLynx Discussion Note Added:[/bold] Under '{pol.discussion_title}'\n"
        f"• [bold]High-Priority Task Created For:[/bold] {pol.assigned_agent}\n\n"
        f"[dim]{esc_note}[/dim]",
        title="⚠️ Escalation Verification Result",
        border_style="yellow"
    ))

def send_live_test_email(db, target_email: str, policy_id: int = 31):
    """Sends a real test outreach email to a specified destination email via Robie's Gmail."""
    from src.email_outreach.gmail_client import GmailRenewalClient

    pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == policy_id).first()
    if not pol:
        console.print(f"[bold red]Policy ID {policy_id} not found.[/bold red]")
        return

    gmail = GmailRenewalClient()
    if not gmail.is_authenticated():
        console.print("[bold red]Gmail client is not authenticated.[/bold red]")
        return

    subject = f"[TEST-OUTREACH] {get_outreach_subject(pol.id, pol.insured_name, pol.policy_number, pol.expiration_date)}"
    body = get_initial_outreach_body(
        underwriter_name=pol.underwriter_name,
        insured_name=pol.insured_name,
        policy_num=pol.policy_number,
        carrier_name=pol.carrier_name,
        line_of_business=pol.line_of_business or "Auto (Commercial)",
        expiration_date=pol.expiration_date,
        expiring_premium=pol.expiring_premium,
        assigned_agent=pol.assigned_agent,
        agency_name="StreetSmart Insurance",
        ask_portal=False
    )

    console.print(f"\n[bold yellow]📤 Sending live test outreach email from {gmail.outreach_email} to {target_email}...[/bold yellow]")
    res = gmail.send_email(
        to_email=target_email,
        subject=subject,
        body_text=body
    )
    console.print(f"[bold green]✅ Email sent successfully![/bold green] Message ID: {res.get('id')}, Thread ID: {res.get('threadId')}")

def dispatch_live_outreach(db, carrier_name="Trinity Underwriters"):
    """Sends real renewal request emails to the underwriter for the carrier's policies,
    CCing the assigned CSR and Jake Ferrara, and sets policies to EMAIL_SENT_AWAITING_REPLY.
    """
    from src.portals.carrier_routing import CarrierRoutingMatrix
    from src.email_outreach.thread_tracker import resolve_outreach_cc_list, OutreachCadenceManager
    from src.email_outreach.gmail_client import GmailRenewalClient

    gmail = GmailRenewalClient()
    if not gmail.is_authenticated():
        console.print("[bold red]Gmail client is not authenticated for outreach.[/bold red]")
        return

    carrier_conf = CarrierRoutingMatrix.get_carrier_config(carrier_name)
    target_email = carrier_conf.get("underwriter_email", "quotes@trinityunderwriters.net")

    policies = get_carrier_renewals(db, carrier_name=carrier_name)
    if not policies:
        console.print(f"[bold red]No policies found for carrier '{carrier_name}'.[/bold red]")
        return

    cadence_mgr = OutreachCadenceManager(gmail_client=gmail)
    curr_date = date.today()
    next_followup = cadence_mgr._calc_next_followup(curr_date, settings.followup_cadence_min_days, settings.followup_cadence_max_days)

    console.print(f"\n[bold yellow]🚀 Dispatching Live Renewal Request Emails for {carrier_name} ({len(policies)} Policies)...[/bold yellow]\n")

    results_table = Table(title=f"📬 Live Email Dispatch Results - {carrier_name}", show_header=True, header_style="bold green")
    results_table.add_column("Pol ID", justify="center")
    results_table.add_column("Policy #", style="cyan")
    results_table.add_column("Insured", style="white")
    results_table.add_column("To", style="green")
    results_table.add_column("CC", style="magenta")
    results_table.add_column("Message ID", style="dim")
    results_table.add_column("Status", style="bold yellow")

    for pol in policies:
        pol.underwriter_email = target_email
        tracking_code = f"RENEWAL-REQ-{pol.id}"
        subject = get_outreach_subject(pol.id, pol.insured_name, pol.policy_number, pol.expiration_date)
        body = get_initial_outreach_body(
            underwriter_name=pol.underwriter_name,
            insured_name=pol.insured_name,
            policy_num=pol.policy_number,
            carrier_name=pol.carrier_name,
            line_of_business=pol.line_of_business or "Auto (Commercial)",
            expiration_date=pol.expiration_date,
            expiring_premium=pol.expiring_premium,
            assigned_agent=pol.assigned_agent,
            agency_name="StreetSmart Insurance",
            ask_portal=False
        )

        cc_list = resolve_outreach_cc_list(pol.assigned_agent, always_cc_jake=True)

        res = gmail.send_email(
            to_email=target_email,
            subject=subject,
            body_text=body,
            cc=cc_list
        )

        msg_id = res.get("id", "N/A")
        gmail_thread_id = res.get("threadId", "N/A")

        thread = db.query(OutreachThread).filter(
            (OutreachThread.tracking_code == tracking_code) | (OutreachThread.policy_id == pol.id)
        ).first()

        if thread:
            thread.gmail_thread_id = gmail_thread_id
            thread.last_message_id = msg_id
            thread.recipient_email = target_email
            thread.subject_line = subject
            thread.last_followup_at = datetime.utcnow()
            thread.next_followup_due = next_followup
            thread.status = ThreadStatus.ACTIVE
        else:
            thread = OutreachThread(
                policy_id=pol.id,
                tracking_code=tracking_code,
                gmail_thread_id=gmail_thread_id,
                last_message_id=msg_id,
                recipient_email=target_email,
                subject_line=subject,
                initial_sent_at=datetime.utcnow(),
                last_followup_at=datetime.utcnow(),
                followup_count=0,
                next_followup_due=next_followup,
                status=ThreadStatus.ACTIVE
            )
            db.add(thread)

        pol.status = RenewalStatus.EMAIL_SENT_AWAITING_REPLY

        note_text = EZLynxNoteBuilder.format_outreach_email_note(
            policy=pol,
            tracking_code=tracking_code,
            recipient_email=target_email,
            subject=subject,
            is_followup=False,
            next_followup_date=next_followup,
            cc_list=cc_list
        )
        db.add(AuditNoteLog(
            policy_id=pol.id,
            applicant_id=pol.applicant_id,
            discussion_title=pol.discussion_title,
            action_type=ActionType.INITIAL_EMAIL_SENT,
            note_text=note_text
        ))

        results_table.add_row(
            str(pol.id),
            pol.policy_number,
            pol.insured_name,
            target_email,
            ", ".join(cc_list),
            msg_id,
            "EMAIL_SENT_AWAITING_REPLY (Pending Documents)"
        )

    db.commit()
    console.print(results_table)
    console.print(f"\n[bold green]✅ All {len(policies)} emails dispatched successfully and policies pended for documents![/bold green]\n")

def main():
    parser = argparse.ArgumentParser(description="Test runner for Carrier Renewals (Default: Trinity Underwriters)")
    parser.add_argument("--carrier", type=str, default="Trinity Underwriters", help="Carrier Name to filter (default: Trinity Underwriters)")
    parser.add_argument("--list", action="store_true", help="List expiring renewals for the carrier")
    parser.add_argument("--preview", action="store_true", help="Preview outbound email requests for the renewals")
    parser.add_argument("--simulate-quote", action="store_true", help="Simulate quote receipt, PDF parsing, EZLynx note & task creation")
    parser.add_argument("--simulate-escalation", action="store_true", help="Simulate 20-25 day CSR escalation trigger")
    parser.add_argument("--send-test-to", type=str, default=None, help="Send real test email for a renewal to this email address")
    parser.add_argument("--dispatch-live", action="store_true", help="Send live outreach emails to carrier underwriter, CC CSR & Jake, pend policies")
    parser.add_argument("--all", action="store_true", help="Run full test demonstration (list, preview, quote simulation, escalation)")

    args = parser.parse_args()
    init_db()
    db = SessionLocal()

    policies = get_carrier_renewals(db, carrier_name=args.carrier)
    if not policies:
        console.print(f"[bold red]No policies found for carrier matching '{args.carrier}'.[/bold red]")
        return

    if args.dispatch_live:
        dispatch_live_outreach(db, carrier_name=args.carrier)
        db.close()
        return

    if args.list or args.all or (not any([args.preview, args.simulate_quote, args.simulate_escalation, args.send_test_to])):
        print_policies_table(policies, carrier_title=args.carrier)

    if args.preview or args.all:
        preview_outreach_emails(policies)

    if args.simulate_quote or args.all:
        simulate_quote_receipt_and_ingestion(db, policy_id=31)

    if args.simulate_escalation or args.all:
        simulate_csr_escalation(db, policy_id=47)

    if args.send_test_to:
        send_live_test_email(db, target_email=args.send_test_to, policy_id=31)

    db.close()

if __name__ == "__main__":
    main()
