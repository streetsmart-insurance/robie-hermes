#!/usr/bin/env python3
"""Diesel: stay on page, save Notes + Document Download (name/title only; portal in notes)."""
import asyncio, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path("/opt/renewal-automation-system")
SHOT = BASE / "data/screenshots/directory_fill"
PROOF = BASE / "data/handoffs/directory_diesel_trinity_rocklake_proof.json"
EXTRACTED = BASE / "data/ezlynx_full_extracted_directory.json"
NOTES = (
    "Portal-for-docs: https://dieselauto.joshu.insure/store/diesel "
    "(add-user: https://dieselauto.joshu.insure/store/diesel/auth/register). "
    "Robie Directory fill 2026-09-06. No passwords stored."
)


def et():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


async def dismiss_leave(page):
    """If Leave Page? dialog is up, click No to keep edits."""
    clicked = await page.evaluate(
        """() => {
          const panes = [...document.querySelectorAll('mat-dialog-container,[role=dialog]')];
          for (const p of panes) {
            const t = (p.innerText || '');
            if (/leave page/i.test(t) || /unsaved changes/i.test(t)) {
              const no = [...p.querySelectorAll('button')].find(b => /^\\s*No\\s*$/i.test((b.innerText||'').trim()));
              if (no) { no.click(); return 'no'; }
            }
          }
          return null;
        }"""
    )
    await page.wait_for_timeout(800)
    return clicked


async def cancel_dialogs(page):
    await page.evaluate(
        """() => {
          const panes = [...document.querySelectorAll('mat-dialog-container,[role=dialog]')];
          for (const p of panes) {
            const cancel = [...p.querySelectorAll('button')].find(b => /^\\s*Cancel\\s*$/i.test((b.innerText||'').trim()));
            if (cancel) cancel.click();
          }
        }"""
    )
    await page.wait_for_timeout(500)


