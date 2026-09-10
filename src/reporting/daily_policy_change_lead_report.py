"""Daily Policy Change & Confirmation Handoff Report for Leadership & Commercial Team Leads.

Delivers a comprehensive morning digest of:
1. Ingested EZLynx Policy Change Confirmation Queue
2. Commercial Auto & Trucking Deep Dive
3. Commercial Lines (GL, BOP, WC, Excess) Status
4. ROBIE Autonomous Verification & Retrieval Actions
5. Escalations & Immediate Action Items
"""

import os
import sys
import csv
import glob
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
import markdown

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.email_outreach.gmail_client import GmailRenewalClient


# --- Carrier routing + age. Facts only: channel is a static property of the
# carrier, age is computed from the record. Nothing here asserts case progress,
# names an underwriter, or invents a date. If we have not verified something,
# the report says so. ---

CARRIER_CHANNEL = {
    "progressive": "Direct portal (Progressive FAO)",
    "national general": "Direct portal (NatGen)",
    "guard": "Direct portal (Berkshire GUARD)",
    "bhhc": "Direct portal (BHHC)",
    "travelers": "Direct portal",
    "amtrust": "Direct portal (AmTrust Online)",
    "merchants": "Dec-less carrier - portal roster inspection required (Inv. 2)",
    "jimcor": "Wholesale/MGA desk",
    "rt specialty": "Wholesale/MGA desk",
    "tapco": "Wholesale/MGA desk",
    "burns": "Wholesale/MGA desk",
    "jm wilson": "Wholesale/MGA desk",
    "utica first": "Underwriter desk",
    "geico": "Underwriter desk",
}


def carrier_channel(carrier):
    """Retrieval channel for a carrier. Static routing fact, not case status."""
    c = (carrier or "").lower()
    for key, chan in CARRIER_CHANNEL.items():
        if key in c:
            return chan
    return "Channel not classified"


def age_status(created_date, report_date, parse_dt):
    """Age and SLA state derived from the record. No invented dates."""
    try:
        days = (report_date - parse_dt(created_date)).days
    except Exception:
        return "age unknown"
    if days <= 2:
        return f"{days}d open - inside 24-48h carrier SLA (Inv. 6)"
    if days <= 14:
        return f"{days}d open"
    if days <= 60:
        return f"<b>{days}d open - aging</b>"
    return f"<b>{days}d open - legacy backlog</b>"


def action_cell(carrier, created_date, report_date, parse_dt):
    return (f"{carrier_channel(carrier)} | {age_status(created_date, report_date, parse_dt)}"
            " | <i>no verified carrier evidence on file</i>")



logger = logging.getLogger("policy_change_lead_report")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

LEAD_RECIPIENT_LIST = [
    "carlo@streetsmart.insurance",
    "sandy@streetsmart.insurance",
    "jake@streetsmart.insurance",
    "ashley@streetsmart.insurance",
    "gabrielac@streetsmart.insurance",
    "taylor@streetsmart.insurance",
]


def find_latest_report(pattern: str) -> Optional[Path]:
    reports_dir = PROJECT_ROOT / "data" / "input_reports"
    matches = list(reports_dir.glob(pattern))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def parse_confirmation_queue(csv_path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "account_name": (r.get("Account Name") or "").strip(),
                "applicant_id": (r.get("Applicant ID") or "").strip(),
                "policy_number": (r.get("Policy Number") or "").strip(),
                "lob": (r.get("Line Of Business") or "").strip(),
                "effective_date": (r.get("Effective Date") or "").strip(),
                "carrier": (r.get("Master Company") or "").strip(),
                "request_status": (r.get("Request Status") or "").strip(),
                "created_by": (r.get("Created By") or "").strip(),
                "written_premium": (r.get("Written Premium") or "").strip(),
                "producer": (r.get("Assigned Producer") or "").strip(),
                "csr": (r.get("CSR") or "").strip(),
                "policy_labels": (r.get("Policy Labels") or "").strip(),
                "created_date": (r.get("Change Request Created Date") or "").strip()
            })
    return rows


