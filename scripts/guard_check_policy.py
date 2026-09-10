import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox", "--disable-gpu"])
    context = browser.new_context(
        storage_state="/opt/renewal-automation-system/data/guard_storage_state.json",
        viewport={"width": 1440, "height": 900}
    )
    page = context.new_page()
    page.goto("https://gigezrate.guard.com/auth/terms?flow=l", timeout=20000)
    time.sleep(3)
    print("URL:", page.url)
    print("Title:", page.title())
    
    # Remove cookie overlay if present
    page.evaluate("""() => {
        var ot = document.getElementById(onetrust-consent-sdk);
        if (ot && ot.parentNode) { ot.parentNode.removeChild(ot); }
        var otBkg = document.querySelector(.onetrust-pc-dark-filter);
        if (otBkg && otBkg.parentNode) { otBkg.parentNode.removeChild(otBkg); }
    }""")
    time.sleep(1)
    
    # Click Accept
    accept_btn = page.locator("button[value=Accept], button:has-text(Accept)")
    if accept_btn.count() > 0:
        print("Found accept button, clicking with force=True...")
        accept_btn.first.click(force=True)
        time.sleep(8)
        print("URL after accept:", page.url)
        context.storage_state(path="/opt/renewal-automation-system/data/guard_storage_state.json")
    
    page.screenshot(path="/tmp/guard_dashboard.png")
    print("Current URL:", page.url)
    print("Current Title:", page.title())
    
    # Print links / navigation
    nav_links = page.locator("a, button")
    print("Navigation items count:", nav_links.count())
    for i in range(min(30, nav_links.count())):
        t = nav_links.nth(i).inner_text().strip()
        h = nav_links.nth(i).get_attribute("href") or ""
        if t or h:
            print(f"  Nav: {t} -> {h}")
            
    browser.close()
