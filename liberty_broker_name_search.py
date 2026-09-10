#!/usr/bin/env python3
"""Liberty broker multi-strategy name/policy/quote search for Pross (Carlo HITL retry)."""
from __future__ import annotations
import asyncio, json, os, re, subprocess, sys, traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/renewal-automation-system")
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
os.environ.setdefault("DISPLAY", ":99")
from playwright.async_api import async_playwright

DOWNLOAD_DIR = ROOT / "data/downloads/carrier_renewals"
SHOT_DIR = ROOT / "data/screenshots/portal_fetch"
PROOF = ROOT / "data/handoffs/portal_fetch_proof.json"
STATE = ROOT / "data/liberty_broker_storage_state.json"
TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
SHOT_DIR.mkdir(parents=True, exist_ok=True)
GCP = "streetsmart-hermes-poc"
BROKER_URL = "https://account.libertymutual.com/broker"

QUERIES = [
    # Name
    ("name", "Pross Construction"),
    ("name", "Pross Construction LLC"),
    ("name", "Pross"),
    ("name", "Wojtowicz"),
    ("name", "Bernard Wojtowicz"),
    # Address
    ("address", "Wonham"),
    ("address", "75 Wonham"),
    ("address", "Clifton"),
    # Policy variants
    ("policy", "WC5-33S-B1X2Z3-025"),
    ("policy", "WC533SB1X2Z3025"),
    ("policy", "B1X2Z3"),
    ("policy", "33S-B1X2Z3"),
    ("policy", "WC5-33S-B1X2Z3-024"),
    ("policy", "WC5-33S"),
    ("policy", "WC5 33S B1X2Z3 025"),
    # Quote
    ("quote", "02212061-01"),
    ("quote", "02212061"),
    ("quote", "2212061"),
]

def log(m):
    print(re.sub(r"\b\d{6}\b", "[REDACTED]", str(m)), flush=True)

def secret(sid):
    from google.cloud import secretmanager
    c = secretmanager.SecretManagerServiceClient()
    try:
        return c.access_secret_version(
            request={"name": f"projects/{GCP}/secrets/{sid}/versions/latest"}
        ).payload.data.decode()
    except Exception as e:
        log(f"secret fail {sid}:{type(e).__name__}"); return None

def wait_otp(query, max_wait=150, since_offset=45):
    from src.email_outreach.otp_interceptor import otp_interceptor
    res = otp_interceptor.wait_for_otp(
        query=query, max_wait_seconds=max_wait, poll_interval=2, since_offset_seconds=since_offset
    )
    if res and res.code:
        log(f"OTP inbox={res.inbox} len={len(res.code)}"); return res.code
    return None

def pdf_lines(path, n=25):
    try:
        out = subprocess.check_output(["pdftotext", str(path), "-"], stderr=subprocess.DEVNULL, timeout=30)
        return [ln.strip() for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()][:n]
    except Exception as e:
        return [f"err:{e}"]

async def shot(page, name):
    path = SHOT_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log(f"shot {name}.png")
        return str(path)
    except Exception as e:
        log(f"shot fail {name}:{type(e).__name__}"); return None

async def dump(page, name):
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    (SHOT_DIR / f"{name}.txt").write_text(f"URL={page.url}\n\n{body[:12000]}")
    return body

async def launch(p, use_state=True):
    b = await p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled", "--window-size=1440,900"],
    )
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    kwargs = {"viewport": {"width": 1440, "height": 900}, "accept_downloads": True, "user_agent": ua}
    if use_state and STATE.exists():
        kwargs["storage_state"] = str(STATE)
        log("using storage_state")
    ctx = await b.new_context(**kwargs)
    ctx.set_default_timeout(45000)
    return b, ctx

