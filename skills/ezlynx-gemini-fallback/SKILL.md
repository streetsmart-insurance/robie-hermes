---
name: "ezlynx-gemini-fallback"
description: "When unique-write blocks a field or an EZLynx modal cannot be uniquely named, stop, describe the dialog title and visible labels only, ask Gemini for one unique field, then HITL Carlo if Gemini is unsure. Never guess. Never use .first/.nth/.last."
---

# EZLynx Gemini fallback for blocked unique writes

Use this Skill when Playwright raises `PLAYWRIGHT_BLOCKED` or when an EZLynx
popup, modal, or selector cannot be uniquely named. Unique-write is already
live. This Skill does not weaken it.

This is a backend workflow Skill, not Chat small talk.

## When to use

- `PLAYWRIGHT_BLOCKED` on a write (zero matches, many matches, or a
  positional `.first` / `.nth()` / `.last` guess).
- An EZLynx dialog, popup, or overlay whose target field cannot be uniquely
  named from a stable role, label, or test id.
- Any EZLynx note write that is blocked on the discussion picker or note
  body (notes still follow the titled-discussion rule).

## Required sequence

1. **Stop.** Do not fill, click, or submit the ambiguous control.
2. **Describe only what is visible and safe:**
   - dialog or page title
   - visible field labels
   - the exact `PLAYWRIGHT_BLOCKED` reason
   - Do not include passwords, MFA codes, cookies, tokens, or secret values.
3. **Ask Gemini for one unique field** through the job-engine Gemini field
   helper (`ask_gemini_unique_field`). The helper is fail-closed.
4. **Apply only if Gemini returns exactly one unique locator** that:
   - names one visible label from the dialog
   - does not use `.first`, `.nth()`, `.last`, or other positional markers
   - still passes unique-write before the write is sent
5. **HITL Carlo** when Gemini is unsure, unreachable, returns more than one
   field, invents a label that is not visible, or suggests a positional
   locator. Never guess a field.

## Hard stops

- Do not disable unique-write.
- Do not add `.first`, `.nth()`, or `.last` escapes.
- Do not retry the same blocked write with a guessed selector.
- Do not bind and do not take payment.
- Do not invent coverage.

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
- `HITL`: park for Carlo with the dialog title, visible labels, and the
  blocked reason. Do not continue the write.
