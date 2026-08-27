---
name: "ezlynx-commercial-auto-from-quote"
description: "Finish a commercial auto policy from an existing Progressive quote. A policy SHELL is not done. After the shell, Save and Continue Edit, then enter vehicles, drivers, garaging, symbols, limits, and banks from the quote. Notes only on titled discussions. Do not bind."
job_type: "ezlynx.commercial_auto"
production_ready: true
---

# EZLynx commercial auto from quote

Use this Skill when Robie creates or continues a commercial auto policy from
a Progressive (or other carrier) quote already on the EZLynx file. The live
failure this Skill prevents: Robie created a commercial auto SHELL on
ROBIE Test LLC (220250093) and treated the shell as finished. Vehicles,
drivers, and coverages from the quote were never entered.

This is a backend workflow Skill, not Chat small talk.

## Hard stops

- Do not bind.
- Do not take payment.
- Do not invent coverage, symbols, limits, vehicles, drivers, or garaging.
- Do not change the named insured. Keep the named insured already on the file.
- Do not use `.first`, `.nth()`, or `.last` to pick a field. Unique-write stays
  fail-closed. A write that cannot uniquely name its field is
  `PLAYWRIGHT_BLOCKED`.
- Do not treat a policy SHELL as a completed commercial auto policy.

## Open an existing EZLynx account

If the Job or prompt already names an EZLynx account or applicant id, navigate
directly to `https://app.ezlynx.com/web/account/<id>/policies` (or the
`/web/account/<id>/…` URL already in the task). Do not open a search box.
Do not guess Summary, Details, or Index URL variants. Job de9c530a burned
36+ minutes doing that after the search locator failed for account 220250093.

If search is required and the search locator fails:

- Known id → one direct `/web/account/<id>/` navigation, then stop or proceed.
  Never enumerate URL guesses.
- No id → HITL Carlo or say stuck / no verified progress. Do not retry URL
  guesses.

Do not claim fills, saves, or COMPLETE without a destination-verified evidence
row. Unique-write / PLAYWRIGHT_BLOCKED / FAIL_CLOSED stay. Never invent LOB
steps. Never bind.

## After the shell — required

Creating the commercial auto policy record is only the shell. It is not done.

1. After the shell exists, click **Save and Continue Edit**. If that control
   is not present, use **Actions → Edit** and continue the same policy.
2. Stay on that exact policy. Confirm the named insured, account, and policy
   type before any write.
3. From the quote, enter every required screen. Do not skip a screen because
   the shell already exists:
   - vehicles (VIN, year, make, model, and every quote vehicle)
   - drivers
   - garaging
   - symbols
   - limits
   - banks / loss payees when the quote names them
4. Use only values that appear on the quote or the existing file. If a required
   quote value is missing or unreadable, stop and HITL Carlo. Do not guess.
5. After each save, reopen the exact policy and reread the server-backed
   state. A shell with empty vehicles, drivers, or coverages is not complete.

## Notes — titled discussions only

This note rule applies to every EZLynx note write, not only commercial auto.

- Write notes only on an existing titled discussion.
- Allowed titles include **New Business**, **New Policy**, **Renewal**,
  **Cancellation**, **Submission Center**, and other existing titled
  discussions on the file.
- Never write a note on **Untitled**.
- If the right titled discussion is missing, create a properly named
  discussion first, then write the note there.
- Every Robie note must include the exact phrase `Robie was here`.

## Blocked popup or selector

When unique-write blocks the write, or a modal/selector cannot be uniquely
named, follow `ezlynx-gemini-fallback`. Do **not** only stop and report.

The unique-write guard asks Gemini for one unique visible label, then applies
that locator only if unique-write still passes. If Gemini is unsure, missing,
or the locator is not unique, HITL Carlo. Never guess a field.

## Completion

A commercial auto-from-quote Job may be reported done only after a fresh
read shows the named insured unchanged and the quote vehicles, drivers,
garaging, symbols, limits, and required banks on that same policy. A shell
alone is not done. Bind is out of scope.

If there is no destination-action checkpoint and no destination-verified
evidence, say you were stuck and made no verified progress. Do not write
"Filling Policy Shell", "I identified" a carrier, or remaining-shell prose
from the quote alone. Those sentences are not destination evidence.
