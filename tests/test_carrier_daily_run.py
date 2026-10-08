"""Daily carrier pull on Test: Drive upload, scheduler run, memo key, BOP refetch (2026-10-08)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from robie_job_engine import carrier_daily_run as daily
from robie_job_engine import carrier_qa_drive as qa

PDF = b"%PDF-1.4 notice"


class FakeDrive:
    """Just enough of the Drive v3 client for CarrierDriveUpload."""

    def __init__(self, *, folder_meta=None):
        self.files_by_parent: dict[str, list[dict]] = {}
        self.created: list[dict] = []
        self.folder_meta = folder_meta
        self._next = 0

    def files(self):
        return self

    def _exec(self, value):
        return SimpleNamespace(execute=lambda: value)

    def get(self, fileId, fields, supportsAllDrives):
        meta = self.folder_meta or {
            "id": fileId, "name": next(t for t, i in qa.CARRIER_QA_FOLDERS.values() if i == fileId),
            "mimeType": qa.FOLDER_MIME, "trashed": False, "parents": [qa.CARRIER_QA_DRIVE_ROOT_ID],
            "capabilities": {"canAddChildren": True},
        }
        return self._exec(meta)

    def list(self, q, fields, pageSize, supportsAllDrives, includeItemsFromAllDrives):
        parent = q.split("'")[1]
        name = q.split("name = '")[1].split("'")[0]
        files = [f for f in self.files_by_parent.get(parent, []) if f["name"] == name]
        if "mimeType =" in q:
            files = [f for f in files if f.get("mimeType") == qa.FOLDER_MIME]
        return self._exec({"files": files})

    def create(self, body, fields, supportsAllDrives, media_body=None):
        self._next += 1
        item = {"id": f"id{self._next}", "name": body["name"], "mimeType": body.get("mimeType")}
        if media_body is not None:
            content = media_body.content
            item["md5Checksum"] = hashlib.md5(content).hexdigest()
        self.files_by_parent.setdefault(body["parents"][0], []).append(item)
        self.created.append(item)
        return self._exec(item)


class DriveUploadTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(qa, "_media_upload", side_effect=lambda content: SimpleNamespace(content=content))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "progressive"
        self.pack = self.root / "2026-10-08"
        (self.pack / "2026-10-06").mkdir(parents=True)
        (self.pack / "2026-10-06" / "875934744 Cancel_Notice Progressive.pdf").write_bytes(PDF + b"a")
        (self.pack / "sources").mkdir()
        (self.pack / "sources" / "x.pdf").write_bytes(PDF)
        (self.pack / "fao-cancellation-ledger.json").write_text("{}")
        os.environ["ROBIE_ENV"] = "TEST"

    def tearDown(self):
        self.tmp.cleanup()

    def test_uploads_into_carrier_date_folder_and_records_ledger(self):
        drive = FakeDrive()
        result = qa.CarrierDriveUpload(drive, "progressive", self.root).upload_pack(self.pack, "2026-10-08")
        self.assertEqual([u["name"] for u in result.uploaded], ["875934744 Cancel_Notice Progressive.pdf"])
        folder = drive.created[0]
        self.assertEqual((folder["name"], folder["mimeType"]), ("2026-10-08", qa.FOLDER_MIME))
        self.assertIn(folder, drive.files_by_parent[qa.CARRIER_QA_FOLDERS["progressive"][1]])
        ledger = json.loads((self.root / qa.DRIVE_LEDGER_NAME).read_text())
        self.assertIn("2026-10-06/875934744 Cancel_Notice Progressive.pdf", ledger["items"])
        self.assertEqual(oct(os.stat(self.root / qa.DRIVE_LEDGER_NAME).st_mode & 0o777), "0o600")

    def test_second_run_and_next_day_repull_are_deduped(self):
        drive = FakeDrive()
        up = qa.CarrierDriveUpload(drive, "progressive", self.root)
        up.upload_pack(self.pack, "2026-10-08")
        again = up.upload_pack(self.pack, "2026-10-08")
        self.assertEqual(again.uploaded, [])
        self.assertEqual(len(again.skipped), 1)
        nxt = self.root / "2026-10-09" / "2026-10-06"
        nxt.mkdir(parents=True)
        (nxt / "875934744 Cancel_Notice Progressive.pdf").write_bytes(PDF + b"a")
        later = up.upload_pack(self.root / "2026-10-09", "2026-10-09")
        self.assertEqual(later.uploaded, [])
        self.assertEqual(len([c for c in drive.created if c.get("md5Checksum")]), 1)

    def test_same_bytes_under_a_new_name_are_not_uploaded_twice(self):
        drive = FakeDrive()
        up = qa.CarrierDriveUpload(drive, "progressive", self.root)
        up.upload_pack(self.pack, "2026-10-08")
        (self.pack / "2026-10-06" / "copy.pdf").write_bytes(PDF + b"a")
        result = up.upload_pack(self.pack, "2026-10-08")
        self.assertEqual(result.uploaded, [])
        self.assertIn("same PDF", result.skipped[-1]["reason"])

    def test_name_clash_across_issue_dates_gets_the_date(self):
        names = qa.drive_names(["2026-10-03/A.pdf", "2026-10-04/A.pdf", "2026-10-04/B.pdf"])
        self.assertEqual(names["2026-10-03/A.pdf"], "A (2026-10-03).pdf")
        self.assertEqual(names["2026-10-04/A.pdf"], "A (2026-10-04).pdf")
        self.assertEqual(names["2026-10-04/B.pdf"], "B.pdf")

    def test_different_file_already_in_drive_is_held_not_overwritten(self):
        drive = FakeDrive()
        folder_parent = qa.CARRIER_QA_FOLDERS["progressive"][1]
        drive.files_by_parent[folder_parent] = [{"id": "f", "name": "2026-10-08", "mimeType": qa.FOLDER_MIME}]
        drive.files_by_parent["f"] = [{"id": "x", "name": "875934744 Cancel_Notice Progressive.pdf", "md5Checksum": "zz"}]
        result = qa.CarrierDriveUpload(drive, "progressive", self.root).upload_pack(self.pack, "2026-10-08")
        self.assertEqual(result.uploaded, [])
        self.assertIn("different", result.held[0]["reason"])

    def test_moved_or_retired_folder_holds(self):
        drive = FakeDrive(folder_meta={"name": "Progressive", "mimeType": qa.FOLDER_MIME, "trashed": False,
                                       "parents": [qa.RETIRED_NESTED_ROOT_ID], "capabilities": {"canAddChildren": True}})
        with self.assertRaises(qa.DriveUploadHold):
            qa.CarrierDriveUpload(drive, "progressive", self.root).upload_pack(self.pack, "2026-10-08")
        self.assertEqual(drive.created, [])

    def test_requires_test_and_a_token(self):
        with mock.patch.dict(os.environ, {"ROBIE_ENV": "PROD"}):
            with self.assertRaises(Exception):
                qa.build_drive_service()
        env = {"ROBIE_ENV": "TEST", qa.TOKEN_ENV: "", qa.FALLBACK_TOKEN_ENV: ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaisesRegex(qa.DriveUploadHold, "not configured"):
                qa.build_drive_service()

    def test_refuses_production_host(self):
        with mock.patch.object(qa.socket, "gethostname", return_value="hermes-poc-01"):
            with self.assertRaisesRegex(qa.DriveUploadHold, "Production"):
                qa.CarrierDriveUpload(FakeDrive(), "progressive", self.root).upload_pack(self.pack, "2026-10-08")


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["ROBIE_ENV"] = "TEST"

    def tearDown(self):
        self.tmp.cleanup()

    def _ok(self, name, **_):
        return {"display": name, "status": "OK", "downloaded": 1, "held": []}

    def test_runs_daily_set_skips_guard_and_travelers_and_cleans_tabs_each_time(self):
        calls, tabs = [], []
        summary = daily.run_daily(
            day=date(2026, 10, 9), root=self.root, carriers=daily._carrier_list(None),
            run_one=lambda name, **kw: calls.append(name) or self._ok(name),
            close_tabs=lambda url: tabs.append(url) or [],
        )
        self.assertEqual(calls, list(daily.DAILY_CARRIERS))
        self.assertEqual(summary["carriers"]["guard"]["status"], "SKIPPED")
        self.assertEqual(summary["carriers"]["travelers"]["status"], "SKIPPED")
        self.assertEqual(len(tabs), len(daily.DAILY_CARRIERS) + 1)
        self.assertEqual(os.environ[daily.KILL_SWITCH_ENV], "0")
        text = (self.root / "runs" / "2026-10-09" / "summary.txt").read_text()
        self.assertIn("nothing filed to EZLynx", text)
        self.assertIn("Guard: skipped", text)

    def test_one_carrier_failing_or_hanging_never_stops_the_others(self):
        def runner(cmd, **kw):
            if "progressive" in cmd and "--carriers" in cmd:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            if "geico" in cmd:
                raise RuntimeError("boom")
            out = {"carriers": {cmd[cmd.index("--carriers") + 1]: {"status": "OK", "downloaded": 2, "held": []}}} \
                if "--carriers" in cmd else {"status": "EMPTY", "pdfs": 0, "policies": []}
            return SimpleNamespace(stdout="summary\n" + json.dumps(out, indent=2), stderr="", returncode=0)

        summary = daily.run_daily(
            day=date(2026, 10, 9), root=self.root,
            run_one=lambda name, **kw: daily.run_carrier(name, runner=runner, **kw),
            close_tabs=lambda url: [],
        )
        r = summary["carriers"]
        self.assertEqual(r["progressive"]["status"], "FAILED")
        self.assertIn("timed out", r["progressive"]["error"])
        self.assertEqual(r["geico"]["status"], "FAILED")
        self.assertEqual(r["natgen"]["downloaded"], 2)
        self.assertEqual(r["progressive_bop"]["status"], "OK")
        self.assertEqual(summary["totals"]["failed"], 2)

    def test_drive_unavailable_holds_upload_but_pulls_still_run(self):
        def no_drive():
            raise qa.DriveUploadHold("Drive upload token is not configured on this host")

        summary = daily.run_daily(
            day=date(2026, 10, 9), root=self.root, carriers=("natgen",), upload_drive=True,
            run_one=lambda name, **kw: self._ok(name), close_tabs=lambda url: [], drive_factory=no_drive,
        )
        self.assertEqual(summary["carriers"]["natgen"]["status"], "OK")
        self.assertIn("not configured", summary["carriers"]["natgen"]["drive"]["reason"])

    def test_refuses_outside_test(self):
        with mock.patch.dict(os.environ, {"ROBIE_ENV": "PROD"}):
            with self.assertRaises(Exception):
                daily.run_daily(day=date(2026, 10, 9), root=self.root, run_one=self._ok, close_tabs=lambda u: [])

    def test_only_local_cdp(self):
        with self.assertRaises(Exception):
            daily.close_stale_tabs("http://10.0.0.5:9223", http=lambda *a, **k: [])

    def test_stale_tab_rules_keep_portal_tabs_and_one_page(self):
        pages = [
            {"type": "page", "id": "A1", "url": "https://clpolicy.foragentsonly.com/Express/PDFHandler.ashx?x=1"},
            {"type": "page", "id": "B2", "url": "https://bop.americanstrategic.com/"},
            {"type": "page", "id": "C3", "url": "about:blank"},
            {"type": "page", "id": "D4", "url": "https://fos.finys.com/"},
            {"type": "page", "id": "E5", "url": "https://www.foragentsonly.com/landingpages/managepolicies/"},
            {"type": "service_worker", "id": "F6", "url": "about:blank"},
        ]
        hits = []

        def http(url, **kw):
            if url.endswith("/json/list"):
                return pages
            hits.append(url.rsplit("/", 1)[-1])
            return None

        daily.close_stale_tabs("http://127.0.0.1:9223", http=http)
        self.assertEqual(sorted(hits), ["A1", "B2", "C3"])
        hits.clear()
        pages[:] = [{"type": "page", "id": "Z9", "url": "about:blank"}]
        daily.close_stale_tabs("http://127.0.0.1:9223", http=http)
        self.assertEqual(hits, [])

    def test_bop_held_per_policy_is_listed_not_a_carrier_hold(self):
        payload = {"status": "HELD", "pdfs": 1, "reason": "x", "policies": [
            {"policy_number": "123456", "disposition": "pulled"},
            {"policy_number": "654321", "disposition": "held", "reason": "no notice"}]}
        r = daily.normalize_result("progressive_bop", payload, returncode=2, stderr="")
        self.assertEqual((r["status"], r["downloaded"]), ("OK", 1))
        self.assertEqual(r["held"], ["654321: no notice"])

    def test_notify_without_space_does_not_post(self):
        with mock.patch.dict(os.environ, {"ROBIE_HEALTH_CHAT_SPACE": ""}):
            self.assertIn("not set", daily.notify("hello"))

    def test_systemd_units_stay_on_test_and_off_current(self):
        base = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        service = (base / "robie-carrier-pull-test.service").read_text()
        timer = (base / "robie-carrier-pull-test.timer").read_text()
        self.assertIn("ROBIE_ENV=TEST", service)
        self.assertIn("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0", service)
        self.assertNotIn("/current", service)
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        self.assertNotIn("--notify", exec_line)
        self.assertIn("--upload-drive", exec_line)
        self.assertIn("/var/lib/robie-carrier-pull-test/release", service)
        self.assertIn("Mon..Fri *-*-* 07:30:00 America/New_York", timer)


class ProgressiveMemoKeyTests(unittest.TestCase):
    def test_two_same_name_memos_on_different_dates_get_different_keys(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        cls = fao.FaoDocument
        names = set(cls.__dataclass_fields__)
        base = {n: "" for n in names}
        base.update(policy_number="879176249", document_name="Underwriting Memo")
        a = cls(**{**base, "document_date": date(2026, 9, 9)})
        b = cls(**{**base, "document_date": date(2026, 9, 2)})
        self.assertNotEqual(a.memo_document_id, b.memo_document_id)
        self.assertIn("2026-09-09", a.memo_document_id)
        self.assertEqual(a.legacy_memo_document_id, "progressive:879176249:memo:underwriting-memo")

    def test_legacy_key_entry_for_the_same_file_counts_as_delivered(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        with tempfile.TemporaryDirectory() as tmp:
            ledger = fao.FaoCancellationLedger(Path(tmp))
            ledger.ensure_private()
            path = ledger.pdf_path(date(2026, 9, 9), "M.pdf")
            path.write_bytes(PDF)
            data = ledger._load()
            data["items"]["progressive:1:memo:underwriting-memo"] = {
                "filename": "M.pdf", "issued_date": "2026-09-09", "sha256": hashlib.sha256(PDF).hexdigest()}
            ledger._write(data)
            self.assertTrue(ledger.delivery_status(
                document_id="progressive:1:memo:2026-09-09:underwriting-memo", filename="M.pdf",
                issued_on=date(2026, 9, 9), legacy_id="progressive:1:memo:underwriting-memo"))
            # A legacy entry for a different date does not count; the new memo is pulled.
            self.assertFalse(ledger.delivery_status(
                document_id="progressive:1:memo:2026-09-02:underwriting-memo", filename="M.pdf",
                issued_on=date(2026, 9, 2), legacy_id="progressive:1:memo:underwriting-memo"))


class BopNoticeRefetchTests(unittest.TestCase):
    URL = "https://bop.americanstrategic.com/Documents/Get?id=1"

    def _page(self, body=PDF):
        page = mock.Mock()
        page.context.request.get.return_value = SimpleNamespace(ok=True, body=lambda: body)
        page.context.request.post.return_value = SimpleNamespace(ok=True, body=lambda: body)
        return page

    def test_get_download_is_refetched_in_session(self):
        from robie_job_engine import progressive_bop as bop

        page = self._page()
        req = SimpleNamespace(url=self.URL, method="GET", post_data=None, headers={})
        self.assertEqual(bop._refetch_download(page, self.URL, [req]), PDF)

    def test_form_post_is_replayed_and_other_hosts_are_refused(self):
        from robie_job_engine import progressive_bop as bop

        page = self._page()
        req = SimpleNamespace(url=self.URL, method="POST", post_data="a=1",
                              headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(bop._refetch_download(page, self.URL, [req]), PDF)
        page.context.request.post.assert_called_once()
        self.assertEqual(bop._refetch_download(page, "https://evil.example/x.pdf", [req]), b"")
        self.assertEqual(bop._refetch_download(self._page(b"<html>"), self.URL, [req]), b"")

    def test_collect_pdf_uses_refetch_when_download_is_empty(self):
        from robie_job_engine import progressive_bop as bop

        page = self._page()
        page.url = "https://www.foragentsonly.com/"
        download = SimpleNamespace(url=self.URL, save_as=lambda p: Path(p).write_bytes(b""))
        handlers = {}
        page.context.on.side_effect = lambda event, fn: handlers.__setitem__(event, fn)

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(value=download)

            def __exit__(self_inner, *a):
                return False

        page.expect_download.return_value = Ctx()

        def click():
            handlers["request"](SimpleNamespace(url=self.URL, method="GET", post_data=None, headers={}))

        observation = bop.collect_pdf(page, click)
        self.assertEqual(bop.pdf_bytes_from_observation(observation), PDF)


if __name__ == "__main__":
    unittest.main()
