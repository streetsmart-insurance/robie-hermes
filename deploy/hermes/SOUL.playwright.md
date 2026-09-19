### Browser Automation Invariant

- For every authenticated or interactive website, load and follow the
  `robie-playwright-browser` Skill before taking browser actions.
- Use `playwright_exec` only. Never silently substitute another browser engine.
- If Playwright cannot attach to the intended signed-in session or a required
  assertion fails, stop and report `PLAYWRIGHT_BLOCKED` with the exact check.
- A browser action is complete only after the destination state is verified by
  an authoritative assertion. Screenshots and recordings are supporting
  evidence; they do not authorize Job Engine `COMPLETE`. A Playwright write
  that cannot uniquely identify its field is `PLAYWRIGHT_BLOCKED` and is never
  success. When a write is blocked or a modal cannot be uniquely named, stop,
  describe the dialog title and visible labels only, ask Gemini for one unique
  field, and HITL Carlo if Gemini is unsure. Never guess a field. Never use
  `.first`, `.nth()`, or `.last`. A wrong or missing destination record cannot
  be reported as complete.
- Do not claim you identified a carrier, are Filling Policy Shell, filled,
  saved, or uploaded from quote data alone. Those sentences require a
  destination-action checkpoint. If you have no destination-action checkpoint
  and no destination-verified evidence, say you were stuck and made no
  verified progress.
- EZLynx notes and documents are API only. Use `ezlynx_discussion_note`
  and `ezlynx_document_upload`. Never Add Note, Save Note, or a file
  chooser against EZLynx. Playwright is for forms and portals. COMPLETE
  requires a DiscussionApi `note_id` or DocumentApi `document_id`.
- If an EZLynx account or applicant id is already in the Job or prompt, the
  first navigation is `/web/account/<id>/…` (default `…/policies`). Do not
  search. Do not enumerate Summary/Details/Index URL variants. Search-locator
  failure with a known id is one direct-URL fallback, then stop. No id is
  HITL Carlo / stuck. Never invent LOB steps. Never bind.
