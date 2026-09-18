---
name: "robie-playwright-browser"
description: "Use Robie's approved Playwright runner for authenticated browser workflows; enforce deterministic locators, assertions, evidence, and fail-closed behavior."
---

# Robie Playwright Browser Contract

Use this skill for Robie workflows that operate a signed-in website. The user
teaches the business workflow; Robie owns the browser mechanics.

## Engine contract

- Use `playwright_exec` only.
- Do not silently substitute browser-harness, raw CDP or WebSocket commands,
  Selenium, Puppeteer, or coordinate-only clicking.
- If Playwright cannot attach to the intended signed-in session or a required
  assertion fails, stop with `PLAYWRIGHT_BLOCKED` and the exact failed check.
- Do not install or upgrade browser software during an executable Job.

## Open an existing EZLynx account

If the Job or prompt already names an EZLynx account or applicant id, the
first navigation is `https://app.ezlynx.com/web/account/<id>/policies` (or
the `/web/account/<id>/…` URL already in the task). Do not treat "open
applicant" as a search-box problem. Do not enumerate Summary, Details, or Index URL variants.

If a search locator fails and an account id is already known, try that
direct account URL once, then stop or proceed. If no account id is known,
HITL Carlo or say stuck / no verified progress. Never spend the Job
URL-guessing.

## Reliable workflow

1. Reuse an existing signed-in tab whose URL and account context match.
2. Locate elements by stable roles, labels, test IDs, or documented selectors.
   A write is allowed only when that locator uniquely identifies exactly one
   field. `.first`, `.nth()`, `.last`, and other positional guesses are
   `PLAYWRIGHT_BLOCKED`; do not fill a field you cannot uniquely name.
   When a write is blocked or a modal cannot be uniquely named on any
   Playwright site, follow `ezlynx-gemini-fallback`: stop, describe the
   dialog title and visible labels only (no passwords), ask Gemini for one
   unique field, and HITL Carlo if Gemini is unsure. Never guess a field.
3. Assert the URL, client or account, heading, and critical values before work.
4. After every navigation, save, upload, selection, or sort, wait for and assert
   the authoritative state change.
5. For tables, assert page size, row selector, total, sort state, and boundary
   rows before classification.
6. Before an external side effect, show the prepared destination and payload and
   obtain any confirmation required by the task Skill.
7. Preserve assertion results and appropriate screenshots as diagnostic evidence.

## Never delete (cardinal rule)

ROBIE never deletes. Do not click Delete / Remove / Void / Terminate /
Cancel-policy controls, and do not press the Delete or Backspace key on a
policy, account, client, or coverage record. The write guard refuses these
clicks and key presses outright and the refusal cannot be overridden by
Gemini or by HITL. (A plain dialog "Cancel" button with no policy context,
and Backspace/Delete typed inside an ordinary text field, are not
deletion-shaped and stay allowed.) If a task seems to require deletion, stop
and HITL Carlo instead.

## Completion gate

Browser assertions and recordings are evidence, not completion authority. The
Job Engine must independently reread the destination through its registered
verifier. A run may become `COMPLETE` only after that verification succeeds and
every required recording segment has a stored Drive link.

## Carrier browser runtime policy — Hartford is sandbox-only (permanent, 2026-09-18)

Hartford (`thehartford.com`, EBC agent portal) is unreachable from the ROBIE
servers — proven by curl probes on hermes-poc-01 on 2026-09-18 (direct, via the
residential proxy, and via proxy forced HTTP/1.1 all fail; independently
re-proven by Dusty the same day). The sandbox browser reaches it fine.

- Never start a Hartford portal / EBC Playwright job on a hermes-* server. The
  job engine refuses it fail-closed with a plain-English reason; see
  `robie_job_engine/carrier_browser_policy.py`, which owns the rule.
- Run Hartford browser work in the sandbox browser, or get the documents by
  email / IVANS.
- Do not debug proxy or stealth settings for Hartford on the servers — that
  path is dead.
- All other carriers keep the designed stealth+proxy browser path unchanged.
