---
name: "ezlynx-gemini-fallback"
description: "When unique-write blocks a field or a Playwright modal on any site cannot be uniquely named, stop, describe the dialog title and visible labels only, ask Gemini for one unique field, then HITL Carlo if Gemini is unsure. Never guess. Never use .first/.nth/.last."
---

# Gemini fallback for a stuck Playwright write on any site

Use this Skill when Playwright raises `PLAYWRIGHT_BLOCKED` or when a popup,
modal, or selector on any site Robie drives (EZLynx, Ascend, carrier portals,
or anything else) cannot be uniquely named. Unique-write is already live.
This Skill does not weaken it.

This is a backend workflow Skill, not Chat small talk.

## When to use

- `PLAYWRIGHT_BLOCKED` on a unique-write (zero matches, many matches, or a
  positional `.first` / `.nth()` / `.last` guess).
- `PLAYWRIGHT_BLOCKED` on a hidden / aria-hidden / not-visible / combobox
  control, or a fill / click / select_option / type `TimeoutError`.
- A dialog, popup, or overlay on any Playwright site whose target field
  cannot be uniquely named from a stable role, label, or test id.
- Any EZLynx note write that is blocked on the discussion picker or note
  body (notes still follow the titled-discussion rule).

## Required sequence

1. **Stop.** Do not fill, click, or submit the ambiguous control.
2. **Describe only what is visible and safe:**
   - dialog or page title (and page host if already known)
   - visible field labels
   - the exact `PLAYWRIGHT_BLOCKED` reason
   - Do not include passwords, MFA codes, cookies, tokens, or secret values.
3. **Ask Gemini for one unique field.** Unique-write now calls
   `ask_gemini_unique_field` on the blocked write itself. Do not only
   stop and report. The helper is fail-closed and site-agnostic.
4. **Apply only if Gemini returns exactly one unique locator** that:
   - names one visible label from the dialog
   - does not use `.first`, `.nth()`, `.last`, or other positional markers
   - still passes unique-write before the write is sent
5. **HITL Carlo** when Gemini is unsure, unreachable, returns more than one
   field, invents a label that is not visible, or suggests a positional
   locator. Never guess a field. There is no second HITL channel.

## Hard stops

- Do not disable unique-write.
- Do not add `.first`, `.nth()`, or `.last` escapes.
- Do not retry the same blocked write with a guessed selector.
- Do not invent a value for a hidden or timed-out control.
- Do not bind and do not take payment.
- Do not invent coverage.
- Do not restart Chrome.

## EZLynx notes

This note rule applies to every EZLynx note write, not only commercial auto.

- Notes belong on an existing titled discussion only (New Business, New
  Policy, Renewal, Cancellation, Submission Center, or another existing
  titled discussion).
- Never write a note on Untitled.
- If the right title is missing, create a properly named discussion first.
- Always include the exact phrase `Robie was here`.

## Result

- `APPLY`: one unique locator, still guarded by unique-write.
- `HITL`: park for Carlo with the page host/title, visible labels, and the
  blocked reason. Do not continue the write.
