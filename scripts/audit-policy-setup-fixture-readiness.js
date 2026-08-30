#!/usr/bin/env node
"use strict";

// Read-only readiness audit for sanitized EZLynx Policy Setup fixtures.
// This probe cannot fill or click a consequential control. It opens a fresh
// page in the authenticated Test browser, emits only generic locator metadata,
// and closes that page before exiting.

const fs = require("fs");
const os = require("os");
const { chromium } = require("playwright");

const EXPECTED_HOST = "hermes-test-01";
const EXPECTED_ROOT = "/opt/streetsmart-hermes-test";
const EZLYNX_ROOT = "https://app.ezlynx.com/web/";
const SAFE_TERMS = ["applicant", "account", "policy", "add", "new", "search"];

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`missing required argument ${name}`);
  }
  return process.argv[index + 1];
}

function safePath(rawUrl) {
  const parsed = new URL(rawUrl);
  return `/${parsed.pathname.split("/").filter(Boolean).map((part) =>
    /^\d{6,}$/.test(part) ? "<id>" : part
  ).join("/")}`;
}

function requireTestRuntime(expectedSha) {
  if (os.hostname().split(".", 1)[0] !== EXPECTED_HOST) {
    throw new Error("fixture audit refuses every host except hermes-test-01");
  }
  if ((process.env.ROBIE_ENV || "").trim().toUpperCase() !== "TEST") {
    throw new Error("fixture audit requires ROBIE_ENV=TEST");
  }
  const current = fs.realpathSync(`${EXPECTED_ROOT}/current`);
  const releasesCurrent = fs.realpathSync(`${EXPECTED_ROOT}/releases/current`);
  if (current !== releasesCurrent) {
    throw new Error("Test release pointers disagree");
  }
  const expectedPrefix = `${EXPECTED_ROOT}/releases/${expectedSha}/`;
  if (!current.startsWith(expectedPrefix)) {
    throw new Error("Test release does not match the approved fixture-audit revision");
  }
  return current;
}

async function audit(cdpUrl) {
  const browser = await chromium.connectOverCDP(cdpUrl, { timeout: 15000 });
  const pages = browser.contexts().flatMap((context) => context.pages());
  const ezlynxPages = pages.filter((page) => {
    try {
      return new URL(page.url()).hostname.toLowerCase().endsWith("ezlynx.com");
    } catch (_) {
      return false;
    }
  });
  if (ezlynxPages.length !== 1) {
    throw new Error(
      `PLAYWRIGHT_BLOCKED: expected exactly one EZLynx Test tab; observed ${ezlynxPages.length}`
    );
  }
  const seed = ezlynxPages[0];
  const seedUrl = seed.url();
  if (/login|signin/i.test(seedUrl)) {
    throw new Error("AUTH_CHALLENGE: EZLynx Test session is expired");
  }
  if (await seed.getByRole("textbox", { name: "Password", exact: true }).count()) {
    throw new Error("AUTH_CHALLENGE: EZLynx Test session is expired");
  }

  const page = await seed.context().newPage();
  try {
    await page.goto(EZLYNX_ROOT, { waitUntil: "domcontentloaded", timeout: 20000 });
    if (/login|signin/i.test(page.url())) {
      throw new Error("AUTH_CHALLENGE: Test navigation reached login");
    }
    const candidates = [];
    for (const role of ["link", "button"]) {
      const locator = page.getByRole(role);
      const count = await locator.count();
      for (let index = 0; index < count; index += 1) {
        const item = locator.nth(index);
        let label = (await item.innerText({ timeout: 2000 })).replace(/\s+/g, " ").trim();
        if (!SAFE_TERMS.some((term) => label.toLowerCase().includes(term))) continue;
        if (label.length > 80) label = `${label.slice(0, 77)}...`;
        const href = await item.getAttribute("href");
        let hrefPath = null;
        if (href) {
          try {
            const parsed = new URL(href, page.url());
            if (parsed.hostname.toLowerCase().endsWith("ezlynx.com")) hrefPath = safePath(parsed.href);
          } catch (_) {
            hrefPath = null;
          }
        }
        candidates.push({ role, text: label, href_path: hrefPath });
      }
    }
    const unique = [...new Map(candidates.map((item) => [JSON.stringify(item), item])).values()];
    return {
      result: "TEST READINESS VERIFIED",
      browser_tabs: pages.length,
      ezlynx_tabs: ezlynxPages.length,
      authenticated: true,
      seed_host: new URL(seedUrl).hostname,
      seed_path: safePath(seedUrl),
      landing_host: new URL(page.url()).hostname,
      landing_path: safePath(page.url()),
      locator_candidates: unique,
      consequential_writes: 0,
      production_touched: false,
    };
  } finally {
    await page.close();
  }
}

async function main() {
  const expectedSha = argument("--expected-sha");
  const cdpUrl = argument("--cdp-url");
  const current = requireTestRuntime(expectedSha);
  const result = await audit(cdpUrl);
  result.test_release = current;
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