async def click_email_method(page) -> bool:
    await shot(page, "liberty_broker_namesearch_auth_method")
    selectors = ["text=Email 1", 'button:has-text("Email")', 'a:has-text("Email")', 'label:has-text("Email")']
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=1500):
                log(f"clicking auth method sel={sel}")
                await loc.click(); await asyncio.sleep(2); return True
        except Exception:
            continue
    ok = await page.evaluate("""() => {
      const nodes = [...document.querySelectorAll('a,button,div,li,span,label')];
      const el = nodes.find(n => {
        const t = (n.innerText||'').trim();
        return /^Email\\s*1/i.test(t) || (t.includes('Email 1') && t.length < 120);
      });
      if (!el) return false; el.click(); return true;
    }""")
    log(f"js email click ok={ok}"); await asyncio.sleep(2); return bool(ok)

async def submit_passcode(page, code: str) -> bool:
    filled = False
    for sel in ["#passcode", 'input[name="passcode"]', 'input[placeholder*="passcode" i]',
                'input[autocomplete="one-time-code"]', 'input[type="tel"]', 'input[type="text"]:visible']:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=1200):
                await loc.fill(code); log(f"filled passcode via {sel}"); filled = True; break
        except Exception:
            continue
    if not filled:
        return False
    for sel in ['button:has-text("Sign On")', 'button:has-text("Submit")',
                'button:has-text("Continue")', 'button:has-text("Verify")', 'button[type="submit"]']:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=1000):
                await loc.click(); break
        except Exception:
            continue
    return True

async def wait_post_mfa(page):
    for i in range(50):
        await asyncio.sleep(1)
        url = page.url
        try:
            txt = (await page.inner_text("body"))[:900].lower()
        except Exception:
            txt = ""
        if "authenticated" in txt:
            await shot(page, "liberty_broker_namesearch_authenticated"); continue
        if "passcode" in txt or "select authentication" in txt:
            continue
        if "account.libertymutual.com/broker" in url or "broker" in url:
            return True
        if "commercial-agent" in url or "agentsportal" in url:
            return True
        if "authorization.ping" in url or "lmidp.libertymutual.com" in url:
            continue
        if "login" not in txt and i > 5:
            return True
    return False

async def do_login(page, entry):
    user = secret("liberty_broker_username")
    pw = secret("liberty_broker_password")
    if not user or not pw:
        entry["errors"].append("missing_broker_creds"); entry["hitl_needed"] = True; return False
    log(f"creds_ok user_len={len(user)}")
    await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=90000)
    await shot(page, "liberty_broker_namesearch_start")
    for _ in range(40):
        if await page.locator('input[type="password"]:visible, input[placeholder="Password"]').count():
            break
        await asyncio.sleep(1)
    for usel in ['input[placeholder="Username"]', 'input[name="username"]', 'input#username',
                 'input[autocomplete="username"]', 'input[type="text"]:visible']:
        loc = page.locator(usel).first
        if await loc.count() and await loc.is_visible(timeout=1000):
            await loc.fill(user); break
    for psel in ['input[placeholder="Password"]', 'input[name="password"]', 'input#password',
                 'input[type="password"]:visible']:
        loc = page.locator(psel).first
        if await loc.count() and await loc.is_visible(timeout=1000):
            await loc.fill(pw); break
    clicked = False
    for bsel in ['button:has-text("Submit")', 'input[value="Submit"]', 'button:has-text("Log in")',
                 'button:has-text("Sign On")', 'button[type="submit"]', 'input[type="submit"]']:
        loc = page.locator(bsel).first
        if await loc.count() and await loc.is_visible(timeout=1000):
            log(f"login click via {bsel}"); await loc.click(); clicked = True; break
    if not clicked:
        ok = await page.evaluate("""() => {
          const btns=[...document.querySelectorAll('button,input[type=submit],a')];
          const b=btns.find(x => /submit|log\\s*in|sign\\s*on/i.test((x.innerText||x.value||'')));
          if(!b) return false; b.click(); return true;
        }""")
        log(f"login js click ok={ok}")
    await asyncio.sleep(4)
    await shot(page, "liberty_broker_namesearch_after_cred")
    body = (await page.inner_text("body"))[:2000].lower()
    if any(x in body for x in ["didn't recognize", "invalid user", "incorrect password"]):
        entry["errors"].append("login_rejected"); entry["hitl_needed"] = True; return False
    if ("select authentication" in body or "authentication method" in body
            or await page.locator("text=Email 1").count()):
        log("auth method chooser detected")
        if not await click_email_method(page):
            entry["errors"].append("could_not_click_email_mfa"); entry["hitl_needed"] = True; return False
        await asyncio.sleep(2)
    for _ in range(20):
        if await page.locator("#passcode, input[name='passcode'], input[placeholder*='passcode' i]").count():
            break
        b2 = (await page.inner_text("body"))[:800].lower()
        if "passcode" in b2:
            break
        await asyncio.sleep(1)
    await shot(page, "liberty_broker_namesearch_otp_prompt")
    loop = asyncio.get_event_loop()
    code = await loop.run_in_executor(
        None, lambda: wait_otp("from:mfa.libertymutual.com subject:Passcode", max_wait=150, since_offset=30),
    )
    if not code:
        entry["errors"].append("otp_not_received"); entry["hitl_needed"] = True; return False
    if not await submit_passcode(page, code):
        entry["errors"].append("otp_field_not_found"); entry["hitl_needed"] = True; return False
    await wait_post_mfa(page)
    await asyncio.sleep(3)
    await shot(page, "liberty_broker_namesearch_landed")
    try:
        await page.context.storage_state(path=str(STATE))
    except Exception:
        pass
    return True

