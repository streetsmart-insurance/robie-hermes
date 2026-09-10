"""Unit and integration tests for Robie Stealth Browser Engine."""

import pytest
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

from src.portals.stealth_browser import (
    create_stealth_carrier_context,
    get_default_user_agent,
    CarrierCircuitBreaker,
    detect_waf_challenge,
    human_type,
    human_click,
    cleanup_zombie_browsers,
    DEFAULT_PROFILE_ROOT,
)


def test_carrier_circuit_breaker():
    """Verifies that circuit breaker records blocks and respects cooldowns."""
    test_carrier = "TestCarrierWAF"
    assert not CarrierCircuitBreaker.is_blocked(test_carrier)[0]

    CarrierCircuitBreaker.record_block(test_carrier, "Cloudflare Turnstile Challenge")
    is_blocked, remaining = CarrierCircuitBreaker.is_blocked(test_carrier)
    assert is_blocked is True
    assert remaining > 0

    # Clean up test carrier
    CarrierCircuitBreaker._blocks.pop(test_carrier.lower(), None)


def test_default_user_agent():
    """Verifies realistic desktop user agent selection."""
    ua = get_default_user_agent()
    assert "Mozilla/5.0" in ua
    assert "Chrome/" in ua
    assert ("Macintosh" in ua or "Windows NT" in ua or "X11; Linux" in ua)


@pytest.mark.asyncio
async def test_stealth_evasion_flags():
    """Verifies that stealth browser initializes with headless=new and neutralizes bot leaks."""
    async with async_playwright() as p:
        context = await create_stealth_carrier_context(
            playwright=p,
            carrier_key="test_stealth_probe",
            headless=True,
        )
        try:
            page = await context.new_page()

            # Test 1: navigator.webdriver should NOT be True
            webdriver_val = await page.evaluate("() => navigator.webdriver")
            assert webdriver_val is not True, f"navigator.webdriver leaked: {webdriver_val}"

            # Test 2: window.chrome runtime object should be present
            has_chrome = await page.evaluate("() => Boolean(window.chrome)")
            assert has_chrome is True, "window.chrome is missing"

            # Test 3: navigator.plugins should be populated
            plugins_count = await page.evaluate("() => navigator.plugins.length")
            assert plugins_count > 0, "navigator.plugins is empty"

            # Test 4: navigator.languages should have en-US
            languages = await page.evaluate("() => Array.from(navigator.languages)")
            assert "en-US" in languages

        finally:
            await context.close()


@pytest.mark.asyncio
async def test_human_interaction_simulation():
    """Verifies that human typing and clicking execute naturally on a test form."""
    async with async_playwright() as p:
        context = await create_stealth_carrier_context(
            playwright=p,
            carrier_key="test_human_interaction",
            headless=True,
        )
        try:
            page = await context.new_page()
            # Set up a lightweight HTML form
            await page.set_content("""
                <html>
                    <body>
                        <form id="login-form">
                            <input id="user-input" type="text" />
                            <button id="submit-btn" type="button" onclick="window.clicked = true">Submit</button>
                        </form>
                    </body>
                </html>
            """)

            # Test human_type
            test_text = "RobieAgent2026"
            await human_type(page, "input#user-input", test_text, min_delay_ms=10, max_delay_ms=30)
            val = await page.input_value("input#user-input")
            assert val == test_text

            # Test human_click
            await human_click(page, "button#submit-btn")
            clicked = await page.evaluate("() => window.clicked")
            assert clicked is True

        finally:
            await context.close()


@pytest.mark.asyncio
async def test_waf_challenge_detection():
    """Verifies that detect_waf_challenge flags Cloudflare challenges."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            # Set mock Cloudflare challenge title & body
            await page.set_content("""
                <html>
                    <head><title>Just a moment... | Cloudflare</title></head>
                    <body>
                        <div>Please verify you are human to continue.</div>
                        <div id="cf-turnstile"></div>
                    </body>
                </html>
            """)

            is_waf, reason = await detect_waf_challenge(page)
            assert is_waf is True
            assert "Cloudflare" in reason

        finally:
            await browser.close()


def test_cleanup_zombie_browsers():
    """Verifies process reaper executes safely without errors."""
    # Should run cleanly without raising exceptions
    cleanup_zombie_browsers(max_age_seconds=999999)


@pytest.mark.asyncio
async def test_optimize_route_performance():
    """Verifies that route optimization interceptor blocks tracking domains."""
    from src.portals.stealth_browser import optimize_route_performance
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await optimize_route_performance(context, block_trackers=True)
        page = await context.new_page()
        try:
            blocked_urls = []
            page.on("requestfailed", lambda req: blocked_urls.append(req.url))

            # Attempt to load a tracker domain
            try:
                await page.goto("data:text/html,<script src='https://www.google-analytics.com/analytics.js'></script>", timeout=3000)
            except Exception:
                pass
            assert any("google-analytics.com" in u for u in blocked_urls)
        finally:
            await browser.close()


def test_optimal_browser_concurrency(monkeypatch):
    """Verifies concurrency auto-scales based on available CPU cores."""
    from src.portals.stealth_browser import get_optimal_browser_concurrency

    # Case 1: 8 cores -> 4 browsers
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    monkeypatch.delenv("ROBIE_MAX_CONCURRENT_BROWSERS", raising=False)
    assert get_optimal_browser_concurrency() == 4

    # Case 2: 4 cores -> 2 browsers
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    assert get_optimal_browser_concurrency() == 2

    # Case 3: Explicit env override
    monkeypatch.setenv("ROBIE_MAX_CONCURRENT_BROWSERS", "6")
    assert get_optimal_browser_concurrency() == 6


