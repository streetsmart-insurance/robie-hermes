"""Email templates for initial underwriter outreach and automated 5-7 day cadence follow-ups."""

from datetime import date
from typing import Optional

def get_outreach_subject(policy_id: int, insured_name: str, policy_num: str, expiration_date: Optional[date] = None) -> str:
    """Formats subject line with tracking reference, named insured, and policy number."""
    if expiration_date:
        return f"[RENEWAL-REQ-{policy_id}] Renewal & Loss Runs Request: {insured_name} - Pol #{policy_num} (Exp: {expiration_date.strftime('%m/%d/%Y')})"
    return f"[RENEWAL-REQ-{policy_id}] Renewal & Loss Runs Request: {insured_name} - Pol #{policy_num}"

def get_initial_outreach_body(
    underwriter_name: Optional[str],
    insured_name: str,
    policy_num: str,
    carrier_name: str,
    line_of_business: str,
    expiration_date: date,
    expiring_premium: Optional[float] = None,
    assigned_agent: Optional[str] = None,
    agency_name: str = "StreetSmart Insurance",
    ask_portal: bool = False,
    sender_name: str = "Robie"
) -> str:
    greeting = f"Hello {underwriter_name};" if underwriter_name else "Hello;"
    prem_str = f"${expiring_premium:,.2f}" if expiring_premium else None

    lines = [
        greeting,
        "",
        "We hope you are well! ",
        "",
        "At your earliest convenience please forward the upcoming renewal and loss runs for our file.",
        "",
        f"• Named Insured: {insured_name}",
        f"• Policy Number: {policy_num}",
        f"• Carrier: {carrier_name}",
        f"• Line of Business: {line_of_business}",
        f"• Expiration Date: {expiration_date.strftime('%m/%d/%Y')}"
    ]

    if prem_str:
        lines.append(f"• Expiring Term Premium: {prem_str}")

    if ask_portal:
        lines.extend([
            "",
            "Additionally, please let us know if there is an online agent portal where we can view and download renewal documents directly."
        ])

    lines.extend([
        "",
        "Should you have any questions please feel free to email me back.",
        "",
        "Thank you,",
        "",
        sender_name,
        agency_name
    ])

    return "\n".join(lines)

def get_followup_body(
    followup_number: int,
    underwriter_name: Optional[str],
    insured_name: str,
    policy_num: str,
    expiration_date: date,
    days_to_expiration: int,
    assigned_agent: Optional[str] = None,
    agency_name: str = "StreetSmart Insurance",
    sender_name: str = "Robie"
) -> str:
    greeting = f"Hello {underwriter_name};" if underwriter_name else "Hello;"

    if followup_number == 1:
        return f"""{greeting}

We hope you are well! 

Following up on our earlier request for {insured_name} (Pol #{policy_num}), expiring on {expiration_date.strftime('%m/%d/%Y')} ({days_to_expiration} days remaining).

At your earliest convenience please forward the upcoming renewal and loss runs for our file.

Should you have any questions please feel free to email me back.

Thank you,

{sender_name}
{agency_name}
"""
    elif followup_number == 2:
        return f"""{greeting}

We hope you are well! 

Second follow-up regarding the upcoming renewal for {insured_name} (Pol #{policy_num}) expiring on {expiration_date.strftime('%m/%d/%Y')} ({days_to_expiration} days remaining).

At your earliest convenience please forward the upcoming renewal and loss runs for our file.

Should you have any questions please feel free to email me back.

Thank you,

{sender_name}
{agency_name}
"""
    else:
        return f"""{greeting}

[URGENT] Final reminder regarding the upcoming renewal for {insured_name} (Pol #{policy_num}) expiring on {expiration_date.strftime('%m/%d/%Y')}.

At your earliest convenience please forward the upcoming renewal and loss runs for our file.

Should you have any questions please feel free to email me back.

Thank you,

{sender_name}
{agency_name}
"""
