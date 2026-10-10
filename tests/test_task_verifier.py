"""Tests for robie_job_engine/task_verifier.py.

Covers: pending queue dedupe, 40-minute due logic, report matching
(verified / missing / unverified), and the never-silently-pass rule.

No live credentials, no network, no box access — everything is local.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from robie_job_engine import task_verifier as tv
from robie_job_engine.task_verifier import (
    TaskVerificationStore,
    format_missing_alert,
    ingest_phone_watchdog,
    load_fallback_confirmations,
    match_task,
    parse_task_report_csv,
    verify_due_tasks,
    PendingTask,
)


# Never read the host's real phone-watchdog confirmation file in unit tests.
os.environ.setdefault("ROBIE_FALLBACK_CONFIRM_STATE", "/nonexistent/zapier_fallback_confirm.json")


def _db():
    d = tempfile.mkdtemp()
    return TaskVerificationStore(os.path.join(d, "pending.db"))


def _iso(dt):
    return dt.isoformat()


class PendingQueueTest(unittest.TestCase):
    def test_record_and_dedupe(self):
        s = _db()
        i1 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales")
        i2 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=None)
        # Different fired_at (now) → different row is fine; same explicit
        # fired_at → deduped to the same id.
        fired = _iso(datetime.now(timezone.utc))
        i3 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=fired)
        i4 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=fired)
        self.assertEqual(i3, i4)
        self.assertGreater(i1, 0)

    def test_due_only_after_40_minutes(self):
        s = _db()
        now = datetime.now(timezone.utc)
        s.record_pending(producer="phone-watchdog", applicant_id="1",
                         title="t", assignee="a",
                         fired_at=_iso(now - timedelta(minutes=10)))
        s.record_pending(producer="phone-watchdog", applicant_id="2",
                         title="t", assignee="a",
                         fired_at=_iso(now - timedelta(minutes=41)))
        due = s.due_for_verification(now)
        self.assertEqual([t.applicant_id for t in due], ["2"])

    def test_resolve(self):
        s = _db()
        i = s.record_pending(producer="certificates", applicant_id="9",
                            title="t", assignee="a",
                            fired_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)))
        s.resolve(i, "VERIFIED", "matched")
        self.assertEqual(s.counts(), {"VERIFIED": 1})
        self.assertEqual(s.due_for_verification(), [])


class ReportMatchTest(unittest.TestCase):
    def _pending(self, **kw):
        base = dict(id=1, producer="certificates", applicant_id="177412857",
                    title="Certificate request — 139 Trucking",
                    assignee="SCanales",
                    fired_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=50)),
                    status="PENDING")
        base.update(kw)
        return PendingTask(**base)

    def test_match_on_applicant_assignee_title(self):
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,177412857,"
            + _iso(datetime.now(timezone.utc) - timedelta(minutes=30)).encode() + b"\n"
        )
        hit = match_task(self._pending(), rows)
        self.assertIsNotNone(hit)

    def test_no_match_wrong_applicant(self):
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,999999999,"
            + _iso(datetime.now(timezone.utc) - timedelta(minutes=30)).encode() + b"\n"
        )
        self.assertIsNone(match_task(self._pending(), rows))

    def test_no_match_created_before_firing(self):
        # A task row that predates the firing cannot be our task.
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,177412857,"
            + _iso(datetime.now(timezone.utc) - timedelta(hours=2)).encode() + b"\n"
        )
        self.assertIsNone(match_task(self._pending(), rows))

    def test_match_real_activity_detail_columns(self):
        # Regression test (2026-09-27): the production "ROBIE task report CSV"
        # uses Activity Detail column names. A naive substring lookup for
        # "task" hit "Task Assigned To" (returning the assignee as the title)
        # and "created" hit "Note Created by" (returning a name as the
        # timestamp). Exact-match-first must resolve the right columns.
        now = datetime.now(timezone.utc)
        created = _iso(now - timedelta(minutes=30))
        csv = (
            b"Applicant ID,Account Name,Task Assigned To,Activity Type,"
            b"Note Created by,Task Status,Note,Created Date,Task Created Date,"
            b"Task ID\n"
            b"177412857,139 TRUCKING LLC,Steffany Canales,Task Creation Note,"
            b"Carlo Ferrara,Open,Certificate request - 139 Trucking,"
            + created.encode() + b","
            + now.strftime("%Y-%m-%d").encode() + b","
            b"63227816\n"
        )
        rows = parse_task_report_csv(csv)
        hit = match_task(self._pending(), rows)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["task id"], "63227816")


class VerifyDueTest(unittest.TestCase):
    def test_verified_missing_unverified(self):
        s = _db()
        now = datetime.now(timezone.utc)
        old = _iso(now - timedelta(minutes=50))
        s.record_pending(producer="certificates", applicant_id="111",
                         title="Cert A", assignee="SCanales", fired_at=old)
        s.record_pending(producer="phone-watchdog", applicant_id="222",
                         title="Callback B", assignee="Ricardo Aguilar", fired_at=old)

        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Cert A,Steffany Canales,111,"
            + _iso(now - timedelta(minutes=30)).encode() + b"\n"
        )
        out = verify_due_tasks(s, rows, True)
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(len(out["missing"]), 1)
        self.assertEqual(out["verified"][0].applicant_id, "111")
        self.assertEqual(out["missing"][0].applicant_id, "222")

    def test_no_report_means_unverified_not_missing(self):
        # Never silently pass, never a false MISSING.
        s = _db()
        old = _iso(datetime.now(timezone.utc) - timedelta(minutes=50))
        s.record_pending(producer="certificates", applicant_id="111",
                         title="Cert A", assignee="SCanales", fired_at=old)
        out = verify_due_tasks(s, None, False)
        self.assertEqual(len(out["unverified"]), 1)
        self.assertEqual(len(out["missing"]), 0)
        self.assertEqual(len(out["verified"]), 0)

    def test_nothing_due_is_quiet(self):
        s = _db()
        out = verify_due_tasks(s, [], True)
        self.assertEqual(out, {"verified": [], "missing": [], "unverified": []})


def _naive_ny(dt_utc: datetime) -> str:
    """Box-local wall clock, the form phone_alerts.processed_at is stored in."""
    local = dt_utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return local.isoformat(timespec="seconds")


def _phone_db(rows: list[tuple]) -> str:
    path = os.path.join(tempfile.mkdtemp(), "phone_alerts.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE processed_calls (
            message_id TEXT,
            ams_account_id TEXT,
            account_name TEXT,
            assigned_user TEXT,
            processed_at TEXT,
            ezlynx_task_status TEXT
        )"""
    )
    conn.executemany(
        """INSERT INTO processed_calls
           (message_id, ams_account_id, account_name, assigned_user,
            processed_at, ezlynx_task_status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return path


class PhoneWatchdogIngestTest(unittest.TestCase):
    def test_ingests_delivered_and_sent_to_relay_once(self):
        """Both handoff statuses are queued; other statuses and repeats are not.

        processed_at is naive America/New_York. A row 3 hours old is inside
        the 6-hour window, but its wall-clock string sorts before a UTC
        cutoff of (now - 6h). The tz-aware filter must still ingest it.
        A row 6.5 hours old is inside the coarse SQL buffer and must be
        dropped by that same filter.
        """
        now = datetime.now(timezone.utc)
        recent = _naive_ny(now - timedelta(hours=3))
        just_outside = _naive_ny(now - timedelta(hours=6, minutes=30))
        stale = _naive_ny(now - timedelta(hours=30))
        db = _phone_db([
            ("m-del", "111", "Jayshri Dixit", "Ricardo Aguilar", recent, "delivered"),
            ("m-relay", "222", "Ada Lovelace", "SCanales", recent, "sent_to_relay"),
            ("m-fail", "333", "Skip Fail", "A", recent, "failed"),
            ("m-pend", "444", "Skip Pending", "A", recent, "pending"),
            ("m-err", "555", "Skip Error", "A", recent, "error"),
            ("m-blank", "666", "Skip Blank", "A", recent, ""),
            ("m-sent", "999", "Skip Sent", "A", recent, "sent"),
            ("m-old-del", "777", "Old Delivered", "A", just_outside, "delivered"),
            ("m-stale-relay", "888", "Stale Relay", "A", stale, "sent_to_relay"),
        ])
        store = _db()
        first = ingest_phone_watchdog(store, db, since_hours=6)
        self.assertEqual(first, 2)

        queued = {t.applicant_id: t for t in store.due_for_verification(now)}
        self.assertEqual(set(queued), {"111", "222"})
        self.assertEqual(queued["111"].status, "PENDING")
        self.assertEqual(queued["111"].producer, "phone-watchdog")
        self.assertEqual(queued["111"].title, "[AFTER-HOURS CALLBACK] Jayshri Dixit")
        self.assertEqual(queued["111"].assignee, "Ricardo Aguilar")
        self.assertEqual(queued["222"].status, "PENDING")
        self.assertEqual(queued["222"].title, "[AFTER-HOURS CALLBACK] Ada Lovelace")
        self.assertEqual(store.counts(), {"PENDING": 2})

        # Same rows again: the unique key (producer, applicant, title, fired_at)
        # keeps the queue at one task per handoff.
        second = ingest_phone_watchdog(store, db, since_hours=6)
        self.assertEqual(second, 0)
        self.assertEqual(store.counts(), {"PENDING": 2})
        self.assertEqual(len(store.due_for_verification(now)), 2)

    def test_sent_to_relay_is_not_ezlynx_delivery_proof(self):
        """sent_to_relay only queues the task. The report still has to match."""
        now = datetime.now(timezone.utc)
        recent = _naive_ny(now - timedelta(hours=3))
        db = _phone_db([
            ("m-relay", "222", "Ada Lovelace", "SCanales", recent, "sent_to_relay"),
        ])
        store = _db()
        self.assertEqual(ingest_phone_watchdog(store, db, since_hours=6), 1)

        unmatched = verify_due_tasks(store, [], True)
        self.assertEqual(unmatched["verified"], [])
        self.assertEqual(len(unmatched["missing"]), 1)
        self.assertEqual(unmatched["missing"][0].applicant_id, "222")
        self.assertEqual(store.counts(), {"MISSING": 1})

        matched_store = _db()
        self.assertEqual(ingest_phone_watchdog(matched_store, db, since_hours=6), 1)
        created = _iso(now - timedelta(hours=2, minutes=30))
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"[AFTER-HOURS CALLBACK] Ada Lovelace,Steffany Canales,222,"
            + created.encode() + b"\n"
        )
        matched = verify_due_tasks(matched_store, rows, True)
        self.assertEqual(len(matched["verified"]), 1)
        self.assertEqual(matched["missing"], [])
        self.assertEqual(matched_store.counts(), {"VERIFIED": 1})


class AlertFormatTest(unittest.TestCase):
    def test_missing_alert_names_task(self):
        t = PendingTask(id=1, producer="phone-watchdog", applicant_id="177412857",
                        title="[AFTER-HOURS CALLBACK] Test", assignee="Ricardo Aguilar",
                        fired_at="2026-09-27T11:00:00+00:00", status="MISSING")
        text = format_missing_alert([t])
        self.assertIn("177412857", text)
        self.assertIn("Ricardo Aguilar", text)
        self.assertIn("not found", text)


# --- 2026-10-10 false alarm: the report is Central, not Eastern -------------
# Real values from Prod: pending.db ids 1192 (Lori Radice), 1182 (Tammy
# Hughes), 1056 (KCG Logistics) and the "ROBIE task report CSV" rows for them.

REPORT_HEADER = (
    b"Applicant ID,Account Name,Task Assigned To,Activity Type,Note Created by,"
    b"Task Status,Note,Created Date,Task Created Date,Task ID\n"
)


def _report(*rows: tuple[str, str, str, str, str, str]) -> list[dict[str, str]]:
    """rows: (applicant, account, assignee, status, created_date, task_id)."""
    body = b""
    for applicant, account, assignee, status, created, task_id in rows:
        note = (
            f'"INBOUND PHONE CALL DETAILS:\n- Caller Name: {account}\n'
            f'- Account: {account} (AMS ID: {applicant})\n- Assigned Staff: {assignee}"'
        )
        body += (
            f"{applicant},{account},{assignee},Task Creation Note,Carlo Ferrara,"
            f"{status},{note},{created},{created[:10]},{task_id}\n"
        ).encode()
    return parse_task_report_csv(REPORT_HEADER + body)


def _task(applicant, title, assignee, fired_utc, producer="phone-watchdog", id=1):
    return PendingTask(id=id, producer=producer, applicant_id=applicant, title=title,
                       assignee=assignee, fired_at=fired_utc, status="PENDING")


LORI = _task("21586658", "[AFTER-HOURS CALLBACK] Lori Radice", "Daniela Aguilar",
             "2026-10-10T16:18:59.116536+00:00", id=1192)   # 12:18:59 ET
TAMMY = _task("224280745", "[AFTER-HOURS CALLBACK] TAMMY HUGHES", "Carlo Ferrara",
              "2026-10-10T14:02:39.162671+00:00", id=1182)  # 10:02:39 ET
KCG = _task("89678810", "[AFTER-HOURS CALLBACK] KCG Logistics Llc", "Jake Ferrara",
            "2026-10-09T18:53:36.187187+00:00", id=1056)    # 14:53:36 ET
LORI_ROW = ("21586658", "Lori Radice", "Daniela Aguilar", "Open",
            "2026-10-10T11:19:08.090000", "63635431")
TAMMY_ROW = ("224280745", "TAMMY HUGHES", "Carlo Ferrara", "Open",
             "2026-10-10T09:03:19.340000", "63634247")
KCG_ROW = ("89678810", "KCG Logistics Llc", "Jose Cabrera", "Closed",
           "2026-10-09T13:53:40.833000", "63618224")


class ReportTimeZoneTest(unittest.TestCase):
    def test_default_report_zone_is_central(self):
        self.assertEqual(str(tv.REPORT_TZ), "America/Chicago")

    def test_lori_and_tammy_match_with_central_report_times(self):
        rows = _report(LORI_ROW, TAMMY_ROW)
        self.assertEqual(match_task(LORI, rows)["task id"], "63635431")
        self.assertEqual(match_task(TAMMY, rows)["task id"], "63634247")

    def test_reading_the_report_as_eastern_was_the_bug(self):
        # Created Date 11:19:08 read as Eastern is 15:19:08Z, an hour BEFORE
        # the 16:18:59Z firing, so every row was thrown out as "predating" it.
        rows = _report(LORI_ROW, TAMMY_ROW)
        with mock.patch.object(tv, "REPORT_TZ", ZoneInfo("America/New_York")):
            self.assertIsNone(match_task(LORI, rows))
            self.assertIsNone(match_task(TAMMY, rows))

    def test_report_zone_can_be_set_in_the_environment(self):
        self.assertEqual(str(tv._load_report_tz("America/New_York")), "America/New_York")
        self.assertEqual(str(tv._load_report_tz("")), "America/Chicago")
        self.assertEqual(str(tv._load_report_tz("Not/AZone")), "America/Chicago")

    def test_timestamps_with_an_offset_are_not_shifted(self):
        row = ("21586658", "Lori Radice", "Daniela Aguilar", "Open",
               "2026-10-10T16:19:08.09+00:00", "63635431")
        self.assertIsNotNone(match_task(LORI, _report(row)))

    def test_task_created_before_the_firing_is_still_rejected(self):
        early = ("21586658", "Lori Radice", "Daniela Aguilar", "Open",
                 "2026-10-10T10:00:00", "1")   # 11:00 ET, 1h18m before the firing
        self.assertIsNone(match_task(LORI, _report(early)))

    def test_report_row_a_minute_before_fired_at_still_matches(self):
        # Metro Trans 2026-10-07: created 56 s before the watchdog logged it.
        t = _task("1", "[AFTER-HOURS CALLBACK] Metro Trans LLC", "Mike Sosa",
                  "2026-10-07T17:41:38+00:00")
        row = ("1", "Metro Trans LLC", "Mike Sosa", "Closed", "2026-10-07T12:40:42", "9")
        self.assertIsNotNone(match_task(t, _report(row)))
        row5 = ("1", "Metro Trans LLC", "Mike Sosa", "Closed", "2026-10-07T12:36:00", "9")
        self.assertIsNone(match_task(t, _report(row5)))


class ReassignedTaskTest(unittest.TestCase):
    def test_kcg_reassigned_to_jose_cabrera_still_matches(self):
        hit = match_task(KCG, _report(KCG_ROW))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["task id"], "63618224")
        self.assertEqual(hit["_assignee_differs"], "1")

    def test_same_assignee_is_preferred_over_a_reassigned_row(self):
        mine = ("89678810", "KCG Logistics Llc", "Jake Ferrara", "Open",
                "2026-10-09T13:53:41.000000", "2")
        hit = match_task(KCG, _report(KCG_ROW, mine))
        self.assertEqual(hit["task id"], "2")
        self.assertNotIn("_assignee_differs", hit)

    def test_a_different_assignee_needs_the_title_and_the_window(self):
        other_title = ("89678810", "Someone Else", "Jose Cabrera", "Closed",
                       "2026-10-09T13:53:40.833000", "3")
        self.assertIsNone(match_task(KCG, _report(other_title)))
        other_applicant = ("1", "KCG Logistics Llc", "Jose Cabrera", "Closed",
                           "2026-10-09T13:53:40.833000", "4")
        self.assertIsNone(match_task(KCG, _report(other_applicant)))
        too_late = ("89678810", "KCG Logistics Llc", "Jose Cabrera", "Closed",
                    "2026-10-09T15:30:00", "5")
        self.assertIsNone(match_task(KCG, _report(too_late)))

    def test_verify_records_the_reassignment(self):
        s = _db()
        now = datetime.now(timezone.utc)
        fired = _iso(now - timedelta(minutes=50))
        s.record_pending(producer="phone-watchdog", applicant_id="89678810",
                         title="[AFTER-HOURS CALLBACK] KCG Logistics Llc",
                         assignee="Jake Ferrara", fired_at=fired)
        created = (now - timedelta(minutes=49)).astimezone(ZoneInfo("America/Chicago"))
        row = ("89678810", "KCG Logistics Llc", "Jose Cabrera", "Closed",
               created.replace(tzinfo=None).isoformat(), "63618224")
        out = verify_due_tasks(s, _report(row), True)
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(out["missing"], [])
        with s._connect() as conn:
            detail = conn.execute("SELECT detail FROM pending_tasks").fetchone()[0]
        self.assertIn("reassigned", detail)
        self.assertIn("Jose Cabrera", detail)


class WatchdogConfirmedTest(unittest.TestCase):
    def _store_with(self, task: PendingTask) -> TaskVerificationStore:
        s = _db()
        s.record_pending(producer=task.producer, applicant_id=task.applicant_id,
                         title=task.title, assignee=task.assignee,
                         fired_at=task.fired_at)
        return s

    def _record(self, task, **kw):
        sent = datetime.fromisoformat(task.fired_at).timestamp() - 1
        rec = {"applicant_id": task.applicant_id, "title": "[CALLBACK REQUIRED] "
               + task.title.split("] ", 1)[1], "state": "confirmed",
               "sent_at": sent, "where": "discussion 851596587 note 1137261034"}
        rec.update(kw)
        return rec

    def test_confirmed_by_the_watchdog_is_verified_even_without_a_report(self):
        s = self._store_with(LORI)
        out = verify_due_tasks(s, None, False, confirmations=[self._record(LORI)])
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(out["unverified"], [])
        with s._connect() as conn:
            row = conn.execute("SELECT status, detail FROM pending_tasks").fetchone()
        self.assertEqual(row["status"], "VERIFIED")
        self.assertIn("phone-watchdog", row["detail"])

    def test_title_prefix_differences_do_not_matter(self):
        # the watchdog recorded "[CALLBACK REQUIRED] ..."; we queued "[AFTER-HOURS ...]"
        s = self._store_with(LORI)
        out = verify_due_tasks(s, [], True, confirmations=[self._record(LORI)])
        self.assertEqual(len(out["verified"]), 1)

    def test_other_applicant_time_or_title_does_not_count(self):
        for rec in (self._record(LORI, applicant_id="1"),
                    self._record(LORI, sent_at=datetime.fromisoformat(LORI.fired_at).timestamp() - 3600),
                    self._record(LORI, title="[CALLBACK REQUIRED] Somebody Else")):
            self.assertIsNone(tv.watchdog_confirmation(LORI, [rec]))

    def test_certificates_tasks_are_never_confirmed_this_way(self):
        cert = _task("21586658", "[AFTER-HOURS CALLBACK] Lori Radice", "x",
                     LORI.fired_at, producer="certificates")
        self.assertIsNone(tv.watchdog_confirmation(cert, [self._record(cert)]))

    def test_load_reads_only_confirmed_records(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "zapier_fallback_confirm.json")
        with open(path, "w") as fh:
            json.dump({"pending": [{"applicant_id": "1", "state": "confirmed"},
                                   {"applicant_id": "2", "state": "alerted"},
                                   {"applicant_id": "3", "state": "pending"}]}, fh)
        got = load_fallback_confirmations([tv.Path(path), tv.Path(d) / "missing.json"])
        self.assertEqual([r["applicant_id"] for r in got], ["1"])
        self.assertEqual(load_fallback_confirmations([tv.Path(d) / "nope.json"]), [])


class ReportCoverageTest(unittest.TestCase):
    """The report's newest row trails the email by 1-3 h (and ~6 h overnight)."""

    def _queued(self, minutes_ago=50):
        s = _db()
        now = datetime.now(timezone.utc)
        s.record_pending(producer="phone-watchdog", applicant_id="21586658",
                         title="[AFTER-HOURS CALLBACK] Lori Radice",
                         assignee="Daniela Aguilar",
                         fired_at=_iso(now - timedelta(minutes=minutes_ago)))
        return s, now

    def _other_row(self, when_utc: datetime):
        ct = when_utc.astimezone(ZoneInfo("America/Chicago")).replace(tzinfo=None)
        return _report(("1", "Other Client", "Someone", "Open", ct.isoformat(), "7"))

    def test_report_that_stops_before_the_firing_does_not_make_it_missing(self):
        s, now = self._queued()
        stale = self._other_row(now - timedelta(minutes=120))   # newest row 70 min before firing
        out = verify_due_tasks(s, stale, True, now=now)
        self.assertEqual(out, {"verified": [], "missing": [], "unverified": []})
        self.assertEqual(s.counts(), {"PENDING": 1})

    def test_report_that_covers_the_firing_without_the_task_is_missing(self):
        s, now = self._queued()
        fresh = self._other_row(now - timedelta(minutes=20))
        out = verify_due_tasks(s, fresh, True, now=now)
        self.assertEqual(len(out["missing"]), 1)
        self.assertEqual(s.counts(), {"MISSING": 1})

    def test_coverage_margin_is_five_minutes(self):
        s, now = self._queued()
        # newest row 3 minutes after the firing: not enough margin yet
        edge = self._other_row(now - timedelta(minutes=47))
        self.assertEqual(verify_due_tasks(s, edge, True, now=now)["missing"], [])
        enough = self._other_row(now - timedelta(minutes=44))
        self.assertEqual(len(verify_due_tasks(s, enough, True, now=now)["missing"]), 1)

    def test_waiting_for_a_covering_report_ends_in_unverified_not_missing(self):
        s, now = self._queued(minutes_ago=13 * 60)
        stale = self._other_row(now - timedelta(hours=14))
        out = verify_due_tasks(s, stale, True, now=now)
        self.assertEqual(len(out["unverified"]), 1)
        self.assertEqual(out["missing"], [])
        self.assertEqual(s.counts(), {"UNVERIFIED": 1})

    def test_lori_is_found_once_the_report_includes_her_row(self):
        s = _db()
        s.record_pending(producer="phone-watchdog", applicant_id="21586658",
                         title="[AFTER-HOURS CALLBACK] Lori Radice",
                         assignee="Daniela Aguilar", fired_at="2026-10-10T16:18:59.116536+00:00")
        old = _report(TAMMY_ROW)   # the 13:00 ET report: newest row 10:08 ET, no Lori
        self.assertEqual(verify_due_tasks(s, old, True, now=datetime(2026, 10, 10, 17, 15, tzinfo=timezone.utc))["missing"], [])
        self.assertEqual(s.counts(), {"PENDING": 1})
        new = _report(TAMMY_ROW, LORI_ROW)   # the 14:00 ET report
        out = verify_due_tasks(s, new, True, now=datetime(2026, 10, 10, 18, 15, tzinfo=timezone.utc))
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(s.counts(), {"VERIFIED": 1})


if __name__ == "__main__":
    unittest.main()