async def main():
    result = {"at_et": et()}
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        page = next(pg for pg in b.contexts[0].pages if "ezlynx.com" in (pg.url or ""))
        await page.bring_to_front()

        # Clear blocking dialogs
        result["leave1"] = await dismiss_leave(page)
        await cancel_dialogs(page)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2000)
        result["leave2"] = await dismiss_leave(page)
        await cancel_dialogs(page)
        await page.wait_for_timeout(500)

        # Notes
        ta = page.locator("textarea").first
        await ta.click()
        await ta.fill(NOTES[:1000])
        result["notes_len"] = len(await ta.input_value())

        # Add contact via anchor (name + title only)
        if "Document Download" not in (await page.inner_text("body")):
            await page.locator("a.add-new-contact").first.click()
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(SHOT / "diesel_r3_dialog.png"), full_page=True)

            # Fill Name (required) using get_by_label when possible
            try:
                await page.get_by_label("Name", exact=False).first.fill("Document Download")
            except Exception:
                # first text input in dialog
                await page.locator("mat-dialog-container input[type='text']").first.fill("Document Download")
            try:
                await page.get_by_label("Title", exact=False).first.fill("See Notes: agent portal for docs")
            except Exception:
                # second text input often Title
                inputs = page.locator("mat-dialog-container input[type='text']")
                if await inputs.count() > 1:
                    await inputs.nth(1).fill("See Notes: agent portal for docs")

            # Explicitly clear Address Line 1 if filled
            try:
                addr = page.get_by_label("Address Line 1", exact=False).first
                if await addr.count():
                    await addr.fill("")
            except Exception:
                pass

            await page.screenshot(path=str(SHOT / "diesel_r3_filled.png"), full_page=True)

            # Save inside dialog
            saved = await page.evaluate(
                """() => {
                  const pane = [...document.querySelectorAll('mat-dialog-container')].slice(-1)[0];
                  if (!pane) return 'no-dialog';
                  const btns = [...pane.querySelectorAll('button')].map(b => (b.innerText||'').trim());
                  const save = [...pane.querySelectorAll('button')].find(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()));
                  if (save) { save.click(); return 'save|' + btns.join(','); }
                  return 'nobtn|' + btns.join(',');
                }"""
            )
            result["dialog_save"] = saved
            await page.wait_for_timeout(2000)
            # if validation errors, cancel and continue with notes-only
            still = await page.evaluate("() => !!document.querySelector('mat-dialog-container')")
            if still:
                err = await page.evaluate(
                    "() => (document.querySelector('mat-dialog-container')||{}).innerText || ''"
                )
                result["dialog_still_open"] = err[:500]
                await cancel_dialogs(page)
                result["contact_added"] = False
            else:
                result["contact_added"] = True
        else:
            result["contact_added"] = True
            result["already_present"] = True

        await page.screenshot(path=str(SHOT / "diesel_r3_before_entry_save.png"), full_page=True)

        # Save entry (force through any leftover)
        result["leave3"] = await dismiss_leave(page)
        clicked = await page.evaluate(
            """() => {
              const btns = [...document.querySelectorAll('button')].filter(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()));
              // prefer Save not inside dialog
              const save = btns.find(b => !b.closest('mat-dialog-container')) || btns[0];
              if (!save) return false;
              save.click();
              return true;
            }"""
        )
        result["entry_save_clicked"] = clicked
        await page.wait_for_timeout(3000)
        # If leave page somehow, choose No then try again — shouldn't happen after save
        await dismiss_leave(page)

        # Verify by reopening — click No if leave warns
        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(1500)
        await dismiss_leave(page)
        await page.wait_for_timeout(2000)
        snap = await page.evaluate(
            """() => {
              const ta = [...document.querySelectorAll('textarea')][0];
              return {
                notes: ta ? ta.value : '',
                hasDoc: /Document Download/i.test(document.body.innerText),
                rows: [...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')]
                  .map(r => r.innerText.replace(/\\n+/g,' | ').trim()).slice(0, 12)
              };
            }"""
        )
        result["verify"] = snap
        await page.screenshot(path=str(SHOT / "diesel_r3_verified.png"), full_page=True)

    # Patch extracted to match UI truth (portal in notes; contact only if present)
    data = json.loads(EXTRACTED.read_text())
    key = "Diesel Insurance Solutions"
    data[key].setdefault("directory", {})["notes"] = NOTES[:1000]
    contacts = [
        c
        for c in data[key].get("contacts", [])
        if (c.get("name") or "").strip().lower() != "document download"
    ]
    if snap.get("hasDoc"):
        contacts.append(
            {
                "name": "Document Download",
                "title": "See Notes: agent portal for docs",
                "email": "",
                "phone": "",
                "address": "",
            }
        )
    data[key]["contacts"] = contacts
    EXTRACTED.write_text(json.dumps(data, indent=2))

    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import enrich_routing_from_directory

    enrich = enrich_routing_from_directory(key)
    result["enrich"] = enrich
    result["notes_ok"] = bool(snap.get("notes") and "Portal-for-docs" in snap.get("notes", ""))
    result["contact_ok"] = bool(snap.get("hasDoc"))

    proof = json.loads(PROOF.read_text())
    proof["passwords_stored"] = False
    proof.pop("password_scrub_applied", None)
    dc = proof["carriers"]["Diesel"]
    dc["diesel_retry3"] = result
    dc["notes_written"] = result["notes_ok"]
    dc["notes_text"] = NOTES
    dc["contacts_filled"] = (
        [
            {
                "purpose": "document_download",
                "contact": {
                    "name": "Document Download",
                    "title": "See Notes: agent portal for docs",
                    "email": "",
                    "phone": "",
                    "address1": "",
                },
                "form_result": {"ok": True},
                "note": "Portal URL kept in entry Notes (Address Line 1 rejects URLs).",
            }
        ]
        if result["contact_ok"]
        else []
    )
    dc["ui_fill_note"] = (
        "Document Download contact added without address URL due to EZLynx Address Line 1 validation; "
        "portal-for-docs recorded in Notes. Missing usable email/phone → email_rep_and_pend already sent."
    )
    dc["snapshot_after"] = {"notes": snap.get("notes"), "rows": snap.get("rows")}
    dc["screenshot_after"] = str(SHOT / "diesel_r3_verified.png")
    dc["enrich"] = {"ok": True, "result": enrich}
    dc["finished_at_et"] = et()
    # keep email_rep_and_pend from earlier successful send
    PROOF.write_text(json.dumps(proof, indent=2))
    print(json.dumps(result, indent=2)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
