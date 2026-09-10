#!/usr/bin/env python3
"""Manual Renewals Directory UI fill for Diesel / Trinity / Rocklake.

Hard stops: no passwords, no bind, no money.
Prefer CDP SSRobie session. Missing contacts → email_rep_and_pend.
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, Page

BASE = Path("/opt/renewal-automation-system")
SHOT = BASE / "data" / "screenshots" / "directory_fill"
PROOF = BASE / "data" / "handoffs" / "directory_diesel_trinity_rocklake_proof.json"
EXTRACTED = BASE / "data" / "ezlynx_full_extracted_directory.json"

TARGETS = [
    {
        "label": "Diesel",
        "directory_id": "562780",
        "name": "Diesel Insurance Solutions",
        "portal_url": "https://dieselauto.joshu.insure/store/diesel",
        "add_user_url": "https://dieselauto.joshu.insure/store/diesel/auth/register",
        "rep_email": "Jason@dieselinsure.com",
        "rep_name": "Jason Wexler",
    },
    {
        "label": "Trinity",
        "directory_id": "239341",
        "name": "Trinity Underwriters",
        "portal_url": "https://trinityunderwriters.net",
        "add_user_url": None,
        "rep_email": "lensey@trinityunderwriters.net",
        "rep_name": "Lensey Rudnick",
    },
    {
        "label": "Rocklake",
        "directory_id": "121172",
        "name": "Rocklake Insurance Group MGA",
        "portal_url": "https://rocklakeig.com/",
        "add_user_url": None,
        "rep_email": "Gabe.McGavock@rlig.com",
        "rep_name": "Gabe McGavock",
    },
]

PURPOSES = ["document_download", "policy_change", "renewals", "loss_runs"]

# Contacts we can safely write from Directory SoT / known portal paths (NO passwords).
# Only add when missing usable email/phone for that purpose after resolver check,
# OR when we are adding portal-pointer rows that help ops (still email_rep if no email/phone).
FILL_PLAN = {
    "Diesel": {
        # renewals already has Jason — do not duplicate
        # Add Document Download portal pointer + ask rep for email/phone
        "add_contacts": [
            {
                "name": "Document Download",
                "title": "Agent portal for docs",
                "email": "",  # unknown — playbook email_rep
                "phone": "",
                "address1": "https://dieselauto.joshu.insure/store/diesel",
                "purpose": "document_download",
                "needs_email_ask": True,
            },
            {
                "name": "Loss Runs",
                "title": "Request contact",
                "email": "",
                "phone": "",
                "address1": "",
                "purpose": "loss_runs",
                "needs_email_ask": True,
                "skip_if_no_email": True,  # don't create empty shell; email_rep instead
            },
        ],
        # Update existing Endorsement Requests? keep URL; still missing email → ask
        "notes": (
            "Portal-for-docs: https://dieselauto.joshu.insure/store/diesel "
            "(add-user: https://dieselauto.joshu.insure/store/diesel/auth/register). "
            "Robie Directory fill 2026-09-06. No passwords stored."
        ),
        "email_ask_purposes": ["document_download", "policy_change", "loss_runs"],
    },
    "Trinity": {
        "add_contacts": [],
        "notes": (
            "Portal-for-docs: https://trinityunderwriters.net "
            "(confirm agent portal + add-user path for robie@streetsmart.insurance). "
            "Robie Directory fill 2026-09-06. No passwords stored."
        ),
        "email_ask_purposes": ["document_download"],
    },
    "Rocklake": {
        "add_contacts": [],  # all four purposes already present in Directory SoT
        "notes": (
            "Portal-for-docs: https://rocklakeig.com/ "
            "(Document Download email: customerservice@scoutig.com). "
            "Robie Directory verify 2026-09-06. No passwords stored."
        ),
        "email_ask_purposes": [],
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def et_now_label() -> str:
    # America/New_York currently UTC-4
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


async def hardstop_check(page: Page, proof: Dict[str, Any]) -> bool:
    url = page.url or ""
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    blob = (url + " " + body).lower()
    if any(
        x in blob
        for x in (
            "forcedoff",
            "forced off",
            "forgotpassword",
            "forgot password",
            "auth/account/login",
        )
    ) or "/login" in url.lower():
        path = SHOT / "HARDSTOP_forcedOff_or_login.png"
        await page.screenshot(path=str(path), full_page=True)
        proof["hard_stop"] = {
            "reason": "forcedOff/forgotpassword/login detected",
            "url": url,
            "screenshot": str(path),
            "at": utc_now(),
        }
        return True
    return False


async def get_ez_page(ctx) -> Page:
    for pg in ctx.pages:
        if "ezlynx.com" in (pg.url or ""):
            await pg.bring_to_front()
            return pg
    return await ctx.new_page()


async def open_directory_entry(page: Page, directory_id: str) -> bool:
    urls = [
        f"https://app.ezlynx.com/web/directory/entry/{directory_id}",
        f"https://app.ezlynx.com/web/directory/entries/{directory_id}",
        f"https://app.ezlynx.com/ApplicantPortal/Directory/Entry/{directory_id}",
    ]
    for url in urls:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            title = await page.title()
            cur = page.url
            print(f"  open {url} -> {cur} title={title} status={getattr(resp,'status',None)}")
            if "login" in cur.lower():
                return False
            # success signals
            text = await page.inner_text("body")
            if "Add contact" in text or "Edit Entry" in title or "Notes" in text:
                return True
            if directory_id in cur and "directory" in cur.lower():
                return True
        except Exception as e:
            print(f"  open fail {url}: {e}")
    # fallback: search directory list
    try:
        await page.goto("https://app.ezlynx.com/web/directory", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        # try click entry link
        link = page.locator(f'a[href*="/directory/entry/{directory_id}"]').first
        if await link.count():
            await link.click()
            await page.wait_for_timeout(2500)
            return "directory" in page.url.lower()
    except Exception as e:
        print(f"  directory list fallback fail: {e}")
    return False


async def read_entry_snapshot(page: Page) -> Dict[str, Any]:
    return await page.evaluate(
        """() => {
      const notesEl = [...document.querySelectorAll('textarea')].find(t => {
        const lab = (t.closest('.mat-mdc-form-field, .mat-form-field')||{}).innerText||'';
        return /notes/i.test(lab) || (t.getAttribute('aria-label')||'').toLowerCase().includes('notes');
      });
      const notes = notesEl ? notesEl.value : null;
      const rows = [...document.querySelectorAll('table tbody tr, mat-row, .mat-mdc-row')]
        .map(r => (r.innerText||'').replace(/\\n+/g,' | ').trim())
        .filter(Boolean);
      return {
        url: location.href,
        title: document.title,
        notes,
        notesLen: notes ? notes.length : 0,
        bodyHead: document.body.innerText.slice(0, 3500),
        rows: rows.slice(0, 40),
      };
    }"""
    )


async def set_notes(page: Page, notes: str) -> bool:
    # Find Notes textarea
    candidates = page.locator("textarea")
    n = await candidates.count()
    target = None
    for i in range(n):
        el = candidates.nth(i)
        # check nearby label
        ok = await el.evaluate(
            """el => {
          const field = el.closest('.mat-mdc-form-field, .mat-form-field, .form-group, div');
          const t = ((field && field.innerText) || '') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.id||'');
          return /notes/i.test(t);
        }"""
        )
        if ok:
            target = el
            break
    if target is None and n:
        # fallback: first textarea on edit entry (often Notes)
        target = candidates.first
    if target is None:
        return False
    await target.click()
    await target.fill("")
    # max 1000
    await target.fill(notes[:1000])
    await page.wait_for_timeout(300)
    return True


async def click_add_contact(page: Page) -> bool:
    for sel in [
        page.get_by_role("button", name=re.compile(r"Add contact", re.I)),
        page.locator("button:has-text('Add contact')"),
        page.get_by_text("Add contact", exact=True),
    ]:
        try:
            if await sel.count():
                await sel.first.click(timeout=8000)
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def fill_add_contact_form(
    page: Page,
    *,
    name: str,
    title: str = "",
    email: str = "",
    phone: str = "",
    address1: str = "",
) -> Dict[str, Any]:
    result = {"ok": False, "fields": {}}
    # Wait for dialog
    await page.wait_for_timeout(800)
    dialog = page.locator("mat-dialog-container, [role='dialog'], .cdk-overlay-pane").last

    async def fill_by_label(label: str, value: str) -> bool:
        if not value:
            return False
        # try get_by_label
        try:
            loc = page.get_by_label(re.compile(label, re.I), exact=False).first
            if await loc.count():
                await loc.fill(value)
                result["fields"][label] = value
                return True
        except Exception:
            pass
        # evaluate fill by mat-label text
        filled = await page.evaluate(
            """({label, value}) => {
          const fields = [...document.querySelectorAll('.mat-mdc-form-field, .mat-form-field')];
          for (const f of fields) {
            const lab = (f.querySelector('mat-label, label, .mdc-floating-label')||{}).innerText||'';
            if (new RegExp(label, 'i').test(lab)) {
              const input = f.querySelector('input, textarea');
              if (input) {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
                  || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                input.focus();
                if (setter) setter.call(input, value); else input.value = value;
                input.dispatchEvent(new Event('input', {bubbles:true}));
                input.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
              }
            }
          }
          return false;
        }""",
            {"label": label, "value": value},
        )
        if filled:
            result["fields"][label] = value
        return bool(filled)

    # Name is required
    ok_name = await fill_by_label("^Name", name)
    if not ok_name:
        # first visible text input in dialog
        try:
            inp = page.locator("mat-dialog-container input[type='text'], [role='dialog'] input[type='text']").first
            await inp.fill(name)
            result["fields"]["Name"] = name
            ok_name = True
        except Exception as e:
            result["error"] = f"name fill failed: {e}"
            return result

    await fill_by_label("^Title", title)
    await fill_by_label("^Email", email)
    await fill_by_label("^Phone", phone)
    await fill_by_label("Address Line 1", address1)
    # Save contact in dialog
    saved = False
    for name_btn in ["Save", "Add", "Create", "OK"]:
        btn = page.get_by_role("button", name=re.compile(rf"^{name_btn}$", re.I))
        try:
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click()
                await page.wait_for_timeout(1500)
                saved = True
                break
        except Exception:
            continue
    if not saved:
        # click Save inside overlay
        try:
            await page.locator(".cdk-overlay-pane button:has-text('Save')").last.click()
            await page.wait_for_timeout(1500)
            saved = True
        except Exception as e:
            result["error"] = f"save click failed: {e}"
            await page.keyboard.press("Escape")
            return result
    result["ok"] = True
    result["saved"] = saved
    return result


async def save_entry(page: Page) -> bool:
    try:
        btn = page.get_by_role("button", name=re.compile(r"^Save$", re.I)).first
        await btn.click(timeout=10000)
        await page.wait_for_timeout(2500)
        return True
    except Exception as e:
        print("save_entry failed", e)
        return False


def resolve_missing(carrier_name: str) -> Dict[str, Any]:
    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import resolve_action_contacts

    out = {}
    for a in PURPOSES:
        r = resolve_action_contacts(carrier_name, a)
        out[a] = {
            "missing": r.get("missing"),
            "primary": r.get("primary"),
            "portal_url": r.get("portal_url"),
            "underwriter_email": r.get("underwriter_email"),
            "next_step": r.get("next_step"),
        }
    return out


def send_rep_email(carrier: Dict[str, Any], missing_purposes: List[str], portal_url: str) -> Dict[str, Any]:
    import sys

    sys.path.insert(0, str(BASE))
    from src.email_outreach.gmail_client import GmailRenewalClient

    ask_lines = []
    for p in missing_purposes:
        ask_lines.append(f"- Who handles {p.replace('_', ' ')} for StreetSmart? (Name, email, phone)")
    ask_lines.append(
        "- Do you have an agent portal for document retrieval / policy service? "
        "Please confirm URL + add-user path for robie@streetsmart.insurance"
    )
    if portal_url:
        ask_lines.append(f"- We currently have portal URL noted as: {portal_url}")

    subject = (
        f"[DIRECTORY-CONTACT-ASK] StreetSmart Insurance — missing Directory contacts "
        f"for {carrier['name']}"
    )
    body = f"""Hello {carrier.get('rep_name') or ''};

