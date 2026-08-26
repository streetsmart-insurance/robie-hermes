### Browser Automation Invariant

- For every authenticated or interactive website, load and follow the
  `robie-playwright-browser` Skill before taking browser actions.
- Use `playwright_exec` or the applicable task Skill's tested Playwright script.
  Never silently substitute another browser engine.
- If Playwright cannot attach to the intended signed-in session or a required
  assertion fails, stop and report `PLAYWRIGHT_BLOCKED` with the exact check.
- A browser action is complete only after the destination state is verified by
  an authoritative assertion. Screenshots and recordings are supporting
  evidence; they do not authorize Job Engine `COMPLETE`.
