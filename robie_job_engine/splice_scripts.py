"""Spoken frames for the Splice workflows and the lead follow-up.

The nine Splice bodies stay here as reference. A label does not select
them. Lead follow-up uses the Sales Center frame: on behalf of the
assigned producer, press 1 to reach that producer, press 2 when a text
is offered, and press 6 to opt out. There is no opt-out toll-free number.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bland_config import CALLBACK_NUMBER

WEBSITE = "www.streetsmart.insurance"


@dataclass(frozen=True)
class Workflow:
    id: str
    title: str
    body: str
    sms_body: str | None
    marketing: bool


WORKFLOWS: dict[str, Workflow] = {
    "audit_not_complete": Workflow(
        "audit_not_complete",
        "Audit Not Complete",
        "It appears that an audit for your account is currently incomplete. "
        "Please take the necessary steps to finalize this audit as soon as possible.",
        None,
        False,
    ),
    "recommendations_follow_up": Workflow(
        "recommendations_follow_up",
        "Recommendations Follow-Up",
        "We are following up on some recommendations that were made for your "
        "account. Please take the necessary steps to address these "
        "recommendations as soon as possible.",
        None,
        False,
    ),
    "returned_mail": Workflow(
        "returned_mail",
        "Returned Mail",
        "We have received some returned mail for your account. Please contact "
        "our office to update your information as soon as possible.",
        None,
        False,
    ),
    "esignature_follow_up": Workflow(
        "esignature_follow_up",
        "E-signature Follow-Up",
        "We are following up on an e-signature request for your account. "
        "Please complete the e-signature process as soon as possible.",
        None,
        False,
    ),
    "additional_information_follow_up": Workflow(
        "additional_information_follow_up",
        "Additional Information Follow-Up",
        "We are following up on a request for additional information for your "
        "account. Please provide the requested information as soon as possible.",
        None,
        False,
    ),
    "sales_center_reviewed": Workflow(
        "sales_center_reviewed",
        "Sales Center Reviewed Status",
        "We are following up on the quote we released a few days ago. We "
        "wanted to check in and see if you have any questions or if you're "
        "ready to move forward with the policy.",
        None,
        True,
    ),
    "winback_campaign": Workflow(
        "winback_campaign",
        "Winback Campaign",
        "We noticed you got insurance elsewhere last year. We appreciate your "
        "past business and wanted to reach out to see if we could win you "
        "back with a new offer.",
        "We noticed you got insurance elsewhere last year and wanted to see "
        "if we could win you back with a new offer.",
        True,
    ),
    "renewal_reach_out": Workflow(
        "renewal_reach_out",
        "Renewal Reach Out",
        "Your insurance policy will be up for renewal soon. We want to ensure "
        "you have the proper coverage and would like to discuss your options.",
        None,
        False,
    ),
    "unresponsive": Workflow(
        "unresponsive",
        "Unresponsive",
        "We are reaching out regarding your policies.",
        None,
        False,
    ),
    "lead_follow_up": Workflow(
        "lead_follow_up",
        "Lead Follow Up",
        "We are following up on your insurance inquiry or quote.",
        "We are following up on your insurance inquiry or quote.",
        False,
    ),
}

# Workflows 1–5 use the voice body as the text. 6 uses its voice body too.
# 7 has its own shorter text. 8 and 9 have no text version.
for _text_id in (
    "audit_not_complete",
    "recommendations_follow_up",
    "returned_mail",
    "esignature_follow_up",
    "additional_information_follow_up",
    "sales_center_reviewed",
):
    _item = WORKFLOWS[_text_id]
    WORKFLOWS[_text_id] = Workflow(
        _item.id, _item.title, _item.body, _item.body, _item.marketing,
    )


def get_workflow(workflow_id: str) -> Workflow | None:
    return WORKFLOWS.get((workflow_id or "").strip())


def spoken_first_name(applicant_name: str) -> str:
    token = (applicant_name or "").strip().split(" ")[0].strip(",").strip()
    return token


def text_option_allowed(
    workflow: Workflow, *, mobile: bool, sms_configured: bool,
) -> bool:
    """Press 2 exists only for a mobile number, configured SMS, and a text body."""
    return bool(mobile and sms_configured and workflow.sms_body)


def render_live(
    workflow: Workflow,
    *,
    first_name: str,
    agent: str,
    transfer_number: str,
    offer_text: bool,
) -> str:
    """Live-answer frame. Press 1 is included only when a transfer number exists."""
    hello = f"Hi {first_name}," if first_name else "Hi,"
    lines = [
        hello,
        f"This is a message on behalf of your agent, {agent} from StreetSmart Insurance.",
        workflow.body,
    ]
    if transfer_number:
        lines.append(
            "To speak to one of our representatives now, please press 1 or, "
            f"call {CALLBACK_NUMBER}."
        )
    else:
        lines.append(
            "To speak to one of our representatives now, please call "
            f"{CALLBACK_NUMBER}."
        )
    lines.append(f"You can also visit {WEBSITE} for more information.")
    if transfer_number:
        lines.append(
            "Press 1: Thank you, you are now being transferred, please stay on the line."
        )
    if offer_text:
        lines.append(
            "If Mobile: To receive a text message with this information, please press 2."
        )
        lines.append(
            "Press 2: Thank you, a text message has been sent to your mobile device."
        )
    lines.extend((
        "To hear this message again, please press 4.",
        "If you no longer wish to receive automated phone notifications, please press 6.",
        "Press 6: Thank you. You will no longer receive automated notifications "
        "from StreetSmart Insurance.",
        "Thank you, we value your business!",
    ))
    return "\n".join(lines)


def render_voicemail(workflow: Workflow, *, first_name: str, agent: str) -> str:
    """Voicemail frame. The toll-free opt-out sentence is omitted."""
    hello = f"Hi {first_name}," if first_name else "Hi,"
    return "\n".join((
        hello,
        f"This is a message on behalf of your agent, {agent} from StreetSmart Insurance.",
        workflow.body,
        f"To speak to one of our representatives now, please call {CALLBACK_NUMBER}.",
        f"You can also visit {WEBSITE} for more information.",
        "Thank you, we value your business!",
    ))


def render_text(workflow: Workflow) -> str | None:
    """Text frame, or None when this workflow has no text version."""
    if not workflow.sms_body:
        return None
    return "\n".join((
        f"StreetSmart: {workflow.sms_body}",
        f"Call {CALLBACK_NUMBER} for immediate assistance. Reply STOP to opt-out.",
        "Msg/Data rates apply",
    ))
