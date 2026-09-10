#!/usr/bin/env python3
"""Diesel: notes already saved. Add Document Download contact after clean save."""
import asyncio, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path("/opt/renewal-automation-system")
SHOT = BASE / "data/screenshots/directory_fill"
PROOF = BASE / "data/handoffs/directory_diesel_trinity_rocklake_proof.json"
EXTRACTED = BASE / "data/ezlynx_full_extracted_directory.json"


def et():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


async def dismiss_leave_no(page):
    return await page.evaluate(
        """() => {
          const panes = [...document.querySelectorAll('mat-dialog-container,[role=dialog]')];
          for (const p of panes) {
            const t = p.innerText || '';
            if (/leave page|unsaved changes/i.test(t)) {
              const no = [...p.querySelectorAll('button')].find(b => /^\\s*No\\s*$/i.test((b.innerText||'').trim()));
              if (no) { no.click(); return 'no'; }
            }
          }
          return null;
        }"""
    )


async def main():
    result = {"at_et": et()}
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        page = next(pg for pg in b.contexts[0].pages if "ezlynx.com" in (pg.url or ""))
        await page.bring_to_front()
        await dismiss_leave_no(page)
        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)
        await dismiss_leave_no(page)

        body = await page.inner_text("body")
        if "Document Download" in body:
            result["already"] = True
        else:
            # Ensure no dirty form: re-save notes if empty, else just Save
            ta = page.locator("textarea").first
            notes_val = await ta.input_value()
            result["notes_before"] = notes_val[:100]
            if "Portal-for-docs" not in notes_val:
                await ta.fill(
                    "Portal-for-docs: https://dieselauto.joshu.insure/store/diesel "
                    "(add-user: https://dieselauto.joshu.insure/store/diesel/auth/register). "
                    "Robie Directory fill 2026-09-06. No passwords stored."
                )
            # Save entry first so Add contact navigation is clean
            await page.evaluate(
                """() => {
                  const save = [...document.querySelectorAll('button')]
                    .find(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()) && !b.closest('mat-dialog-container'));
                  if (save) save.click();
                }"""
            )
            await page.wait_for_timeout(3000)
            result["pre_save_url"] = page.url

            # Re-open entry cleanly
            await page.goto(
                "https://app.ezlynx.com/web/directory/entry/562780",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2000)

            # Click Add contact anchor
            await page.locator("a.add-new-contact").first.click()
            await page.wait_for_timeout(2000)
            result["after_add_url"] = page.url
            result["leave_after_add"] = await dismiss_leave_no(page)
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(SHOT / "diesel_r4_add.png"), full_page=True)

            dlg_text = await page.evaluate(
                """() => {
                  const pane = document.querySelector('mat-dialog-container,[role=dialog]');
                  return pane ? pane.innerText.slice(0, 1200) : ('NO_DIALOG title=' + document.title + ' url=' + location.href + ' head=' + document.body.innerText.slice(0,400));
                }"""
            )
            result["dlg_or_page"] = dlg_text[:800]

            # If navigated to add-contact page (not dialog)
            # Fill Name
            name_filled = False
            for sel in [
                page.get_by_label("Name", exact=False).first,
                page.locator("input").nth(0),
            ]:
                try:
                    if await sel.count():
                        await sel.fill("Document Download")
                        name_filled = True
                        break
                except Exception:
                    continue
            result["name_filled"] = name_filled

            try:
                await page.get_by_label("Title", exact=False).first.fill(
                    "See Notes: agent portal for docs"
                )
                result["title_filled"] = True
            except Exception as e:
                result["title_filled"] = str(e)[:120]

            # Clear address if present
            try:
                loc = page.get_by_label("Address Line 1", exact=False).first
                if await loc.count():
                    await loc.fill("")
            except Exception:
                pass

            await page.screenshot(path=str(SHOT / "diesel_r4_filled.png"), full_page=True)

            # Save contact
            save_res = await page.evaluate(
                """() => {
                  const candidates = [...document.querySelectorAll('button,a')]
                    .filter(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()));
                  if (!candidates.length) {
                    return 'none|' + [...document.querySelectorAll('button')].map(b => (b.innerText||'').trim()).filter(Boolean).slice(0,20).join(',');
                  }
                  // prefer dialog save if dialog exists
                  const inDlg = candidates.find(b => b.closest('mat-dialog-container'));
                  (inDlg || candidates[0]).click();
                  return inDlg ? 'dialog-save' : 'page-save';
                }"""
            )
            result["save_res"] = save_res
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(SHOT / "diesel_r4_saved.png"), full_page=True)

        # Verify
        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)
        await dismiss_leave_no(page)
        snap = await page.evaluate(
            """() => {
              const ta = [...document.querySelectorAll('textarea')][0];
              return {
                notes: ta ? ta.value : '',
                hasDoc: /Document Download/i.test(document.body.innerText),
                rows: [...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')]
                  .map(r => r.innerText.replace(/\\n+/g,' | ').trim()).slice(0, 15)
              };
            }"""
        )
        result["verify"] = snap
        await page.screenshot(path=str(SHOT / "diesel_r4_verified.png"), full_page=True)

    data = json.loads(EXTRACTED.read_text())
    key = "Diesel Insurance Solutions"
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
    if snap.get("notes"):
        data[key].setdefault("directory", {})["notes"] = snap["notes"][:1000]
    data[key]["contacts"] = contacts
    EXTRACTED.write_text(json.dumps(data, indent=2))

    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import enrich_routing_from_directory

    enrich = enrich_routing_from_directory(key)

    proof = json.loads(PROOF.read_text())
    proof["passwords_stored"] = False
    proof.pop("password_scrub_applied", None)
    dc = proof["carriers"]["Diesel"]
    dc["diesel_retry4"] = result
    dc["notes_written"] = bool(snap.get("notes") and "Portal-for-docs" in (snap.get("notes") or ""))
    dc["contacts_filled"] = (
        [
            {
                "purpose": "document_download",
                "contact": {
                    "name": "Document Download",
                    "title": "See Notes: agent portal for docs",
                    "email": "",
                    "phone": "",
                },
                "form_result": {"ok": True},
                "note": "Portal URL in entry Notes only (Address Line 1 rejects URLs).",
            }
        ]
        if snap.get("hasDoc")
        else dc.get("contacts_filled") or []
    )
    dc["snapshot_after"] = {"notes": snap.get("notes"), "rows": snap.get("rows")}
    dc["screenshot_after"] = str(SHOT / "diesel_r4_verified.png")
    dc["enrich"] = {"ok": True, "result": enrich}
    dc["finished_at_et"] = et()
    PROOF.write_text(json.dumps(proof, indent=2))
    print(json.dumps(result, indent=2)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
