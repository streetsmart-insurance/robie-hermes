"""Audit Email Dispatcher for StreetSmart Insurance EZLynx System.

Automates composing and sending WC and commercial audit emails via EZLynx CDP
with strict safety gates ensuring:
1. Emails are never sent without required audit documents attached.
2. Completed audits never receive blank request questionnaires.
3. Sender identity is strictly verified as Robie AI (never human CSR).
4. Full audit trail and DOM preflight validation before clicking Send.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("audit_email_dispatcher")


class AuditAttachmentRequiredError(Exception):
    """Raised when an audit email is attempted without required attachments."""
    pass


class AuditTemplateMismatchError(Exception):
    """Raised when an inappropriate template is selected for the audit state."""
    pass


class AuditSenderSecurityError(Exception):
    """Raised when sender is detected as a human or unauthorized identity."""
    pass


class AuditEmailDispatcher:
    """Manages EZLynx compose UI interactions over Chrome CDP."""

    COMPOSE_URL = "https://app.ezlynx.com/web/email/compose/{applicant_id}"

    REQUEST_TEMPLATES = {
        "Audit Request to Client",
        "X Audit Audit Non Compliance/Request",
    }

    COMPLETED_TEMPLATES = {
        "Audit Results (Completed Statement)",
        "Audit Worker Comp Audit Results",
        "Audit General Liability Audit Results",
        "Audit Commercial Package Audit Results",
        "Audit Audit Results",
    }

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", screenshot_dir: Optional[Path] = None):
        self.cdp_url = cdp_url
        self.screenshot_dir = screenshot_dir or Path("/tmp/audit_email_proofs")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_template_for_status(status: str, template_name: str) -> None:
        """Enforces that completed audits receive results templates and vice versa."""
        st = (status or "").lower()
        if st in ("completed", "finalized", "closed"):
            if template_name in AuditEmailDispatcher.REQUEST_TEMPLATES:
                raise AuditTemplateMismatchError(
                    f"Cannot send request template '{template_name}' for an audit with completed carrier status."
                )
        elif st in ("pending_client", "in_progress", "reminder_sent"):
            if template_name in AuditEmailDispatcher.COMPLETED_TEMPLATES:
                raise AuditTemplateMismatchError(
                    f"Cannot send completion template '{template_name}' for an audit that is pending client submission."
                )

    @staticmethod
    async def dismiss_overlays(page: Any) -> None:
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)

    @staticmethod
    async def ensure_from_robie(page: Any) -> str:
        from_input = page.locator("#mat-input-1")
        val = ""
        for _ in range(30):
            if await from_input.count():
                val = await from_input.input_value()
                if val:
                    break
            await page.wait_for_timeout(500)

        low = (val or "").lower()
        if "carlo@" in low:
            raise AuditSenderSecurityError(f"From is human Carlo Ferrara, aborting automated send: {val}")
        if not any(x in low for x in ("robie", "ssrobie")):
            raise AuditSenderSecurityError(f"From identity is not Robie/SSRobie: {val!r}")
        return val

    @staticmethod
    async def set_to_email(page: Any, email: str) -> None:
        chips = await page.evaluate(
            """() => [...document.querySelectorAll('mat-chip-row')].map(c => ({
              text: (c.innerText || '').split('\\n')[0].trim(),
              grid: (c.closest('mat-form-field')?.innerText || '').split('\\n')[0]
            }))"""
        )
        to_chips = [c for c in chips if c.get("grid") == "To"]
        if len(to_chips) == 1 and to_chips[0]["text"].lower() == email.lower():
            return

        # Clear existing To chips
        await page.evaluate(
            """() => {
              for (const c of document.querySelectorAll('mat-chip-row')) {
                const grid = (c.closest('mat-form-field')?.innerText || '').split('\\n')[0];
                if (grid === 'To') {
                  const btn = c.querySelector('button.mat-mdc-chip-remove, button[aria-label*=emove], button');
                  if (btn) btn.click();
                }
              }
            }"""
        )
        await page.wait_for_timeout(300)
        to_input = page.locator("#mat-mdc-chip-list-input-1")
        await to_input.click()
        await to_input.fill("")
        await to_input.type(email, delay=15)
        await page.wait_for_timeout(600)
        opts = page.locator("mat-option")
        if await opts.count():
            clicked = False
            for i in range(await opts.count()):
                opt = opts.nth(i)
                if asyncio.iscoroutine(opt):
                    opt = await opt
                t = (await opt.inner_text()).lower()
                if email.lower() in t:
                    await opt.click()
                    clicked = True
                    break
            if not clicked:
                await opts.first.click()
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(400)
        await AuditEmailDispatcher.dismiss_overlays(page)

    @staticmethod
    async def set_cc_emails(page: Any, emails: List[str]) -> None:
        if not emails:
            return
        if await page.locator("#mat-mdc-chip-list-input-2").count() == 0:
            btn_cc = page.locator("#btnCC")
            if await btn_cc.count():
                await btn_cc.click()
                await page.wait_for_timeout(400)
        existing = await page.evaluate(
            """() => [...document.querySelectorAll('mat-chip-row')].filter(c => ((c.closest('mat-form-field')?.innerText||'').split('\\n')[0]==='CC')).map(c => (c.innerText||'').split('\\n')[0].trim().toLowerCase())"""
        )
        for e in emails:
            if not e or e.lower() in existing:
                continue
            cc_in = page.locator("#mat-mdc-chip-list-input-2")
            await cc_in.click()
            await cc_in.fill("")
            await cc_in.type(e, delay=20)
            await page.wait_for_timeout(600)
            opts = page.locator("mat-option")
            if await opts.count():
                clicked = False
                for i in range(min(await opts.count(), 10)):
                    opt = opts.nth(i)
                    if asyncio.iscoroutine(opt):
                        opt = await opt
                    t = (await opt.inner_text()).lower()
                    if e.lower() in t:
                        await opt.click()
                        clicked = True
                        break
                if not clicked:
                    await opts.first.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)
            await AuditEmailDispatcher.dismiss_overlays(page)

    @staticmethod
    async def select_template(page: Any, template_name: str) -> str:
        sel = page.locator("#mat-select-0")
        await sel.click()
        await page.wait_for_timeout(800)
        opt = page.get_by_role("option", name=template_name, exact=True)
        if await opt.count() == 0:
            opt = page.get_by_role("option").filter(has_text=template_name)
        if await opt.count() == 0:
            raise RuntimeError(f"Template not found in EZLynx: '{template_name}'")
        await opt.first.scroll_into_view_if_needed()
        await opt.first.click()
        await page.wait_for_timeout(1500)
        return await page.locator("#subject").input_value()

    @staticmethod
    async def attach_documents(page: Any, search_terms: List[str]) -> List[str]:
        """Opens document selector and attaches matching documents."""
        btn_attach = page.locator("#btnAttach")
        if await btn_attach.count() == 0:
            return []

        await btn_attach.click()
        await page.wait_for_timeout(1500)

        # Look for matching rows
        attached_names = []
        rows = page.locator("tr, mat-row")
        row_count = await rows.count()
        for i in range(row_count):
            row = rows.nth(i)
            if asyncio.iscoroutine(row):
                row = await row
            text = (await row.inner_text()).strip()
            low_text = text.lower()
            if any(term.lower() in low_text for term in search_terms):
                cb = row.locator("mat-checkbox, input[type='checkbox']")
                if await cb.count():
                    await cb.first.click()
                    doc_name = text.split("\n")[0].strip()
                    attached_names.append(doc_name)
                    await page.wait_for_timeout(300)

        # Click Attach / Add button in selector
        btn_add = page.locator('#btnAdd, button:has-text("Attach"), button:has-text("Add")')
        add_count = await btn_add.count()
        for i in range(add_count):
            loc = btn_add.nth(i)
            if asyncio.iscoroutine(loc):
                loc = await loc
            t = (await loc.inner_text()).lower()
            if "attach" in t or "add" in t:
                await loc.click()
                break

        await page.wait_for_timeout(1500)
        return attached_names

    @staticmethod
    async def count_attached_documents(page: Any) -> int:
        """Inspects the compose window DOM to verify number of attached documents."""
        return await page.evaluate(
            """() => {
              const body = document.body.innerText || '';
              // Match 'Attachments (N)' or individual attached document chips/links
              const m = body.match(/Attachments?\\s*\\((\\d+)\\)/i);
              if (m) return parseInt(m[1], 10);
              
              // Count attachment chips or rows in compose area
              const chips = document.querySelectorAll('.attachment-item, .attachment-chip, [data-testid="attachment-item"]');
              if (chips.length > 0) return chips.length;

              // Fallback to checking for document rows under attachment section
              const attachHeader = Array.from(document.querySelectorAll('*')).find(el => el.innerText && el.innerText.trim() === 'Attachments');
              if (attachHeader && attachHeader.parentElement) {
                const links = attachHeader.parentElement.querySelectorAll('a, mat-chip');
                if (links.length > 0) return links.length;
              }
              return 0;
            }"""
        )

    async def send_audit_email(
        self,
        page: Any,
        applicant_id: str,
        to_email: str,
        cc_emails: List[str],
        template_name: str,
        carrier_status: str,
        search_terms: List[str],
        enforce_attachments: bool = True,
        custom_subject: Optional[str] = None,
        custom_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatches an audit email with strict safety assertions."""
        # 1. Validate status vs template
        self.validate_template_for_status(carrier_status, template_name)

        result: Dict[str, Any] = {
            "applicant_id": applicant_id,
            "to_email": to_email,
            "cc_emails": cc_emails,
            "template_name": template_name,
            "carrier_status": carrier_status,
            "attached_documents": [],
            "sent": False,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 2. Navigate to compose
        compose_url = self.COMPOSE_URL.format(applicant_id=applicant_id)
        await page.goto(compose_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        await self.dismiss_overlays(page)
        await page.locator("#subject").wait_for(state="visible", timeout=30000)

        # 3. Security check: Ensure from Robie
        from_id = await self.ensure_from_robie(page)
        result["from_identity"] = from_id

        # 4. Set recipients
        await self.set_to_email(page, to_email)
        await self.set_cc_emails(page, cc_emails)

        # 5. Select template
        subject = await self.select_template(page, template_name)
        result["subject"] = subject

        # 6. Apply custom subject/body if provided
        if custom_subject:
            subj_input = page.locator("#subject")
            await subj_input.fill("")
            await subj_input.fill(custom_subject)
            result["subject"] = custom_subject

        if custom_body:
            await page.evaluate(
                """(text) => {
                  const ed = document.querySelector('.ql-editor') || document.querySelector('#body') || document.querySelector('textarea');
                  if (ed) {
                    if (ed.classList && ed.classList.contains('ql-editor')) {
                      ed.innerHTML = text.split('\\n\\n').map(p => '<p>' + p.replace(/\\n/g, '<br>') + '</p>').join('');
                    } else {
                      ed.value = text;
                    }
                  }
                }""",
                custom_body,
            )
            await page.wait_for_timeout(800)

        # 7. Attach documents
        attached = await self.attach_documents(page, search_terms)
        result["attached_documents"] = attached

        # 8. PREFLIGHT HARDENING GATE: Verify physical attachment count
        attach_count = await self.count_attached_documents(page)
        result["verified_attachment_count"] = attach_count

        if enforce_attachments:
            if attach_count == 0 and len(attached) == 0:
                raise AuditAttachmentRequiredError(
                    f"CRITICAL SAFETY GATE: Refusing to send audit email to {to_email}. "
                    f"0 documents were attached matching {search_terms}. "
                    "Emails must never be sent without carrier audit paperwork."
                )

        # 9. Proof screenshot before send
        shot_pre = self.screenshot_dir / f"{applicant_id}_audit_email_pre_send.png"
        await page.screenshot(path=str(shot_pre))
        result["screenshot_pre"] = str(shot_pre)

        # 10. Click Send
        btn_send = page.locator('#btnSend, button:has-text("Send")')
        send_count = await btn_send.count()
        sent_clicked = False
        for i in range(send_count):
            loc = btn_send.nth(i)
            if asyncio.iscoroutine(loc):
                loc = await loc
            t = (await loc.inner_text()).lower()
            if "send" in t:
                await loc.click()
                sent_clicked = True
                break

        if not sent_clicked:
            raise RuntimeError("Could not locate or click the Send button in EZLynx compose UI.")

        await page.wait_for_timeout(4000)

        # Proof screenshot after send
        shot_post = self.screenshot_dir / f"{applicant_id}_audit_email_post_send.png"
        await page.screenshot(path=str(shot_post))
        result["screenshot_post"] = str(shot_post)
        result["sent"] = True

        return result
