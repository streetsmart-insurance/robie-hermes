from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
    for context_index, context in enumerate(browser.contexts):
        for page_index, page in enumerate(context.pages):
            print(f"PAGE {context_index}:{page_index}")
            print(f"URL {page.url}")
            try:
                print(f"TITLE {page.title()}")
                body = page.locator("body").inner_text(timeout=10_000)
                print("BODY_START")
                print(body[:8_000])
                print("BODY_END")
                for selector in ("button", "input", "a", "label", "select", "[role=button]"):
                    locator = page.locator(selector)
                    print(f"SELECTOR {selector} COUNT {locator.count()}")
                    for index in range(min(locator.count(), 40)):
                        item = locator.nth(index)
                        try:
                            print(
                                "ELEMENT",
                                selector,
                                index,
                                {
                                    "text": item.inner_text(timeout=1_000)[:300],
                                    "type": item.get_attribute("type"),
                                    "name": item.get_attribute("name"),
                                    "id": item.get_attribute("id"),
                                    "value": item.get_attribute("value"),
                                    "aria": item.get_attribute("aria-label"),
                                },
                            )
                        except Exception as exc:
                            print(f"ELEMENT_ERROR {selector} {index} {type(exc).__name__}")
                terms = (
                    "workers", "quoted", "premium", "hartford", "razza",
                    "robie was here", "untitled", "discussion", "attachment",
                    "document", "24,622", "24622",
                )
                for line_number, line in enumerate(body.splitlines(), start=1):
                    if any(term in line.lower() for term in terms):
                        print(f"MATCH {line_number}: {line}")
            except Exception as exc:
                print(f"READ_ERROR {type(exc).__name__}: {exc}")
