#!/usr/bin/env python3
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


async def fill_field(page, label, value):
    return await page.evaluate(
        """({label, value}) => {
          const fields = [...document.querySelectorAll('.mat-mdc-form-field, .mat-form-field')];
          for (const f of fields) {
            const lab = ((f.querySelector('mat-label,label,.mdc-floating-label')||{}).innerText||'').trim();
            if (new RegExp(label, 'i').test(lab)) {
              const input = f.querySelector('input,textarea');
              if (!input) continue;
              input.focus();
              const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, value); else input.value = value;
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.dispatchEvent(new Event('change', {bubbles:true}));
              input.dispatchEvent(new Event('blur', {bubbles:true}));
              return lab;
            }
          }
          return null;
        }""",
        {"label": label, "value": value},
    )


async def main():
    result = {"at_et": et()}
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        page = next(pg for pg in b.contexts[0].pages if "ezlynx.com" in (pg.url or ""))
        await page.bring_to_front()
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)

        ta = page.locator("textarea").first
        await ta.click()
        await ta.fill("")
        await ta.type(NOTES[:1000], delay=5)
        await page.wait_for_timeout(300)
        cur = await ta.input_value()
        result["notes_typed_len"] = len(cur)
        result["notes_preview"] = cur[:120]

        await page.locator("a.add-new-contact").first.click()
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(SHOT / "diesel_add_anchor_dialog.png"), full_page=True)
        dlg = await page.evaluate(
            """() => [...document.querySelectorAll('mat-dialog-container,[role=dialog]')].map(p => ({
              text: p.innerText.slice(0, 1500),
              buttons: [...p.querySelectorAll('button,a')].map(b => (b.innerText||'').trim())
            }))"""
        )
        result["dialog"] = dlg

        fills = []
        for lab, val in [
            (r"Name", "Document Download"),
            (r"^Title", "Agent portal for docs"),
            (r"Address Line 1", "https://dieselauto.joshu.insure/store/diesel"),
        ]:
            fills.append({lab: await fill_field(page, lab, val)})
        result["fills"] = fills

        save_clicked = await page.evaluate(
            """() => {
              const pane = [...document.querySelectorAll('mat-dialog-container,[role=dialog]')].slice(-1)[0];
              if (!pane) return 'no-dialog';
              const els = [...pane.querySelectorAll('button,a')];
              const save = els.find(b => /^\\s*Save\\s*$/i.test((b.innerText||'').trim()));
              if (save) { save.click(); return 'save'; }
              return 'btns:' + els.map(b => (b.innerText||'').trim()).join('|');
            }"""
        )
        result["dialog_save"] = save_clicked
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(SHOT / "diesel_after_dialog_save.png"), full_page=True)

        # If still on dialog with Save visible use get_by_role
        if save_clicked != "save":
            try:
                await page.get_by_role("button", name="Save").last.click(timeout=5000)
                result["dialog_save_fallback"] = "role_save"
                await page.wait_for_timeout(1500)
            except Exception as e:
                result["dialog_save_fallback"] = str(e)[:200]

        await page.locator("button:has-text('Save')").first.click()
        await page.wait_for_timeout(2500)
        yn = await page.evaluate(
            """() => {
              const pane = [...document.querySelectorAll('mat-dialog-container,[role=dialog]')].slice(-1)[0];
              if (!pane) return null;
              const t = pane.innerText || '';
              const yes = [...pane.querySelectorAll('button')].find(b => /^\\s*Yes\\s*$/i.test((b.innerText||'').trim()));
              if (yes) { yes.click(); return 'yes:' + t.slice(0, 200); }
              return 'dialog:' + t.slice(0, 200);
            }"""
        )
        result["confirm"] = yn
        await page.wait_for_timeout(2500)

        await page.goto(
            "https://app.ezlynx.com/web/directory/entry/562780",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)
        snap = await page.evaluate(
            """() => {
              const ta = [...document.querySelectorAll('textarea')][0];
              return {
                notes: ta ? ta.value : '',
                hasDoc: document.body.innerText.includes('Document Download'),
                rows: [...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')]
                  .map(r => r.innerText.replace(/\\n+/g,' | ').trim()).slice(0, 12)
              };
            }"""
        )
        result["verify"] = snap
        await page.screenshot(path=str(SHOT / "diesel_final_verify.png"), full_page=True)

    data = json.loads(EXTRACTED.read_text())
    key = "Diesel Insurance Solutions"
    data[key].setdefault("directory", {})["notes"] = NOTES[:1000]
    contacts = [
        c
        for c in data[key].setdefault("contacts", [])
        if (c.get("name") or "").strip().lower() != "document download"
    ]
    if snap.get("hasDoc"):
        contacts.append(
            {
                "name": "Document Download",
                "title": "Agent portal for docs",
                "email": "",
                "phone": "",
                "address": "https://dieselauto.joshu.insure/store/diesel",
            }
        )
    data[key]["contacts"] = contacts
    EXTRACTED.write_text(json.dumps(data, indent=2))

    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import enrich_routing_from_directory

    enrich = enrich_routing_from_directory(key)
    result["enrich"] = enrich

    proof = json.loads(PROOF.read_text())
    proof["passwords_stored"] = False
    proof.pop("password_scrub_applied", None)
    dc = proof["carriers"]["Diesel"]
    dc["diesel_retry2"] = result
    dc["notes_written"] = bool(snap.get("notes") and "Portal-for-docs" in snap.get("notes", ""))
    dc["notes_text"] = NOTES
    dc["contacts_filled"] = [
        {
            "purpose": "document_download",
            "contact": {
                "name": "Document Download",
                "title": "Agent portal for docs",
                "email": "",
                "phone": "",
                "address1": "https://dieselauto.joshu.insure/store/diesel",
            },
            "form_result": {"ok": bool(snap.get("hasDoc")), "dialog_save": save_clicked},
        }
    ]
    dc["snapshot_after"] = {"notes": snap.get("notes"), "rows": snap.get("rows")}
    dc["screenshot_after"] = str(SHOT / "diesel_final_verify.png")
    dc["enrich"] = {"ok": True, "result": enrich}
    dc["finished_at_et"] = et()
    PROOF.write_text(json.dumps(proof, indent=2))
    print(json.dumps(result, indent=2)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
