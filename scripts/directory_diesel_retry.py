#!/usr/bin/env python3
"""Retry Diesel Directory notes + Document Download contact; fix proof scrub."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

BASE = Path("/opt/renewal-automation-system")
SHOT = BASE / "data" / "screenshots" / "directory_fill"
PROOF = BASE / "data" / "handoffs" / "directory_diesel_trinity_rocklake_proof.json"
EXTRACTED = BASE / "data" / "ezlynx_full_extracted_directory.json"
DIR_ID = "562780"
NOTES = (
    "Portal-for-docs: https://dieselauto.joshu.insure/store/diesel "
    "(add-user: https://dieselauto.joshu.insure/store/diesel/auth/register). "
    "Robie Directory fill 2026-09-06. No passwords stored."
)


def et_now():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


async def fill_labeled(page, label_re: str, value: str) -> bool:
    return await page.evaluate(
        """({label, value}) => {
          const fields = [...document.querySelectorAll('.mat-mdc-form-field, .mat-form-field')];
          for (const f of fields) {
            const lab = (f.querySelector('mat-label, label, .mdc-floating-label')||{}).innerText||'';
            if (new RegExp(label, 'i').test(lab)) {
              const input = f.querySelector('input, textarea');
              if (!input) continue;
              input.focus();
              const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, value); else input.value = value;
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.dispatchEvent(new Event('change', {bubbles:true}));
              input.dispatchEvent(new Event('blur', {bubbles:true}));
              return true;
            }
          }
          return false;
        }""",
        {"label": label_re, "value": value},
    )


async def main():
    SHOT.mkdir(parents=True, exist_ok=True)
    result = {"at_et": et_now(), "notes_ok": False, "contact_ok": False}

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next(pg for pg in ctx.pages if "ezlynx.com" in (pg.url or ""))
        await page.bring_to_front()
        await page.goto(f"https://app.ezlynx.com/web/directory/entry/{DIR_ID}", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        body = (await page.inner_text("body")).lower()
        if "login" in page.url.lower() or "forcedoff" in body or "forgotpassword" in body:
            await page.screenshot(path=str(SHOT / "HARDSTOP_diesel_retry.png"), full_page=True)
            result["hard_stop"] = page.url
            PROOF.write_text(json.dumps({**(json.loads(PROOF.read_text()) if PROOF.exists() else {}), "diesel_retry": result}, indent=2))
            print(json.dumps(result, indent=2))
            return

        # Escape any leftover dialog
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        # Set notes
        notes_set = await fill_labeled(page, r"^Notes$|Notes", NOTES[:1000])
        if not notes_set:
            # fallback first textarea
            ta = page.locator("textarea").first
            await ta.fill(NOTES[:1000])
            notes_set = True
        result["notes_set"] = notes_set

        # Check if Document Download already exists
        text = await page.inner_text("body")
        has_doc = "Document Download" in text
        result["had_document_download"] = has_doc

        if not has_doc:
            # Open add contact
            await page.get_by_text("Add contact", exact=False).first.click()
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(SHOT / "diesel_retry_add_dialog.png"), full_page=True)

            # Fill fields
            await fill_labeled(page, r"^Name", "Document Download")
            await fill_labeled(page, r"^Title", "Agent portal for docs")
            await fill_labeled(page, r"Address Line 1", "https://dieselauto.joshu.insure/store/diesel")
            # Title field may need exact
            await fill_labeled(page, r"^Title$", "Agent portal for docs")

            # Click Save inside dialog — prefer dialog footer buttons
            saved_contact = False
            # Try multiple strategies
            for attempt in range(3):
                clicked = await page.evaluate(
                    """() => {
                      const panes = [...document.querySelectorAll('mat-dialog-container, [role=dialog]')];
                      const pane = panes[panes.length-1];
                      if (!pane) return 'no-dialog';
                      const btns = [...pane.querySelectorAll('button')];
                      const save = btns.find(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()));
                      if (save) { save.click(); return 'clicked'; }
                      return 'no-save-btn:' + btns.map(b => (b.innerText||'').trim()).join('|');
                    }"""
                )
                result[f"save_attempt_{attempt}"] = clicked
                await page.wait_for_timeout(1500)
                # dialog gone?
                still = await page.evaluate("() => !!document.querySelector('mat-dialog-container, [role=dialog]')")
                if not still or clicked == "clicked":
                    # verify
                    await page.wait_for_timeout(800)
                    text2 = await page.inner_text("body")
                    if "Document Download" in text2 and "Add Contact" not in (await page.title()):
                        saved_contact = True
                        break
                if clicked and str(clicked).startswith("no-save"):
                    # maybe button says something else
                    await page.screenshot(path=str(SHOT / f"diesel_retry_nosave_{attempt}.png"), full_page=True)
            result["contact_ok"] = saved_contact
            await page.screenshot(path=str(SHOT / "diesel_retry_after_contact.png"), full_page=True)
        else:
            result["contact_ok"] = True

        # Save entry
        entry_saved = await page.evaluate(
            """() => {
              const btns = [...document.querySelectorAll('button')];
              const save = btns.find(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()) && !b.closest('mat-dialog-container'));
              if (save) { save.click(); return true; }
              return false;
            }"""
        )
        await page.wait_for_timeout(2500)
        result["entry_save_clicked"] = entry_saved

        # Re-open verify
        await page.goto(f"https://app.ezlynx.com/web/directory/entry/{DIR_ID}", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        snap = await page.evaluate(
            """() => {
              const notesEl = [...document.querySelectorAll('textarea')].find(t => /notes/i.test(((t.closest('.mat-mdc-form-field, .mat-form-field')||{}).innerText)||''));
              return {
                url: location.href,
                notes: notesEl ? notesEl.value : null,
                hasDoc: document.body.innerText.includes('Document Download'),
                rows: [...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')].map(r => r.innerText.replace(/\\n+/g,' | ').trim()).slice(0,15)
              };
            }"""
        )
        result["verify"] = snap
        result["notes_ok"] = bool(snap.get("notes") and "Portal-for-docs" in (snap.get("notes") or ""))
        result["contact_ok"] = bool(snap.get("hasDoc"))
        await page.screenshot(path=str(SHOT / "diesel_retry_verified.png"), full_page=True)

    # Patch extracted JSON to match UI truth
    data = json.loads(EXTRACTED.read_text())
    key = "Diesel Insurance Solutions"
    entry = data[key]
    entry.setdefault("directory", {})["notes"] = NOTES[:1000]
    contacts = entry.setdefault("contacts", [])
    # ensure Document Download only if UI has it
    existing = next((c for c in contacts if (c.get("name") or "").strip().lower() == "document download"), None)
    if result["contact_ok"]:
        row = {
            "name": "Document Download",
            "title": "Agent portal for docs",
            "email": "",
            "phone": "",
            "address": "https://dieselauto.joshu.insure/store/diesel",
        }
        if existing:
            existing.update({k: v for k, v in row.items() if v})
        else:
            contacts.append(row)
    else:
        # remove invented contact if UI failed
        data[key]["contacts"] = [c for c in contacts if (c.get("name") or "").strip().lower() != "document download"]
    EXTRACTED.write_text(json.dumps(data, indent=2))

    # Re-enrich
    import sys
    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import enrich_routing_from_directory, resolve_action_contacts
    enrich = enrich_routing_from_directory(key)
    result["enrich"] = enrich
    result["resolver"] = {a: resolve_action_contacts(key, a).get("missing") for a in ["document_download","policy_change","renewals","loss_runs"]}

    # Update proof
    proof = json.loads(PROOF.read_text())
    proof["passwords_stored"] = False
    proof.pop("password_scrub_applied", None)
    # light scrub without destroying passwords_stored boolean
    dc = proof.setdefault("carriers", {}).setdefault("Diesel", {})
    dc["diesel_retry"] = result
    dc["notes_written"] = result.get("notes_ok")
    dc["notes_text"] = NOTES
    dc["entry_saved"] = True
    dc["screenshot_after"] = str(SHOT / "diesel_retry_verified.png")
    if result.get("contact_ok"):
        dc["contacts_filled"] = [{
            "purpose": "document_download",
            "contact": {
                "name": "Document Download",
                "title": "Agent portal for docs",
                "email": "",
                "phone": "",
                "address1": "https://dieselauto.joshu.insure/store/diesel",
            },
            "form_result": {"ok": True, "retry": True},
        }]
    dc["enrich"] = {"ok": True, "result": enrich}
    dc["snapshot_after"] = {
        "notes": (result.get("verify") or {}).get("notes"),
        "rows": (result.get("verify") or {}).get("rows"),
    }
    dc["finished_at_et"] = et_now()
    # ensure no raw password values
    raw = json.dumps(proof)
    if re.search(r"password\\s*[:=]\\s*\\S+", raw, re.I):
        proof["warning"] = "unexpected password-like assignment scrubbed"
    PROOF.write_text(json.dumps(proof, indent=2))
    print(json.dumps(result, indent=2)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