async def session_ok(page) -> bool:
    await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)
    body = (await page.inner_text("body"))[:2500].lower()
    if await page.locator('input[type="password"]:visible').count():
        return False
    if "commercial login" in body or "username" in body and "password" in body:
        return False
    if "client accounts" in body or "search clients" in body or "streetsmart" in body:
        return True
    if "broker" in page.url and "login" not in body:
        return True
    return False

def parse_result_count(txt: str) -> int:
    m = re.search(r"(\d+)\s+Items?", txt, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+results?\s+returned", txt, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"0\s+results", txt, re.I) or "not returning any results" in txt.lower():
        return 0
    # if Pross-like hit visible, count rows heuristically
    if re.search(r"Pross|Wojtowicz|WC5-33S|B1X2Z3|02212061", txt, re.I):
        return max(1, len(re.findall(r"Pross|Wojtowicz|WC5", txt, re.I)))
    return 0

async def find_search_input(page):
    sels = [
        'input[placeholder*="search" i]',
        'input[type="search"]',
        'input[name*="search" i]',
        'input[aria-label*="search" i]',
        'input[placeholder*="client" i]',
        'input[placeholder*="account" i]',
        'input[placeholder*="policy" i]',
        'input[placeholder*="insured" i]',
        # Angular/material common
        'input[formcontrolname*="search" i]',
        'input.mat-input-element',
    ]
    for sel in sels:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=600):
                return loc, sel
        except Exception:
            continue
    # JS fallback: find input near "Search Clients"
    handle = await page.evaluate_handle("""() => {
      const inputs = [...document.querySelectorAll('input')].filter(i => {
        const t = (i.type||'').toLowerCase();
        return t === 'text' || t === 'search' || t === '';
      });
      return inputs[0] || null;
    }""")
    el = handle.as_element()
    if el:
        return el, "js_first_text_input"
    return None, None