StreetSmart Insurance (Robie / Manual Renewals) is updating our EZLynx Carrier Directory contacts for {carrier['name']}.

We are missing usable Directory contacts for:
{chr(10).join(ask_lines)}

Please reply with the best Name / Email / Phone for each, and portal guidance if available.
We will pend 2–3 business days for your reply, then call if needed.

Thank you,
Robie
StreetSmart Insurance
robie@streetsmart.insurance
"""
    client = GmailRenewalClient()
    # Approved missing-contact playbook: email rep (no client/insured email).
    # CC Carlo for visibility (not required by exception, but operational).
    sent = client.send_email(
        to_email=carrier["rep_email"],
        subject=subject,
        body_text=body,
        cc=["carlo@streetsmart.insurance"],
    )
    return {
        "to": carrier["rep_email"],
        "cc": ["carlo@streetsmart.insurance"],
        "subject": subject,
        "missing_purposes": missing_purposes,
        "pend_min_days": 2,
        "pend_max_days": 3,
        "call_after_pend": True,
        "gmail_result": {
            k: sent.get(k)
            for k in ("id", "threadId", "status", "labelIds", "error")
            if k in sent or True
        },
        "at": utc_now(),
        "at_et": et_now_label(),
    }



def patch_extracted_directory(carrier_name: str, directory_id: str, notes: str, new_contacts: list) -> Dict[str, Any]:
    """Update ezlynx_full_extracted_directory.json from UI fills (SoT for resolver). No passwords."""
    if not EXTRACTED.exists():
        return {"ok": False, "error": "extracted missing"}
    data = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    key = None
    for k, v in data.items():
        did = str(((v or {}).get("directory") or {}).get("id") or "")
        if did == str(directory_id) or k == carrier_name:
            key = k
            break
    if not key:
        return {"ok": False, "error": f"carrier not found id={directory_id}"}
    entry = data[key]
    directory = entry.setdefault("directory", {})
    if notes:
        directory["notes"] = notes[:1000]
        # never store passwords in notes (guard)
        if re.search(r"password|passwd", directory["notes"], re.I):
            directory["notes"] = re.sub(r"(?i)(password|passwd)\s*[:=]\s*\S+", "[REDACTED]", directory["notes"])
    contacts = entry.setdefault("contacts", [])
    added = []
    for nc in new_contacts or []:
        c = nc.get("contact") or {}
        name = (c.get("name") or "").strip()
        if not name:
            continue
        # upsert by name
        existing = next((x for x in contacts if (x.get("name") or "").strip().lower() == name.lower()), None)
        row = {
            "name": name,
            "title": c.get("title") or "",
            "email": c.get("email") or "",
            "phone": c.get("phone") or "",
            "address": c.get("address1") or "",
        }
        if existing:
            existing.update({k: v for k, v in row.items() if v})
            added.append({"updated": name})
        else:
            contacts.append(row)
            added.append({"added": name})
    EXTRACTED.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True, "key": key, "changes": added, "notes_len": len(directory.get("notes") or "")}


def enrich_carrier(carrier_name: str) -> Dict[str, Any]:
    import sys

    sys.path.insert(0, str(BASE))
    from src.directory.ezlynx_directory_resolver import enrich_routing_from_directory

    try:
        return {"ok": True, "result": enrich_routing_from_directory(carrier_name)}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()[-800:]}


async def process_carrier(page: Page, carrier: Dict[str, Any], proof_carrier: Dict[str, Any]) -> None:
    label = carrier["label"]
    plan = FILL_PLAN[label]
    proof_carrier["directory_id"] = carrier["directory_id"]
    proof_carrier["name"] = carrier["name"]
    proof_carrier["started_at"] = utc_now()

    before = resolve_missing(carrier["name"])
    proof_carrier["resolver_before"] = before

    opened = await open_directory_entry(page, carrier["directory_id"])
    proof_carrier["opened"] = opened
    proof_carrier["url_after_open"] = page.url
    if not opened:
        shot = SHOT / f"{label.lower()}_open_fail.png"
        await page.screenshot(path=str(shot), full_page=True)
        proof_carrier["open_fail_screenshot"] = str(shot)
        return
    if await hardstop_check(page, proof_carrier):
        return

    snap0 = await read_entry_snapshot(page)
    proof_carrier["snapshot_before"] = {
        "url": snap0.get("url"),
        "notesLen": snap0.get("notesLen"),
        "rows": snap0.get("rows"),
    }
    await page.screenshot(path=str(SHOT / f"{label.lower()}_before.png"), full_page=True)
    proof_carrier["screenshot_before"] = str(SHOT / f"{label.lower()}_before.png")

    # Notes: portal-for-docs (never passwords)
    notes_ok = await set_notes(page, plan["notes"])
    proof_carrier["notes_written"] = notes_ok
    proof_carrier["notes_text"] = plan["notes"][:1000]

    filled = []
    skipped = []
    for c in plan.get("add_contacts") or []:
        # skip empty shells that have no email and skip_if_no_email
        if c.get("skip_if_no_email") and not (c.get("email") or c.get("phone")):
            skipped.append({"purpose": c["purpose"], "reason": "no email/phone; email_rep_and_pend instead"})
            continue
        # if purpose already has usable contact, skip add
        if not before.get(c["purpose"], {}).get("missing") and c["purpose"] != "document_download":
            # For Trinity document_download is missing; for Rocklake none added
            skipped.append({"purpose": c["purpose"], "reason": "already present per resolver"})
            continue
        if not before.get(c["purpose"], {}).get("missing") and label == "Rocklake":
            skipped.append({"purpose": c["purpose"], "reason": "already present"})
            continue

        # For Diesel document_download always missing — add portal pointer even without email
        opened_add = await click_add_contact(page)
        if not opened_add:
            skipped.append({"purpose": c["purpose"], "reason": "Add contact button failed"})
            continue
        await page.screenshot(path=str(SHOT / f"{label.lower()}_add_{c['purpose']}_dialog.png"), full_page=True)
        fr = await fill_add_contact_form(
            page,
            name=c["name"],
            title=c.get("title") or "",
            email=c.get("email") or "",
            phone=c.get("phone") or "",
            address1=c.get("address1") or "",
        )
        filled.append({"purpose": c["purpose"], "contact": {k: c.get(k) for k in ("name", "title", "email", "phone", "address1")}, "form_result": fr})
        await page.screenshot(path=str(SHOT / f"{label.lower()}_after_add_{c['purpose']}.png"), full_page=True)

    proof_carrier["contacts_filled"] = filled
    proof_carrier["contacts_skipped"] = skipped

    saved = await save_entry(page)
    proof_carrier["entry_saved"] = saved
    await page.wait_for_timeout(1500)
    # re-open to verify
    await open_directory_entry(page, carrier["directory_id"])
    snap1 = await read_entry_snapshot(page)
    proof_carrier["snapshot_after"] = {
        "url": snap1.get("url"),
        "notesLen": snap1.get("notesLen"),
        "notes": (snap1.get("notes") or "")[:1000],
        "rows": snap1.get("rows"),
    }
    await page.screenshot(path=str(SHOT / f"{label.lower()}_after.png"), full_page=True)
    proof_carrier["screenshot_after"] = str(SHOT / f"{label.lower()}_after.png")

    # email_rep_and_pend for missing purposes (playbook)
    email_ask = list(plan.get("email_ask_purposes") or [])
    # also include still-missing from resolver-before that we couldn't fill with email/phone
    for p in PURPOSES:
        if before.get(p, {}).get("missing") and p not in email_ask:
            # if we added phone-only usable contact, may not need ask — check after enrich later
            email_ask.append(p)
    # de-dup
    email_ask = list(dict.fromkeys(email_ask))
    if email_ask:
        # For Rocklake email_ask empty. For Trinity/Diesel send.
        # If we made Trinity document_download usable via phone, still ask for email/portal per playbook ask list.
        try:
            em = send_rep_email(carrier, email_ask, carrier.get("portal_url") or "")
            proof_carrier["email_rep_and_pend"] = em
            proof_carrier["pended"] = {
                "pend_min_days": 2,
                "pend_max_days": 3,
                "call_after_pend": True,
                "missing_purposes": email_ask,
            }
        except Exception as e:
            proof_carrier["email_rep_and_pend"] = {"error": str(e), "trace": traceback.format_exc()[-800:]}
    else:
        proof_carrier["email_rep_and_pend"] = None
        proof_carrier["pended"] = None

    # patch extracted SoT then enrich routing
    proof_carrier["extracted_patch"] = patch_extracted_directory(
        carrier["name"],
        carrier["directory_id"],
        plan["notes"],
        filled,
    )
    proof_carrier["enrich"] = enrich_carrier(carrier["name"])
    proof_carrier["resolver_after"] = resolve_missing(carrier["name"])
    proof_carrier["finished_at"] = utc_now()
    proof_carrier["finished_at_et"] = et_now_label()


async def main():
    SHOT.mkdir(parents=True, exist_ok=True)
    PROOF.parent.mkdir(parents=True, exist_ok=True)
    proof: Dict[str, Any] = {
        "job": "directory_diesel_trinity_rocklake",
        "board": "IN",
        "executor": "Manual Renewals / hermes-poc-01",
        "started_at": utc_now(),
        "started_at_et": et_now_label(),
        "hard_stops": ["no_passwords", "no_bind", "no_money"],
        "session": {"prefer": "CDP/storage SSRobie", "cdp": "http://localhost:9222"},
        "carriers": {},
        "passwords_stored": False,
    }

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await get_ez_page(ctx)
        await page.goto("https://app.ezlynx.com/web/dashboard", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        proof["session"]["dashboard_url"] = page.url
        proof["session"]["title"] = await page.title()
        # detect user footer
        try:
            foot = await page.evaluate("() => document.body.innerText.includes('SSRobie')")
            proof["session"]["ssrobie_footer"] = bool(foot)
        except Exception:
            pass
        if await hardstop_check(page, proof):
            proof["finished_at"] = utc_now()
            PROOF.write_text(json.dumps(proof, indent=2))
            print("HARD STOP — wrote proof")
            return

        for carrier in TARGETS:
            print("====", carrier["label"])
            pc: Dict[str, Any] = {}
            proof["carriers"][carrier["label"]] = pc
            try:
                await process_carrier(page, carrier, pc)
            except Exception as e:
                pc["error"] = str(e)
                pc["trace"] = traceback.format_exc()[-1500:]
                try:
                    await page.screenshot(path=str(SHOT / f"{carrier['label'].lower()}_error.png"), full_page=True)
                    pc["error_screenshot"] = str(SHOT / f"{carrier['label'].lower()}_error.png")
                except Exception:
                    pass
            # checkpoint proof after each carrier
            PROOF.write_text(json.dumps(proof, indent=2))

    proof["finished_at"] = utc_now()
    proof["finished_at_et"] = et_now_label()
    # scrub any accidental password-like keys
    raw = json.dumps(proof)
    if re.search(r"password|passwd|secret", raw, re.I):
        # remove values but keep notice
        def scrub(o):
            if isinstance(o, dict):
                return {
                    k: ("[REDACTED]" if re.search(r"pass|secret", k, re.I) else scrub(v))
                    for k, v in o.items()
                }
            if isinstance(o, list):
                return [scrub(x) for x in o]
            if isinstance(o, str) and re.search(r"(password\s*[:=])", o, re.I):
                return "[REDACTED]"
            return o

        proof = scrub(proof)
        proof["password_scrub_applied"] = True
    PROOF.write_text(json.dumps(proof, indent=2))
    print("PROOF", PROOF)
    print(json.dumps({k: {
        "filled": [c.get("purpose") for c in (v.get("contacts_filled") or [])],
        "email": (v.get("email_rep_and_pend") or {}).get("to") if isinstance(v.get("email_rep_and_pend"), dict) else None,
        "enrich_ok": (v.get("enrich") or {}).get("ok"),
        "opened": v.get("opened"),
        "error": v.get("error"),
    } for k,v in proof["carriers"].items()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