def generate_report_content(queue_rows: List[Dict[str, Any]], report_date: date) -> str:
    total_open = len(queue_rows)

    # Filter by category
    trucking_auto = []
    commercial_other = []
    personal_lines = []

    for r in queue_rows:
        lob_lower = r["lob"].lower()
        if "auto (commercial)" in lob_lower:
            trucking_auto.append(r)
        elif any(k in lob_lower for k in ["genl liability", "workers comp", "business owners", "commercial", "excess", "inland marine", "bop", "bonds"]):
            commercial_other.append(r)
        else:
            personal_lines.append(r)

    def parse_dt(d_str):
        try:
            return datetime.strptime(d_str, "%Y-%m-%d").date()
        except Exception:
            return date(2000, 1, 1)

    trucking_auto.sort(key=lambda x: parse_dt(x["created_date"]), reverse=True)
    commercial_other.sort(key=lambda x: parse_dt(x["created_date"]), reverse=True)

    recent_trucking = [r for r in trucking_auto if parse_dt(r["created_date"]) >= (report_date - timedelta(days=60))]
    legacy_trucking = [r for r in trucking_auto if parse_dt(r["created_date"]) < (report_date - timedelta(days=60))]

    recent_comm = [r for r in commercial_other if parse_dt(r["created_date"]) >= (report_date - timedelta(days=60))]
    legacy_comm = [r for r in commercial_other if parse_dt(r["created_date"]) < (report_date - timedelta(days=60))]

    date_str = report_date.strftime("%A, %B %d, %Y")

    md = []
    md.append(f"# 📋 StreetSmart Daily Policy Change & Confirmation Handoff Report")
    md.append(f"**Date:** {date_str} | **Engine:** ROBIE 1.0 Autonomous Confirmation & Audit | **Environment:** Production Server (`hermes-poc-01`)")
    md.append("")
    md.append("> [!NOTE]")
    md.append(f"> This report is compiled on the production server following intake of the morning EZLynx scheduled reports. It provides real-time visibility into open policy changes, carrier document retrieval progress, 3-way match verification, and immediate action items for Commercial & Trucking leadership.")
    md.append("")

    # Executive Overview
    md.append("## 1. Executive Summary & Queue Metrics")
    md.append("| Metric | Count | Primary Focus Lines |")
    md.append("| :--- | :---: | :--- |")
    md.append(f"| **Total Open Policy Change Requests** | **{total_open}** | Agency-wide open confirmation requests in EZLynx |")
    md.append(f"| **Commercial Auto & Trucking (Active)** | **{len(recent_trucking)}** | Fleet additions, vehicle swaps, driver roster updates |")
    md.append(f"| **Commercial Casualty & Property (Active)** | **{len(recent_comm)}** | Additional Insured, Waiver of Subrogation, Limit endorsements |")
    md.append(f"| **Personal Lines (Home / Auto / Dwelling)** | **{len(personal_lines)}** | Home purchases, lender updates, vehicle updates |")
    md.append(f"| **Legacy Backlog (>60 Days Old)** | **{len(legacy_trucking) + len(legacy_comm)}** | Prior year unconfirmed items pending archival / closure |")
    md.append("")

    # Commercial Auto / Trucking Section
    md.append("## 2. 🚛 Commercial Auto & Trucking Action Roster")
    md.append("Active Commercial Auto and Trucking policy changes under verification and carrier follow-up:")
    md.append("")
    md.append("| Insured / Account | Policy # | Carrier | CSR / Producer | Request Date | Status & ROBIE Autonomous Action |")
    md.append("| :--- | :--- | :--- | :--- | :---: | :--- |")

    for r in recent_trucking:
        carrier = r["carrier"]
        notes = action_cell(carrier, r["created_date"], report_date, parse_dt)

        md.append(f"| **{r['account_name']}**<br><code>App #{r['applicant_id']}</code> | <code>{r['policy_number']}</code> | {carrier} | {r['csr']}<br>*(Prod: {r['producer']})* | {r['created_date']} | {notes} |")

    md.append("")

    # Commercial Lines (GL, BOP, WC, Excess)
    md.append("## 3. 🏢 Commercial Casualty, Property & Workers Comp Roster")
    md.append("Active General Liability, Business Owners, Workers Comp, and Excess endorsements:")
    md.append("")
    md.append("| Insured / Account | LOB | Policy # | Carrier | CSR | Created Date | Labels / Scope | Action Status |")
    md.append("| :--- | :--- | :--- | :--- | :--- | :---: | :--- | :--- |")

    for r in recent_comm[:15]:
        labels = r["policy_labels"] or "Policy Change"
        carrier = r["carrier"]
        action_status = action_cell(carrier, r["created_date"], report_date, parse_dt)

        md.append(f"| **{r['account_name']}**<br><code>App #{r['applicant_id']}</code> | {r['lob']} | <code>{r['policy_number']}</code> | {carrier} | {r['csr']} | {r['created_date']} | <code>{labels}</code> | {action_status} |")

    md.append("")

    # What ROBIE is Doing
    md.append("## 4. 🤖 Autonomous ROBIE Actions & Safeguards in Flight")
    md.append("- **3-Way Match Verification**: Run on demand per case via the verification pipeline. This report does not assert that any row below has been matched.")
    md.append("- **Document Retrieval**: Portal, email and voice channels are available per the routing column above. Retrieval is not running continuously against the whole queue.")
    md.append("- **Invariant enforcement status** (what is actually enforced in code, not aspirational):")
    md.append("  - *Invariant 7 (Telephony Hours)*: **ENFORCED** - outbound calls gated to Mon-Fri 9:00-18:00 ET.")
    md.append("  - *Invariant 1 (Change-Request State)*: **NOT ENFORCED IN CODE** - closure gate is procedural only.")
    md.append("  - *Invariant 2 (Dec-Less Carrier Gate)*: **NOT ENFORCED IN CODE** - dec-less carriers flagged in the roster above.")
    md.append("  - *Invariant 6 (Turnaround Gate)*: **NOT ENFORCED IN CODE** - age is reported, follow-up is not suppressed automatically.")
    md.append("  - *Invariant 8 (Email Preemption)*: **NOT IMPLEMENTED** - no email watcher or call-kill path exists. Calls are not preempted by underwriter email.")
    md.append("")

    # Immediate Team Action Items
    md.append("## 5. ⚠️ Action Items & Team Escalations")
    md.append("1. **Merchants Insurance Group Logins**: Profile completed for Robie (`AI1434` / Agency `84409`). Verifying agency portal credentials to enable autonomous dec downloads on `files.merchantsgroup.com`.")
    md.append("2. **Legacy Policy Changes Archive**: There are legacy confirmation items from prior years in the queue. ROBIE can run an automated reconciliation to clear closed items with team approval.")
    md.append("3. **Underwriter Replies**: Not monitored automatically. Overnight carrier and underwriter replies must be reviewed by the assigned CSR - ROBIE does not log or route them.")

    return "\n".join(md)


