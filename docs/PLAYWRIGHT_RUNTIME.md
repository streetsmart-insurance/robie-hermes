# Robie Playwright runtime overlay

This release carries the deterministic browser integration as a reviewable
overlay. It does not modify a live Hermes installation in place.

## Release files

- `deploy/hermes/tools/playwright_tool.py` registers the `playwright_exec`
  toolset and connects only to the configured persistent Chrome CDP endpoint.
- `deploy/hermes/skills/robie-playwright-browser/SKILL.md` defines locator,
  assertion, evidence, and fail-closed rules.
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
