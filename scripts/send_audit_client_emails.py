#!/usr/bin/env python3
"""CLI runner for sending hardened client audit emails via EZLynx over Chrome CDP.

Ensures that every audit email dispatched has its carrier documents attached,
validates template appropriateness against carrier status, and prevents bare emails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from src.ezlynx.audit_email_dispatcher import (
    AuditEmailDispatcher,
    AuditAttachmentRequiredError,
    AuditTemplateMismatchError,
    AuditSenderSecurityError,
    AuditSubjectMissingMetadataError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("send_audit_client_emails")


async def run_single(
    applicant_id: str,
    to_email: str,
    cc_emails: list[str],
    template_name: str,
    carrier_status: str,
    search_terms: list[str],
    dry_run: bool = False,
    enforce_attachments: bool = True,
    custom_subject: str | None = None,
    custom_body: str | None = None,
    insured_name: str | None = None,
    policy_number: str | None = None,
):
    dispatcher = AuditEmailDispatcher()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = await context.new_page()
        try:
            if dry_run:
                logger.info(f"[DRY-RUN] Validating compose for {applicant_id} -> {to_email}...")
                dispatcher.validate_template_for_status(carrier_status, template_name)
                url = dispatcher.COMPOSE_URL.format(applicant_id=applicant_id)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                await dispatcher.dismiss_overlays(page)
                await page.locator("#subject").wait_for(state="visible", timeout=30000)
                from_id = await dispatcher.ensure_from_robie(page)
                logger.info(f"[DRY-RUN] Verified From identity: {from_id}")
                await dispatcher.set_to_email(page, to_email)
                await dispatcher.set_cc_emails(page, cc_emails)
                await dispatcher.select_template(page, template_name)
                # Resolve insured name & policy number and enforce in subject line
                res_insured, res_policy = await dispatcher.resolve_insured_and_policy(
                    applicant_id=applicant_id,
                    insured_name=insured_name,
                    policy_number=policy_number,
                    page=page,
                )
                base_subj = custom_subject if custom_subject else await page.locator("#subject").input_value()
                final_subj = dispatcher.format_subject_with_insured_and_policy(
                    subject=base_subj,
                    insured_name=res_insured,
                    policy_number=res_policy,
                )
                subj_input = page.locator("#subject")
                await subj_input.fill("")
                await subj_input.fill(final_subj)
                logger.info(f"[DRY-RUN] Hardened subject line: '{final_subj}' (Insured: {res_insured}, Policy: {res_policy})")
                attached = await dispatcher.attach_documents(page, search_terms)
                count = await dispatcher.count_attached_documents(page)
                logger.info(f"[DRY-RUN] Attached matching docs: {attached} (DOM count: {count})")
                if enforce_attachments and count == 0 and len(attached) == 0:
                    raise AuditAttachmentRequiredError(
                        f"CRITICAL GATE: 0 documents attached for {applicant_id}."
                    )
                logger.info(f"[DRY-RUN] SUCCESS: Preflight passed for {applicant_id}")
                return {"applicant_id": applicant_id, "dry_run": True, "subject": final_subj, "insured_name": res_insured, "policy_number": res_policy, "attached": attached, "count": count}
            else:
                res = await dispatcher.send_audit_email(
                    page=page,
                    applicant_id=applicant_id,
                    to_email=to_email,
                    cc_emails=cc_emails,
                    template_name=template_name,
                    carrier_status=carrier_status,
                    search_terms=search_terms,
                    enforce_attachments=enforce_attachments,
                    custom_subject=custom_subject,
                    custom_body=custom_body,
                    insured_name=insured_name,
                    policy_number=policy_number,
                )
                logger.info(f"Successfully sent audit email for {applicant_id} with {res.get('verified_attachment_count')} attachments.")
                return res
        finally:
            await page.close()


def main():
    parser = argparse.ArgumentParser(description="Send hardened audit client emails via EZLynx CDP")
    parser.add_argument("--applicant", required=True, help="Applicant ID")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--cc", nargs="*", default=[], help="CC emails")
    parser.add_argument("--template", required=True, help="EZLynx email template name")
    parser.add_argument("--status", default="pending_client", help="Carrier audit status (e.g. pending_client, completed)")
    parser.add_argument("--search-terms", nargs="+", default=["audit"], help="Terms to search in document library to attach")
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight without sending")
    parser.add_argument("--allow-unattached", action="store_true", help="Bypass attachment requirement (NOT recommended)")
    parser.add_argument("--subject", help="Custom email subject")
    parser.add_argument("--body", help="Custom email body")
    parser.add_argument("--insured-name", help="Named insured on the policy")
    parser.add_argument("--policy-number", help="Policy number")

    args = parser.parse_args()

    enforce = not args.allow_unattached
    try:
        res = asyncio.run(
            run_single(
                applicant_id=args.applicant,
                to_email=args.to,
                cc_emails=args.cc,
                template_name=args.template,
                carrier_status=args.status,
                search_terms=args.search_terms,
                dry_run=args.dry_run,
                enforce_attachments=enforce,
                custom_subject=args.subject,
                custom_body=args.body,
                insured_name=args.insured_name,
                policy_number=args.policy_number,
            )
        )
        print(json.dumps(res, indent=2))
    except Exception as e:
        logger.error(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
