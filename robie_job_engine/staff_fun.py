"""Monthly staff fun job (staff.fun.monthly).

Runs on the 1st of each month: generates that month's activity content with
the Gemini API (fresh and seasonally timely), posts it to the general Google
Chat space via incoming webhook (URL from Secret Manager at runtime), and
emails Carlo a gift-card budget/fulfillment reminder since prizes stay manual.

12-month plan (Oct 2026 - Sep 2027). entry_method:
- "chat_thread": staff reply in the chat thread (photo contests, votes, picks).
- "google_form": worker creates a Google Form and links it in the post.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from . import staff_jobs_common as common

ACTION_TYPE = "staff.fun.monthly"
TASK_NAME = "StreetSmart monthly staff fun"
WORKER_NAME = "staff-fun"

SENDER = "robie@streetsmart.insurance"
CARLO = "carlo@streetsmart.insurance"
TIMEZONE = "America/New_York"

# month -> activity config. "scope" rotates whole-staff vs department spotlight.
FUN_PLAN: dict[int, dict[str, str]] = {
    10: {"name": "Halloween costume contest",
         "instructions": "Staff post a photo of their Halloween costume in the chat thread. The photo with the most thread replies/reactions by Oct 30 wins.",
         "entry_method": "chat_thread", "scope": "whole staff"},
    11: {"name": "Gratitude wall + Friendsgiving potluck",
         "instructions": "Everyone posts one thing they are grateful for about a coworker in the thread. Sign up dishes for a Friendsgiving potluck lunch.",
         "entry_method": "chat_thread", "scope": "whole staff"},
    12: {"name": "Holiday trivia + ugly sweater",
         "instructions": "10-question holiday trivia in the thread, plus an ugly-sweater photo vote. Fastest perfect trivia score wins; best sweater by vote wins.",
         "entry_method": "chat_thread", "scope": "whole staff"},
    1: {"name": "New Year goals bingo",
        "instructions": "Bingo card of fun New Year goals (try a new lunch spot, learn one EZLynx shortcut, compliment a coworker). First bingo wins.",
        "entry_method": "chat_thread", "scope": "department spotlight"},
    2: {"name": "Super Bowl squares",
        "instructions": "10x10 Super Bowl squares board. Staff claim squares in the thread; winners per quarter get gift cards.",
        "entry_method": "chat_thread", "scope": "whole staff"},
    3: {"name": "March Madness bracket",
        "instructions": "Staff submit tournament brackets. Best bracket at the end of March Madness wins.",
        "entry_method": "google_form", "scope": "whole staff"},
    4: {"name": "Spring trivia",
        "instructions": "10-question spring-themed trivia (sports, movies, music, fun facts) posted in the thread. Fastest perfect score wins.",
        "entry_method": "chat_thread", "scope": "department spotlight"},
    5: {"name": "Kentucky Derby picks",
        "instructions": "Everyone picks their horse in the thread before post time. Winning horse pick takes the prize; longshot bonus for highest odds.",
        "entry_method": "chat_thread", "scope": "whole staff"},
    6: {"name": "Summer kickoff cookout",
        "instructions": "Vote on cookout menu and date in the thread. Plus a summer bucket-list: post one fun thing you will do this summer.",
        "entry_method": "chat_thread", "scope": "whole staff"},
    7: {"name": "4th of July trivia",
        "instructions": "10-question Americana trivia (history, movies, music, food). Fastest perfect score wins.",
        "entry_method": "chat_thread", "scope": "department spotlight"},
    8: {"name": "Back-to-school supply drive contest",
        "instructions": "Department competition: most school-supply donations for a local drive wins a team lunch. Post donation photos in the thread.",
        "entry_method": "chat_thread", "scope": "department spotlight"},
    9: {"name": "Fantasy football kickoff",
        "instructions": "StreetSmart fantasy football league sign-up. Winner at season end gets the grand gift card; weekly high scorer shout-outs.",
        "entry_method": "chat_thread", "scope": "whole staff"},
}

CONTENT_PROMPT = """You are writing the monthly staff-fun announcement for StreetSmart, an insurance agency.
Write a fun, warm Google Chat post (under 1200 characters) announcing this month's activity.

Today is {today}. The entry/voting deadline is {deadline} — use exactly this date.
Month: {month_name} {year}
Activity: {activity}
How to enter: {instructions}
Entry method: {entry_method_line}
Scope: {scope}