def dispatch_daily_lead_report(skip_email: bool = False, custom_recipients: Optional[List[str]] = None) -> str:
    target_date = date.today()
    queue_file = find_latest_report("*Policy_Change_Request_Confirmation_Queue_-_ROBIE_*.csv")

    if not queue_file:
        logger.error("No Policy Change Confirmation Queue report found in data/input_reports!")
        return "Error: No confirmation queue report found."

    logger.info(f"Parsing confirmation queue report: {queue_file}")
    queue_rows = parse_confirmation_queue(queue_file)

    report_md = generate_report_content(queue_rows, target_date)

    # Save to reports directory
    reports_out_dir = PROJECT_ROOT / "reports"
    reports_out_dir.mkdir(parents=True, exist_ok=True)
    out_file = reports_out_dir / f"daily_policy_change_lead_report_{target_date.strftime('%Y_%m_%d')}.md"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info(f"Saved lead report markdown to {out_file}")

    if not skip_email:
        recipients = custom_recipients or LEAD_RECIPIENT_LIST
        date_str = target_date.strftime("%B %d, %Y")
        subject = f"📋 StreetSmart Daily Policy Change & Commercial Handoff - {date_str}"

        html_body = markdown.markdown(report_md, extensions=["tables", "fenced_code"])
        styled_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #2d3748; padding: 20px; }}
        h1 {{ color: #1a365d; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; font-size: 20px; }}
        h2 {{ color: #2b6cb0; margin-top: 24px; border-bottom: 1px solid #edf2f7; padding-bottom: 6px; font-size: 16px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 13px; }}
        th, td {{ border: 1px solid #cbd5e0; padding: 8px 10px; text-align: left; vertical-align: top; }}
        th {{ background-color: #f7fafc; font-weight: 600; color: #4a5568; }}
        tr:nth-child(even) {{ background-color: #f8fafc; }}
        code {{ background-color: #edf2f7; padding: 2px 5px; border-radius: 4px; font-family: monospace; font-size: 12px; }}
        blockquote {{ border-left: 4px solid #3182ce; margin: 12px 0; padding: 8px 14px; background: #ebf8ff; color: #2b6cb0; font-size: 13px; border-radius: 0 4px 4px 0; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #a0aec0; border-top: 1px solid #e2e8f0; padding-top: 12px; }}
    </style>
</head>
<body>
    {html_body}
    <div class="footer">
        <p>Sent autonomously by ROBIE 1.0 • StreetSmart Insurance Automation Engine • Production VM</p>
    </div>
</body>
</html>"""

        client = GmailRenewalClient()
        logger.info(f"Dispatching Lead Report Email to: {', '.join(recipients)}")
        try:
            res = client.send_email(
                to_email=recipients[0],
                cc=recipients[1:] if len(recipients) > 1 else None,
                subject=subject,
                body_text=report_md,
                html_body=styled_html
            )
            logger.info(f"Daily Lead Report email sent successfully! {res}")
        except Exception as e:
            logger.error(f"Failed to send Lead Report email: {e}", exc_info=True)
    else:
        logger.info("Skipping email delivery (--skip-email specified)")

    return report_md


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-email", action="store_true")
    parser.add_argument("--to-carlo-only", action="store_true")
    args = parser.parse_args()

    recips = ["carlo@streetsmart.insurance"] if args.to_carlo_only else None
    dispatch_daily_lead_report(skip_email=args.skip_email, custom_recipients=recips)