async def run_one_search(page, kind, term, idx, results):
    safe = re.sub(r"[^A-Za-z0-9]+", "_", term)[:40].strip("_")
    shot_name = f"liberty_broker_namesearch_{idx:02d}_{kind}_{safe}"
    box, sel = await find_search_input(page)
    rec = {"kind": kind, "query": term, "result_count": None, "url": page.url,
           "screenshot": shot_name + ".png", "search_input": sel, "notes": []}
    if not box:
        rec["notes"].append("no_search_input"); rec["result_count"] = None
        await shot(page, shot_name)
        results.append(rec); return
    try:
        if hasattr(box, "fill"):
            await box.click(); await box.fill("")
            await box.fill(term)
        else:
            await box.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.type(term)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2.5)
        # also try clicking a Search button if present
        for bsel in ['button:has-text("Search")', 'button[aria-label*="search" i]',
                     'button:has-text("Go")', '[role="button"]:has-text("Search")']:
            loc = page.locator(bsel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=400):
                    await loc.click(); await asyncio.sleep(1.5); break
            except Exception:
                pass
        txt = await page.inner_text("body")
        count = parse_result_count(txt)
        rec["result_count"] = count
        rec["url"] = page.url
        # capture snippet of result area
        if count and count > 0:
            rec["notes"].append("HIT")
            rec["body_snippet"] = txt[:1500]
        elif "0 results" in txt.lower() or "0 Items" in txt or "not returning any results" in txt.lower():
            rec["notes"].append("empty")
        else:
            rec["notes"].append("ambiguous")
            # if table has rows beyond header
            try:
                rows = await page.locator("table tbody tr, .account-row, [class*='account'] tr").count()
                rec["notes"].append(f"dom_rows={rows}")
            except Exception:
                pass
        await shot(page, shot_name)
        await dump(page, shot_name)
        log(f"query[{idx}] kind={kind} q={term!r} count={count} url={page.url}")
    except Exception as e:
        rec["notes"].append(f"err:{type(e).__name__}:{e}")
        await shot(page, shot_name)
        log(f"query[{idx}] FAIL {term}: {type(e).__name__}")
    results.append(rec)

async def document_add_account(page, entry):
    info = {"banner_present": False, "mailto": None, "links_clicked": [], "forms_seen": [],
            "do_not_submit": True, "notes": []}
    body = await page.inner_text("body")
    if "Missing accounts" in body or "add them" in body.lower():
        info["banner_present"] = True
    # mailto / email link
    for sel in ['a[href^="mailto:"]', 'a:has-text("send us an email")', 'a:has-text("email")',
                'a:has-text("Missing accounts")', 'button:has-text("Add Account")',
                'a:has-text("Add Account")', 'button:has-text("Request Access")',
                'a:has-text("Request Access")', 'a:has-text("Link Account")',
                'button:has-text("Link")', 'a:has-text("Request")']:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=800):
                href = await loc.get_attribute("href")
                txt = (await loc.inner_text())[:120]
                info["links_clicked"].append({"sel": sel, "text": txt, "href": href})
                if href and href.startswith("mailto:"):
                    # redact full email body but keep destination domain
                    info["mailto"] = re.sub(r"(subject|body)=[^&]+", r"\1=[REDACTED]", href)[:300]
                    info["notes"].append("mailto_present_not_sent")
                else:
                    # open but do not fill/submit forms that email Liberty
                    try:
                        await loc.click(); await asyncio.sleep(2)
                        await shot(page, f"liberty_broker_namesearch_addflow_{re.sub(r'[^A-Za-z0-9]+','_',sel)[:24]}")
                        b2 = await page.inner_text("body")
                        (SHOT_DIR / "liberty_broker_namesearch_addflow.txt").write_text(
                            f"URL={page.url}\nSEL={sel}\n\n{b2[:8000]}")
                        # list form fields
                        fields = await page.evaluate("""() => [...document.querySelectorAll('input,textarea,select')]
                          .map(e => ({tag:e.tagName,type:e.type,name:e.name,placeholder:e.placeholder,id:e.id}))
                          .slice(0,40)""")
                        info["forms_seen"].append({"sel": sel, "url": page.url, "fields": fields,
                                                   "body_snippet": b2[:800]})
                        info["notes"].append(f"opened:{sel} NO_SUBMIT")
                        # navigate back to broker if left
                        if "/broker" not in page.url:
                            await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                            await asyncio.sleep(2)
                    except Exception as e:
                        info["notes"].append(f"click_fail:{sel}:{type(e).__name__}")
        except Exception:
            continue
    await shot(page, "liberty_broker_namesearch_addflow_final")
    entry["add_account_flow"] = info
    return info

