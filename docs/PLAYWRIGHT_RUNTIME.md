# Robie Playwright runtime overlay

This release carries the deterministic browser integration as a reviewable
overlay. It does not modify a live Hermes installation in place.

## Release files

- `deploy/hermes/tools/playwright_tool.py` registers the `playwright_exec`
  toolset, connects only to the configured persistent Chrome CDP endpoint,
  and refuses form writes unless the locator uniquely identifies one field.
  An empty PDF (`EmptyFileError` / pypdf empty file) or a missing
  `/tmp/playwright-artifacts-*` file is `PLAYWRIGHT_FAIL_CLOSED` once — not
  a retryable `PLAYWRIGHT_BLOCKED`. Production Hermes loads this file from
  `/opt/streetsmart-hermes/.hermes/hermes-agent/tools/playwright_tool.py`;
  a zip-only deploy does not install it. Copy the overlay onto that .hermes
  path or the next install will not run this fail-closed remap.
- `deploy/hermes/tools/playwright_write_guard.py` is the unique-write guard
  installed into every `playwright_exec` run. A blocked write asks Gemini
  for one unique visible label and applies that locator only if unique-write
  still passes; otherwise it HITLs Carlo.
- `deploy/hermes/skills/robie-playwright-browser/SKILL.md` defines locator,
  assertion, evidence, and fail-closed rules.
- `robie_job_engine/ezlynx_account_nav.py` is the zip-path helper that
  `build_chat_execution_text` loads. When the Job already names an EZLynx
  account id, first navigation is `/web/account/<id>/…`. Search-locator
  failure with a known id is one direct-URL fallback; with no id it is HITL /
  stuck. A zip flip loads this module. Skills and SOUL below are `.hermes`
  copies and are not updated by a zip flip.
- `deploy/hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md` requires
  Save and Continue Edit after a commercial auto SHELL, quote vehicles /
  drivers / garaging / symbols / limits / banks, titled-discussion notes
  that include `Robie was here`, and no bind. It also forbids applicant
  search / URL-guess loops when an account id is already known.
- `deploy/hermes/skills/ezlynx-gemini-fallback/SKILL.md` plus
  `robie_job_engine/gemini_field_helper.py` are the fail-closed stuck-field
  hook: after `PLAYWRIGHT_BLOCKED`, ask Gemini for one unique field, then
  HITL if Gemini is unsure. Unique-write is not weakened.
- `deploy/hermes/config/playwright.yaml` is the minimal configuration fragment
  that exposes the tool to CLI and Google Chat profiles.
- `deploy/hermes/SOUL.playwright.md` is the browser invariant to merge into the
  deployed Robie identity file.
- `deploy/systemd/robie-chrome-refresh.service` and `.timer` refresh the
  persistent browser on a bounded daily schedule.

## Deployment contract

Install the overlay first in the isolated Test profile using the Test deployment
identity. The tool file belongs in the installed Hermes tools directory, the
Skill belongs in the profile's Skills directory, and the two YAML/Markdown
fragments must be merged without replacing unrelated configuration or identity
rules. Set `ROBIE_PLAYWRIGHT_CDP_URL` to the Test browser's loopback endpoint.

The rollout must verify all of the following before an immutable digest can be
approved for Production:

1. `playwright_exec` attaches to the intended persistent Chrome context.
2. Recorder start does not return until Playwright writes the first video frame.
3. A failed attach or first-frame timeout prevents the worker from running.
4. Five successful and five deliberately failed Jobs have final statuses,
   verifier evidence, and numbered Drive recording links.
5. Authentication, upload failure, retry, and worker-restart cases are persisted
   and displayed correctly in the Control Center.
6. The Jobs sheet is updated by Job ID without clearing unrelated rows.

Production must receive the exact archive digest verified in Test. Do not copy
these files directly into a live Production checkout.