Rules:
- Plain English, upbeat, human. Light humor is welcome.
- Include: what the activity is, how to enter, the deadline ({deadline}), and that the winner gets a gift card.
- End with one line: "Prizes are gift cards — winner announced at month end."
- Do not use @mentions.
"""


def second_friday(year: int, month: int) -> str:
    import datetime as dt
    d = dt.date(year, month, 1)
    days_to_friday = (4 - d.weekday()) % 7
    first = d + dt.timedelta(days=days_to_friday)
    second = first + dt.timedelta(days=7)
    return second.strftime("%A, %B %-d")


def entry_method_line(method: str) -> str:
    if method == "google_form":
        return "a Google Form link included below"
    return "reply in this chat thread"


def generate_post(month: int, year: int) -> tuple[str, dict[str, str]]:
    import datetime as dt
    plan = FUN_PLAN[month]
    month_name = calendar.month_name[month]
    today = dt.date(year, month, 1).strftime("%A, %B %-d, %Y")
    text = common.gemini_generate(CONTENT_PROMPT.format(
        today=today, deadline=second_friday(year, month),
        month_name=month_name, year=year,
        activity=plan["name"], instructions=plan["instructions"],
        entry_method_line=entry_method_line(plan["entry_method"]),
        scope=plan["scope"],
    ))
    return text, plan


def create_entry_form(activity_name: str, month_name: str) -> str:
    """Create a Google Form for entries/votes. Returns the form's responder URL."""
    try:
        from googleapiclient.discovery import build
        forms = build("forms", "v1", credentials=common.google_credentials(),
                      cache_discovery=False)
        form = forms.forms().create(body={"info": {"title": f"StreetSmart staff fun — {activity_name} ({month_name})"}}).execute()
        form_id = form["formId"]
        forms.forms().batchUpdate(formId=form_id, body={"requests": [
            {"createItem": {"item": {"title": "Your name", "questionItem": {
                "question": {"required": True, "textQuestion": {}}}}}},
            {"createItem": {"item": {"title": "Your entry / pick", "questionItem": {
                "question": {"required": True, "textQuestion": {"paragraph": True}}}}}},
        ], "includeFormInResponse": True}).execute()
        return f"https://docs.google.com/forms/d/{form_id}/viewform"
    except Exception as exc:
        raise RuntimeError(f"Google Form creation failed: {type(exc).__name__}: {exc}") from exc


class StaffFunWorker:
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        try:
            return self._perform(job)
        except Exception as exc:
            return WorkerResult(
                False, ACTION_TYPE, {"task": TASK_NAME},
                retryable=True,
                error=f"Staff fun failed: {type(exc).__name__}: {exc}",
            )

    def _perform(self, job: dict[str, Any]) -> WorkerResult:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        month, year = now.month, now.year
        month_name = calendar.month_name[month]

        post_text, plan = generate_post(month, year)
        form_url: str | None = None
        if plan["entry_method"] == "google_form":
            try:
                form_url = create_entry_form(plan["name"], month_name)
                post_text += f"\n\nEnter here: {form_url}"
            except Exception as exc:
                post_text += "\n\n(The entry form could not be created — reply in this thread to enter.)"
                form_url = f"FAILED: {exc}"

        chat_ok = common.post_chat_webhook(post_text)

        # Gift-card fulfillment stays manual: remind Carlo.
        reminder_id = common.send_gmail(
            sender=SENDER, to=[CARLO],
            subject=f"Staff fun — {month_name}: gift-card budget / fulfillment",
            body=(
                f"Hi Carlo,\n\nThis month's staff fun is live: {plan['name']} ({plan['scope']}).\n\n"
                "The announcement just posted to the general chat. "
                "Gift cards are still manual — please confirm the prize budget and fulfill the winner at month end.\n\n"
                "— Robie (StreetSmart automation)"
            ),
        )

        return WorkerResult(
            True, ACTION_TYPE,
            {"chat_posted": chat_ok, "carlo_reminder_id": reminder_id,
             "form_url": form_url or ""},
            detail={
                "month": f"{year}-{month:02d}", "activity": plan["name"],
                "scope": plan["scope"], "post_chars": len(post_text),
            },
            retryable=True,
        )


class StaffFunVerifier:
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = action.get("destination") or {}
        chat_posted = bool(destination.get("chat_posted"))
        reminder_id = str(destination.get("carlo_reminder_id") or "")
        expected = {"chat_posted": True, "carlo_reminder_id": reminder_id}
        captured = datetime.now(tz=ZoneInfo("UTC")).isoformat()

        email_ok = bool(reminder_id) and common.gmail_message_exists(reminder_id, sender=SENDER)
        observed = {"chat_webhook_2xx": chat_posted, "carlo_email_from_robie": email_ok}
        verified = bool(chat_posted and email_ok)
        return VerificationResult(
            verified,
            VerificationEvidence(
                method="webhook_status_and_gmail_readback",
                source="chat webhook HTTP status + gmail.users.messages.get",
                expected=expected, observed=observed,
                authoritative=True, captured_at=captured,
                locator=f"https://mail.google.com/mail/#all/{reminder_id}" if reminder_id else None,
            ),
            retryable=not verified,
            error=None if verified else f"Verification failed: {observed}",
            hold_status=None if verified else JobStatus.FAILED,
        )
