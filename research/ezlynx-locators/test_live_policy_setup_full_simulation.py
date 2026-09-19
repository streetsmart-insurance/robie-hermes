import asyncio

from robie_job_engine.ezlynx_api_only_writes import refuse_playwright_note_or_doc
from robie_job_engine.ezlynx_write_scope import require_allowed_ezlynx_write_applicant

APPLICANT_ID = "220250093"


async def add_discussion_note() -> None:
    """Playwright must never file EZLynx notes (Carlo 2026-09-19)."""
    require_allowed_ezlynx_write_applicant(APPLICANT_ID)
    refuse_playwright_note_or_doc(
        "research/ezlynx-locators/test_live_policy_setup_full_simulation.add_discussion_note"
    )


if __name__ == "__main__":
    asyncio.run(add_discussion_note())
