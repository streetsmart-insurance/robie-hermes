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
                attached = await dispatcher.attach_documents(page, search_terms)
                count = await dispatcher.count_attached_documents(page)
                logger.info(f"[DRY-RUN] Attached matching docs: {attached} (DOM count: {count})")
                if enforce_attachments and count == 0 and len(attached) == 0:
                    raise AuditAttachmentRequiredError(
                        f"CRITICAL GATE: 0 documents attached for {applicant_id}."
                    )
                logger.info(f"[DRY-RUN] SUCCESS: Preflight passed for {applicant_id}")
                return {"applicant_id": applicant_id, "dry_run": True, "attached": attached, "count": count}
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
            )
        )
        print(json.dumps(res, indent=2))
    except Exception as e:
        logger.error(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
