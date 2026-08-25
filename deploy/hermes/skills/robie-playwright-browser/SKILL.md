---
name: "robie-playwright-browser"
description: "Use Robie's approved Playwright runner for authenticated browser workflows; enforce deterministic locators, assertions, evidence, and fail-closed behavior."
---

# Robie Playwright Browser Contract

Use this skill for Robie workflows that operate a signed-in website. The user
teaches the business workflow; Robie owns the browser mechanics.

## Engine contract

- Use `playwright_exec` or a task Skill's tested Playwright script.
- Do not silently substitute browser-harness, raw CDP or WebSocket commands,
  Selenium, Puppeteer, or coordinate-only clicking.
- If Playwright cannot attach to the intended signed-in session or a required
  assertion fails, stop with `PLAYWRIGHT_BLOCKED` and the exact failed check.
- Do not install or upgrade browser software during an executable Job.

## Reliable workflow

1. Reuse an existing signed-in tab whose URL and account context match.
2. Locate elements by stable roles, labels, test IDs, or documented selectors.
3. Assert the URL, client or account, heading, and critical values before work.
4. After every navigation, save, upload, selection, or sort, wait for and assert
   the authoritative state change.
5. For tables, assert page size, row selector, total, sort state, and boundary
   rows before classification.
6. Before an external side effect, show the prepared destination and payload and
   obtain any confirmation required by the task Skill.
7. Preserve assertion results and appropriate screenshots as diagnostic evidence.

## Completion gate

Browser assertions and recordings are evidence, not completion authority. The
Job Engine must independently reread the destination through its registered
verifier. A run may become `COMPLETE` only after that verification succeeds and
every required recording segment has a stored Drive link.
