# zapier-bland-webhook (Cloud Run)

Source of the Cloud Run service `zapier-bland-webhook` in project
`streetsmart-hermes-poc`. The first commit adding this directory is the
source of the latest deployed revision, unchanged (copied from the Cloud
Run GCS source zip on Oct 2–3, 2026). Later commits are changes.

Safety state as of Oct 7, 2026: revision `00013-rsg` runs with `DRY_RUN=1`
and `ROBIE_VOICE_AUTODIAL_LIVE=0`. Any new revision must keep both.

Hard rule (Carlo, Oct 7, 2026): a **Robie Call** dials only a phone number
typed into the note text. It never dials the number on file, and never a
placeholder. With no typed number (or more than one), it dials nothing
and asks for the number. See `dispatcher.typed_phones` and
`tests/test_robie_call_typed_only.py`.