async def explore_other_uis(page, ctx, entry):
    notes = []
    # click nav / menus
    nav_sels = [
        'a:has-text("Policies")', 'a:has-text("Accounts")', 'a:has-text("Search")',
        'a:has-text("Quotes")', 'a:has-text("Documents")', 'a:has-text("Renewals")',
        'button:has-text("Advanced")', 'a:has-text("Advanced")', 'text=Policy Search',
        'text=Find Policy', 'text=Quote Search', 'a:has-text("Clients")',
        'button:has-text("Filter")', 'a:has-text("Filter")',
    ]
    for sel in nav_sels:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible(timeout=600):
                await loc.click(); await asyncio.sleep(2)
                sn = f"liberty_broker_namesearch_nav_{re.sub(r'[^A-Za-z0-9]+','_',sel)[:28]}"
                await shot(page, sn)
                notes.append(f"nav_ok:{sel}->url={page.url}")
        except Exception as e:
            notes.append(f"nav_fail:{sel}:{type(e).__name__}")

    # Agents Portal
    loc = page.locator('a:has-text("Access Agents\' Portal"), a:has-text("Access Agents")').first
    if await loc.count():
        notes.append("agents_portal_link_present")
        try:
            async with ctx.expect_page(timeout=15000) as pi:
                await loc.click()
            np = await pi.value
            await np.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            await np.screenshot(path=str(SHOT_DIR / "liberty_broker_namesearch_agents_portal.png"), full_page=True)
            btxt = await np.inner_text("body")
            (SHOT_DIR / "liberty_broker_namesearch_agents_portal.txt").write_text(f"URL={np.url}\n\n{btxt[:8000]}")
            notes.append(f"agents_portal_url={np.url}")
            if "access denied" in btxt.lower():
                notes.append("agents_portal_access_denied")
            # try search boxes there
            for term in ["Pross", "WC5-33S", "02212061", "Wojtowicz"]:
                box = np.locator('input[type="search"], input[placeholder*="search" i], input[name*="search" i]').first
                if await box.count() and await box.is_visible(timeout=800):
                    await box.fill(term); await np.keyboard.press("Enter"); await asyncio.sleep(2.5)
                    safe = re.sub(r"[^A-Za-z0-9]+", "_", term)
                    await np.screenshot(path=str(SHOT_DIR / f"liberty_broker_namesearch_agents_{safe}.png"), full_page=True)
                    t2 = await np.inner_text("body")
                    cnt = parse_result_count(t2)
                    notes.append(f"agents_search:{term}:count={cnt}")
                    entry["queries"].append({
                        "kind": "agents_portal", "query": term, "result_count": cnt,
                        "url": np.url, "screenshot": f"liberty_broker_namesearch_agents_{safe}.png",
                        "notes": ["agents_portal"],
                    })
                    if cnt and cnt > 0:
                        entry["any_hit"] = True
            await np.close()
        except Exception as e:
            notes.append(f"agents_portal_err:{type(e).__name__}:{e}")
            # maybe same-tab nav
            try:
                await loc.click(); await asyncio.sleep(4)
                await shot(page, "liberty_broker_namesearch_agents_portal_sametab")
                notes.append(f"agents_same_tab_url={page.url}")
                await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                notes.append(f"agents_sametab_fail:{type(e2).__name__}")
    entry["other_ui_notes"] = notes
    return notes

