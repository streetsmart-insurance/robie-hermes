#!/usr/bin/env python3
"""Finalize Directory proof: verify UI notes, enrich, accurate fill status. No passwords."""
import asyncio, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path("/opt/renewal-automation-system")
SHOT = BASE / "data/screenshots/directory_fill"
PROOF = BASE / "data/handoffs/directory_diesel_trinity_rocklake_proof.json"
EXTRACTED = BASE / "data/ezlynx_full_extracted_directory.json"

TARGETS = [
    ("Diesel", "562780", "Diesel Insurance Solutions"),
    ("Trinity", "239341", "Trinity Underwriters"),
    ("Rocklake", "121172", "Rocklake Insurance Group MGA"),
]


def et():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


async def main():
    proof = json.loads(PROOF.read_text())
    proof["passwords_stored"] = False
    proof.pop("password_scrub_applied", None)
    proof["finalized_at_et"] = et()

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        page = next(pg for pg in b.contexts[0].pages if "ezlynx.com" in (pg.url or ""))
        await page.bring_to_front()
        # dismiss leave if any
        await page.evaluate(
            """() => {
              const panes=[...document.querySelectorAll('mat-dialog-container,[role=dialog]')];
              for (const p of panes) {
                if (/leave page|unsaved/i.test(p.innerText||'')) {
                  const no=[...p.querySelectorAll('button')].find(b=>/^\\s*No\\s*$/i.test((b.innerText||'').trim()));
                  if (no) no.click();
                }
              }
            }"""
        )
        await page.wait_for_timeout(500)

        for label, did, name in TARGETS:
            await page.goto(
                f"https://app.ezlynx.com/web/directory/entry/{did}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2200)
            snap = await page.evaluate(
                """() => {
                  const ta=[...document.querySelectorAll('textarea')][0];
                  const rows=[...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')]
                    .map(r=>r.innerText.replace(/\\n+/g,' | ').trim()).filter(Boolean);
                  return {
                    url: location.href,
                    title: document.title,
                    notes: ta ? ta.value : '',
                    rows: rows.slice(0, 30),
                    ssrobie: document.body.innerText.includes('SSRobie')
                  };
                }"""
            )
            path = SHOT / f"{label.lower()}_final.png"
            await page.screenshot(path=str(path), full_page=True)
            dc = proof.setdefault("carriers", {}).setdefault(label, {})
            dc["final_verify"] = {
                "url": snap["url"],
                "notes": snap["notes"],
                "notes_has_portal": "Portal-for-docs" in (snap["notes"] or "") or "portal" in (snap["notes"] or "").lower(),
                "row_count": len(snap["rows"]),
                "rows": snap["rows"][:12],
                "screenshot": str(path),
                "ssrobie": snap["ssrobie"],
                "at_et": et(),
            }
            dc["notes_written"] = bool(snap["notes"]) and (
                "Portal-for-docs" in snap["notes"] or "portal" in snap["notes"].lower()
            )
            dc["notes_text"] = snap["notes"]
            dc["screenshot_after"] = str(path)
            # patch extracted notes from UI
            data = json.loads(EXTRACTED.read_text())
            if name in data and snap["notes"]:
                data[name].setdefault("directory", {})["notes"] = snap["notes"][:1000]
                EXTRACTED.write_text(json.dumps(data, indent=2))

    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import (
        enrich_routing_from_directory,
        resolve_action_contacts,
    )

    purposes = ["document_download", "policy_change", "renewals", "loss_runs"]
    for label, did, name in TARGETS:
        dc = proof["carriers"][label]
        enrich = enrich_routing_from_directory(name)
        dc["enrich"] = {"ok": True, "result": enrich}
        after = {}
        for a in purposes:
            r = resolve_action_contacts(name, a)
            after[a] = {
                "missing": r.get("missing"),
                "primary": r.get("primary"),
            }
        dc["resolver_after"] = after
        # summarize purposes filled (usable contact present)
        dc["purposes_ready"] = [a for a, r in after.items() if not r.get("missing")]
        dc["purposes_missing"] = [a for a, r in after.items() if r.get("missing")]

        # Diesel-specific honesty: Document Download contact row not added (UI Address validation / nav);
        # portal recorded in Notes; email_rep_and_pend covers missing.
        if label == "Diesel":
            dc["contacts_filled_ui"] = {
                "notes_portal_for_docs": True,
                "document_download_contact_row": False,
                "reason": "EZLynx Address Line 1 rejects portal URLs; Add-contact route flaky with unsaved-nav guard. Portal kept in Notes. email_rep_and_pend sent for missing purposes.",
            }
            # clear misleading contacts_filled claiming success if any
            filled = dc.get("contacts_filled") or []
            dc["contacts_filled"] = [
                c for c in filled if (c.get("form_result") or {}).get("ok") is True and c.get("purpose") != "document_download"
            ]
            dc["contacts_attempted"] = [
                {
                    "purpose": "document_download",
                    "result": "notes_only",
                    "detail": dc["contacts_filled_ui"]["reason"],
                }
            ]

    proof["finished_at"] = datetime.now(timezone.utc).isoformat()
    proof["finished_at_et"] = et()
    proof["success_summary"] = {
        label: {
            "directory_id": did,
            "purposes_ready": proof["carriers"][label].get("purposes_ready"),
            "purposes_missing": proof["carriers"][label].get("purposes_missing"),
            "notes_portal": proof["carriers"][label].get("notes_written"),
            "emailed": (proof["carriers"][label].get("email_rep_and_pend") or {}).get("to")
            if isinstance(proof["carriers"][label].get("email_rep_and_pend"), dict)
            else None,
            "pended": proof["carriers"][label].get("pended"),
            "enrich_channel": ((proof["carriers"][label].get("enrich") or {}).get("result") or {}).get("channel"),
        }
        for label, did, _ in TARGETS
    }
    PROOF.write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof["success_summary"], indent=2))
    print("PROOF", PROOF)


if __name__ == "__main__":
    asyncio.run(main())
