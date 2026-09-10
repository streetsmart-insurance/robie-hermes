"""
Email & SMS Copy Generator for Robie Multi-Channel Cadences.

Generates grounded, personalized, TCPA/CAN-SPAM compliant email and SMS copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.models.cadence_models import ApplicantLead, CadenceType, Opportunity, QuoteSummary

logger = logging.getLogger("email_scripts")


@dataclass
class EmailPackage:
    subject: str
    body_html: str
    body_text: str
    tracking_code: str


@dataclass
class SMSPackage:
    body_text: str
    tracking_code: str


class EmailAndSMSScriptBuilder:
    AGENCY_NAME = "StreetSmart Insurance"
    MAIN_PHONE = "(732) 462-8343"
    OFFICE_EMAIL = "team@streetsmart.insurance"

    @classmethod
    def build_email(
        cls,
        cadence_type: CadenceType,
        touch_number: int,
        lead: ApplicantLead,
        opportunity: Opportunity,
        quote: Optional[QuoteSummary] = None,
    ) -> EmailPackage:
        first = lead.spoken_first_name
        producer = lead.assigned_producer or "Jake Ferrara"
        producer_email = "jake@streetsmart.insurance"
        lob = opportunity.line_of_business or "insurance"
        tracking_code = f"ROBIE-{cadence_type.value[:3]}-{lead.applicant_id}-T{touch_number}"

        if cadence_type == CadenceType.INBOUND_LEAD:
            if touch_number == 0:
                subj = f"We received your {lob} quote inquiry — StreetSmart Insurance"
                body = (
                    f"Hi {first},\n\n"
                    f"Thank you for contacting StreetSmart Insurance! We received your request for a {lob} quote.\n\n"
                    f"Our licensed producer, {producer}, is reviewing your details to find you the best coverage and rates "
                    f"across our network of top-rated carriers.\n\n"
                    f"If you have any specific requirements or would like to discuss right away, feel free to call our office at "
                    f"{cls.MAIN_PHONE} or reply directly to this email.\n\n"
                    f"Best regards,\n"
                    f"{producer}\n"
                    f"StreetSmart Insurance\n"
                    f"Phone: {cls.MAIN_PHONE}\n"
                    f"Email: {producer_email}\n\n"
                    f"Tracking Code: {tracking_code}"
                )
            elif touch_number == 1:
                subj = f"Checking in on your {lob} quote — StreetSmart Insurance"
                body = (
                    f"Hi {first},\n\n"
                    f"I wanted to follow up on your recent inquiry for {lob} insurance. {producer} has gathered preliminary options "
                    f"and is ready to help you finalize the details.\n\n"
                    f"Do you have a few minutes today or tomorrow for a brief review? You can reach {producer} at {cls.MAIN_PHONE} "
                    f"or let us know a good time to call you.\n\n"
                    f"Best regards,\nRobie (on behalf of {producer})\nStreetSmart Insurance"
                )
            elif touch_number == 2:
                subj = f"Options for your {lob} insurance — StreetSmart Insurance"
                body = (
                    f"Hi {first},\n\n"
                    f"Just a quick check-in from StreetSmart Insurance regarding your {lob} coverage. We know shopping for insurance "
                    f"takes time, and we're here to make the process effortless.\n\n"
                    f"Reply here or call {cls.MAIN_PHONE} whenever you're ready to review your options.\n\n"
                    f"Sincerely,\n{producer}\nStreetSmart Insurance"
                )
            else:  # Touch 3 (Day 7)
                subj = f"Closing your file on {lob} quote inquiry"
                body = (
                    f"Hi {first},\n\n"
                    f"We haven't been able to connect regarding your {lob} inquiry, so I am going to place your file on inactive status "
                    f"so we don't continue to clutter your inbox.\n\n"
                    f"If you ever need coverage or want to revisit your rates in the future, please don't hesitate to reach back out to {producer} "
                    f"at {cls.MAIN_PHONE}.\n\n"
                    f"Thank you for considering StreetSmart Insurance!\n\n"
                    f"Warmly,\n{producer}\nStreetSmart Insurance"
                )

        elif cadence_type == CadenceType.QUOTED_PROSPECT:
            carrier = quote.carrier_name if quote and quote.carrier_name else "our carriers"
            if touch_number == 1:
                subj = f"Your {lob} proposal with {carrier} — StreetSmart Insurance"
                body = (
                    f"Hi {first},\n\n"
                    f"{producer} recently prepared your {lob} proposal with {carrier}. I wanted to check in and see if you had "
                    f"a chance to review the coverage details and pricing.\n\n"
                    f"If you'd like to make adjustments, explore higher liability limits, or are ready to bind coverage, please reply "
                    f"to this email or call our team at {cls.MAIN_PHONE}.\n\n"
                    f"Best regards,\n{producer}\nStreetSmart Insurance"
                )
            elif touch_number == 2:
                subj = f"Reviewing your {lob} coverage details"
                body = (
                    f"Hi {first},\n\n"
                    f"Following up on the proposal {producer} put together for your {lob} policy. We want to make sure you have "
                    f"the exact coverage you need with zero gaps.\n\n"
                    f"Feel free to reply with any questions or call us at {cls.MAIN_PHONE}.\n\n"
                    f"Best,\nRobie (on behalf of {producer})\nStreetSmart Insurance"
                )
            else:  # Touch 3
                subj = f"Final check-in regarding your {lob} proposal"
                body = (
                    f"Hi {first},\n\n"
                    f"I wanted to reach out one last time regarding your {lob} quote. If you've already found coverage elsewhere, "
                    f"no worries at all! Just let us know so we can update your file.\n\n"
                    f"If you still wish to proceed, {producer} is available at {cls.MAIN_PHONE}.\n\n"
                    f"Best wishes,\n{producer}\nStreetSmart Insurance"
                )

        else:  # XDATE_OPPORTUNITY
            if touch_number == 1:
                subj = f"Upcoming {lob} renewal review — StreetSmart Insurance"
                body = (
                    f"Hi {first},\n\n"
                    f"Our records show your current {lob} insurance policy is up for renewal in roughly six weeks. We'd love the opportunity "
                    f"to shop the market and see if we can provide better coverage or savings for the upcoming term.\n\n"
                    f"Reply to this email or call {cls.MAIN_PHONE} if you'd like a complimentary comparison.\n\n"
                    f"Best regards,\n{producer}\nStreetSmart Insurance"
                )
            elif touch_number == 2:
                subj = f"Comparing your {lob} rates before renewal"
                body = (
                    f"Hi {first},\n\n"
                    f"With your {lob} renewal about a month away, {producer} is ready to run comparative quotes across our top carriers.\n\n"
                    f"Give us a call at {cls.MAIN_PHONE} or reply with any recent changes to your policy so we can find you the best fit.\n\n"
                    f"Sincerely,\n{producer}\nStreetSmart Insurance"
                )
            else:
                subj = f"Important: {lob} policy renewing soon"
                body = (
                    f"Hi {first},\n\n"
                    f"Your {lob} renewal is approximately two weeks away. If you haven't reviewed alternative options yet, there's still "
                    f"time before your current policy automatically renews.\n\n"
                    f"Call {producer} at {cls.MAIN_PHONE} today for a fast turnaround.\n\n"
                    f"Best,\nRobie\nStreetSmart Insurance"
                )

        html = f"""<div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #333;">
{body.replace(chr(10), '<br>')}
<hr style="border: none; border-top: 1px solid #eee; margin-top: 20px;">
<p style="font-size: 11px; color: #888;">
StreetSmart Insurance | 732-462-8343 | team@streetsmart.insurance<br>
To opt out of future automated emails, reply with 'UNSUBSCRIBE'.
</p>
</div>"""
        return EmailPackage(
            subject=subj,
            body_html=html,
            body_text=body,
            tracking_code=tracking_code,
        )

    @classmethod
    def build_sms(
        cls,
        cadence_type: CadenceType,
        touch_number: int,
        lead: ApplicantLead,
        opportunity: Opportunity,
    ) -> SMSPackage:
        first = lead.spoken_first_name
        producer = lead.assigned_producer or "Jake Ferrara"
        producer_first = producer.split()[0]
        lob = opportunity.line_of_business or "insurance"
        tracking_code = f"SMS-{cadence_type.value[:3]}-{lead.applicant_id}-T{touch_number}"

        if cadence_type == CadenceType.INBOUND_LEAD:
            if touch_number == 0:
                text = (
                    f"Hi {first}, thanks for reaching out to StreetSmart Insurance! {producer_first} is preparing your {lob} "
                    f"quote. Call or text {cls.MAIN_PHONE} with questions. Reply STOP to opt out."
                )
            else:
                text = (
                    f"Hi {first}, Robie checking in from StreetSmart Insurance regarding your {lob} quote. "
                    f"Are you free for a quick chat with {producer_first}? Call {cls.MAIN_PHONE}. Reply STOP to opt out."
                )
        elif cadence_type == CadenceType.QUOTED_PROSPECT:
            text = (
                f"Hi {first}, {producer_first} sent over your {lob} proposal from StreetSmart Insurance! "
                f"Have 2 mins to review? Call us at {cls.MAIN_PHONE}. Reply STOP to opt out."
            )
        else:  # X-Date
            text = (
                f"Hi {first}, StreetSmart Insurance reminder: your {lob} policy renews soon. "
                f"Call {cls.MAIN_PHONE} for a free rate comparison. Reply STOP to opt out."
            )

        return SMSPackage(body_text=text, tracking_code=tracking_code)