async def try_download_if_hit(page, ctx, entry):
    downloaded = []
    dest = DOWNLOAD_DIR / f"liberty_pross_portal_{TS}_renewal_packet.pdf"
    # open first matching row/link
    for sel in [
        'a:has-text("Pross")', 'tr:has-text("Pross") a', 'a:has-text("Wojtowicz")',
        'a:has-text("WC5-33S")', 'a:has-text("B1X2Z3")', 'a:has-text("02212061")',
        'tr:has-text("WC5") a', 'a:has-text("Renewal")',
    ]:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=900):
                log(f"open hit {sel}")
                try:
                    async with ctx.expect_page(timeout=7000) as pi:
                        await loc.click()
                    np = await pi.value
                    await np.wait_for_load_state("domcontentloaded"); await asyncio.sleep(2)
                    page = np
                except Exception:
                    await loc.click(); await asyncio.sleep(2)
                await shot(page, "liberty_broker_namesearch_detail")
                await dump(page, "liberty_broker_namesearch_detail")
                break
        except Exception:
            continue
    for label in ["Download", "PDF", "Renewal Packet", "Renewal", "Proposal", "Quote",
                  "Packet", "Print", "Documents", "Forms", "Declarations", "View Document"]:
        locs = page.locator(f'a:has-text("{label}"), button:has-text("{label}")')
        for i in range(min(await locs.count(), 3)):
            try:
                async with page.expect_download(timeout=18000) as di:
                    await locs.nth(i).click()
                d = await di.value
                await d.save_as(str(dest))
                if dest.stat().st_size > 1000:
                    downloaded.append(str(dest))
                    log(f"download ok label={label} size={dest.stat().st_size}")
                    return downloaded, page
            except Exception:
                try:
                    async with ctx.expect_page(timeout=9000) as pi:
                        await locs.nth(i).click()
                    np = await pi.value
                    await np.wait_for_load_state("domcontentloaded"); await asyncio.sleep(2)
                    if "pdf" in np.url.lower():
                        dest.write_bytes(await (await np.request.get(np.url)).body())
                        if dest.stat().st_size > 1000:
                            downloaded.append(str(dest)); await np.close(); return downloaded, page
                    txt = (await np.inner_text("body"))[:4000]
                    if "Pross" in txt or "WC5" in txt:
                        dest.write_bytes(await np.pdf())
                        if dest.stat().st_size > 1000:
                            downloaded.append(str(dest)); await np.close(); return downloaded, page
                    await np.close()
                except Exception as e:
                    log(f"dl {label}[{i}] {type(e).__name__}")
    return downloaded, page

def merge_proof(entry):
    data = json.loads(PROOF.read_text()) if PROOF.exists() else {}
    data.setdefault("task", "portal_fetch_guard_liberty")
    data.setdefault("host", "hermes-poc-01")
    data["browser"] = "playwright_chromium_dedicated_context_DISPLAY_:99 (not EZLynx CDP 9222)"
    data["note"] = "No bind / no payment / no client email / no secrets or OTP digits in this proof"
    data["liberty_broker_name_search"] = entry
    data["finished_at"] = datetime.now(timezone.utc).isoformat()
    data.setdefault("existing_non_portal_files_not_claimed", [
        "data/downloads/carrier_renewals/liberty_pross_renewal_packet.pdf"
    ])
    PROOF.write_text(json.dumps(data, indent=2))
    log(f"WROTE {PROOF}")

