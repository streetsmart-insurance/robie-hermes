import asyncio
import json
import base64
import subprocess
import time
import re
from playwright.sync_api import sync_playwright
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def get_guard_code(start_time):
    with open("/opt/streetsmart-hermes/.hermes/robie_token.json") as f:
        info = json.load(f)
    creds = Credentials.from_authorized_user_info(info)
    service = build("gmail", "v1", credentials=creds)
    for _ in range(30):
        time.sleep(3)
        res = service.users().messages().list(userId="me", q="from:DoNotReply@guard.com newer_than:5m", maxResults=3).execute()
        for m in res.get("messages", []):
            msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            internal_date = int(msg.get("internalDate", 0)) / 1000.0
            if internal_date >= start_time - 15:
                def get_text(part):
                    if part.get("body", {}).get("data"):
                        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                    for sp in part.get("parts", []):
                        t = get_text(sp)
                        if t: return t
                    return ""
                body = get_text(msg.get("payload", {}))
                match = re.search(r"<span style=\"[^\"]*font-size:\s*32px[^\"]*\">(\d{6})</span>", body)
                if not match:
                    match = re.search(r"\b(\d{6})\b", body)
                if match:
                    return match.group(1)
    return None

user = subprocess.check_output("gcloud secrets versions access latest --secret=guard_robie_username --project=streetsmart-hermes-poc", shell=True).decode().strip()
pwd = subprocess.check_output("gcloud secrets versions access latest --secret=guard_robie_password --project=streetsmart-hermes-poc", shell=True).decode().strip()

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox", "--disable-gpu"])
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    
    t0 = time.time()
    page.goto("https://gigezrate.guard.com/auth", timeout=20000)
    page.fill("input[name=Username], input#Username", user)
    page.fill("input[name=Password], input#Password", pwd)
    page.locator("input[type=submit], button[type=submit], button:has-text(\"Log In\"), button:has-text(\"Submit\")").first.click()
    
    time.sleep(4)
    print("URL after login submit:", page.url)
    if "verify" in page.url:
        print("Waiting for 2FA code from DoNotReply@guard.com...")
        code = get_guard_code(t0)
        print(f"Extracted verification code: {code}")
        if code:
            inputs = page.locator("input:not([type=hidden]):not([type=checkbox])")
            print("Visible inputs count:", inputs.count())
            inputs.first.fill(code)
            remember_cb = page.locator("input[type=checkbox]")
            if remember_cb.count() > 0:
                remember_cb.first.check()
            page.locator("button:has-text(\"CONTINUE\"), input[value*=\"CONTINUE\"], button[type=submit]").first.click()
            time.sleep(8)
            print("URL after 2FA:", page.url)
            print("Title after 2FA:", page.title())
            page.screenshot(path="/tmp/guard_portal_home.png")
            context.storage_state(path="/opt/renewal-automation-system/data/guard_storage_state.json")
            print("Saved session to /opt/renewal-automation-system/data/guard_storage_state.json")
    browser.close()