async def main():
    entry = {
        "carrier": "Liberty Mutual",
        "portal": BROKER_URL,
        "insured": "Pross Construction LLC",
        "attempt": "name_search_multi_strategy",
        "success": False,
        "any_hit": False,
        "accounts_list_empty": None,
        "pdf_paths": [],
        "pdftotext_first_lines": {},
        "queries": [],
        "errors": [],
        "hitl_needed": False,
        "credentials_source": "GCP SM liberty_broker_username/password (+ storage_state if valid)",
        "screenshots_prefix": "liberty_broker_namesearch_*",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "final_url": None,
        "used_storage_state": False,
        "relgin_performed": False,
    }
    async with async_playwright() as p:
        b, ctx = await launch(p, use_state=True)
        page = await ctx.new_page()
        try:
            ok = False
            if STATE.exists():
                entry["used_storage_state"] = True
                ok = await session_ok(page)
                log(f"storage_state session_ok={ok} url={page.url}")
            if not ok:
                log("session expired or missing; re-login")
                await ctx.close(); await b.close()
                b, ctx = await launch(p, use_state=False)
                page = await ctx.new_page()
                entry["relgin_performed"] = True
                if not await do_login(page, entry):
                    entry["final_url"] = page.url
                    return
                await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)

            await shot(page, "liberty_broker_namesearch_home")
            home_body = await dump(page, "liberty_broker_namesearch_home")
            entry["final_url"] = page.url
            # baseline accounts empty?
            baseline = parse_result_count(home_body)
            entry["accounts_list_empty"] = (baseline == 0) or ("0 Items" in home_body)
            entry["baseline_items"] = baseline
            log(f"baseline items={baseline} empty={entry['accounts_list_empty']}")

            # inventory visible UI controls
            ui = await page.evaluate("""() => {
              const pick = (nodes) => [...nodes].slice(0,80).map(n => ({
                tag: n.tagName, text: (n.innerText||n.value||'').trim().slice(0,80),
                href: n.getAttribute && n.getAttribute('href'),
                type: n.type, placeholder: n.placeholder, name: n.name, id: n.id,
                aria: n.getAttribute && n.getAttribute('aria-label')
              })).filter(x => (x.text||x.placeholder||x.name||x.aria));
              return {
                links: pick(document.querySelectorAll('a,button')),
                inputs: pick(document.querySelectorAll('input,select,textarea')),
              };
            }""")
            entry["home_ui_inventory"] = ui
            (SHOT_DIR / "liberty_broker_namesearch_ui_inventory.json").write_text(json.dumps(ui, indent=2))

            # Run all queries on Client Accounts Search Clients box
            for i, (kind, term) in enumerate(QUERIES, 1):
                # ensure on broker home between searches
                if "/broker" not in page.url:
                    await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(1.5)
                await run_one_search(page, kind, term, i, entry["queries"])
                last = entry["queries"][-1]
                if last.get("result_count") and last["result_count"] > 0:
                    entry["any_hit"] = True

            # Other search UIs / menus
            if "/broker" not in page.url:
                await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
            await explore_other_uis(page, ctx, entry)

            # Add account / request access documentation (no submit)
            if "/broker" not in page.url:
                await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
            await document_add_account(page, entry)

            if entry["any_hit"]:
                # re-run best hit query then download
                hit = next((q for q in entry["queries"] if q.get("result_count") and q["result_count"] > 0), None)
                if hit:
                    await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(1)
                    await run_one_search(page, hit["kind"], hit["query"], 99, [])
                downloaded, page = await try_download_if_hit(page, ctx, entry)
                entry["final_url"] = page.url
                if downloaded:
                    entry["success"] = True
                    entry["pdf_paths"] = downloaded
                    for pth in downloaded:
                        entry["pdftotext_first_lines"][Path(pth).name] = pdf_lines(pth)
                else:
                    entry["hitl_needed"] = True
                    entry["errors"].append("HIT found but could not download renewal packet")
            else:
                entry["hitl_needed"] = True
                entry["errors"].append(
                    "HITL: multi-strategy name/address/policy/quote search on broker Client Accounts "
                    "returned 0 for all queries; accounts list still empty. Add-account/email-to-Liberty "
                    "flow documented but NOT submitted. See liberty_broker_namesearch_* screenshots."
                )

            await shot(page, "liberty_broker_namesearch_final")
            await dump(page, "liberty_broker_namesearch_final")
            entry["final_url"] = page.url
            # recompute accounts empty from last home view if possible
            try:
                await page.goto(BROKER_URL, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
                fb = await page.inner_text("body")
                entry["accounts_list_empty"] = ("0 Items" in fb) or parse_result_count(fb) == 0
            except Exception:
                pass
        except Exception as e:
            entry["errors"].append(f"exception:{type(e).__name__}:{e}")
            entry["hitl_needed"] = True
            entry["final_url"] = getattr(page, "url", None)
            log(traceback.format_exc())
            try:
                await shot(page, "liberty_broker_namesearch_exc"); await dump(page, "liberty_broker_namesearch_exc")
            except Exception:
                pass
        finally:
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            entry["screenshots"] = sorted(p.name for p in SHOT_DIR.glob("liberty_broker_namesearch_*.png"))
            # compact query summary for parent
            entry["query_summary"] = [
                {"kind": q["kind"], "query": q["query"], "result_count": q.get("result_count"),
                 "url": q.get("url"), "notes": q.get("notes")}
                for q in entry["queries"]
            ]
            merge_proof(entry)
            await ctx.close(); await b.close()
            log(f"RESULT success={entry['success']} any_hit={entry['any_hit']} empty={entry['accounts_list_empty']} "
                f"hitl={entry['hitl_needed']} pdfs={entry['pdf_paths']} queries={len(entry['queries'])} "
                f"url={entry['final_url']} errors={entry['errors']}")

if __name__ == "__main__":
    asyncio.run(main())
