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
            close_tabs=lambda url: tabs.append(url) or [], open_tab=lambda name, url: None,
        )
        skipped = set(daily.daily_skip_names())
        self.assertEqual(skipped, {"guard", "travelers"})
        self.assertEqual(calls, [name for name in daily.DAILY_CARRIERS if name not in skipped])
        self.assertEqual(summary["carriers"]["guard"]["status"], "SKIPPED")
        self.assertEqual(summary["carriers"]["travelers"]["status"], "SKIPPED")
        self.assertEqual(summary["carriers"]["guard"]["reason"], "skipped, login not ready")
        self.assertEqual(len(tabs), len(calls) + 1)
        self.assertEqual(os.environ[daily.KILL_SWITCH_ENV], "0")
        text = (self.root / "runs" / "2026-10-09" / "summary.txt").read_text()
        self.assertIn("nothing filed to EZLynx", text)
        self.assertIn("skipped, login not ready", text)

    def test_empty_skip_setting_runs_guard_and_travelers(self):
        calls = []
        with mock.patch.dict(os.environ, {daily.SKIP_ENV: ""}, clear=False):
            daily.run_daily(
                day=date(2026, 10, 9), root=self.root, carriers=("guard", "travelers"),
                run_one=lambda name, **kw: calls.append(name) or self._ok(name),
                close_tabs=lambda url: [], open_tab=lambda name, url: None,
            )
        self.assertEqual(calls, ["guard", "travelers"])

    def test_exit_0_when_the_rest_are_ok_or_held_and_skipped_do_not_count(self):
        def run_one(name, **_kw):
            if name == "geico":
                return {"display": "GEICO", "status": "HELD", "reason": "the portal asked to sign in again",
                        "downloaded": 0, "held": []}
            return self._ok(name)

        summary = daily.run_daily(
            day=date(2026, 10, 10), root=self.root, carriers=("geico", "guard", "natgen"),
            run_one=run_one, close_tabs=lambda url: [], open_tab=lambda name, url: None,
        )
        self.assertEqual(summary["carriers"]["guard"]["status"], "SKIPPED")
        self.assertEqual(daily.carrier_exit_code(summary), 0)

    def test_exit_3_after_drive_and_notify_when_a_carrier_failed_or_was_partial(self):
        events = []

        def run_one(name, **_kw):
            events.append("pull")
            if name == "uticafirst":
                return {"display": "Utica First", "status": "FAILED", "downloaded": 0, "held": [],
                        "error": "SecretManagerAccessError: NEEDS_AUTH: cannot read secret 'utica_password'"}
            return {"display": "Progressive (FAO)", "status": "PARTIAL", "downloaded": 1, "held": [],
                    "reason": "the pull stopped early", "unprocessed": 4}

        def drive_factory():
            events.append("drive")
            return object()

        with mock.patch.object(daily, "upload_carrier", side_effect=lambda *a, **k: events.append("upload") or {"status": "OK", "uploaded": [], "skipped": [], "held": []}), \
                mock.patch.object(daily, "notify", side_effect=lambda text: events.append("notify") or "posted"):
            summary = daily.run_daily(
                day=date(2026, 10, 10), root=self.root, carriers=("uticafirst", "progressive"),
                upload_drive=True, do_notify=True, run_one=run_one,
                close_tabs=lambda url: [], open_tab=lambda name, url: None, drive_factory=drive_factory,
            )
        self.assertEqual(events, ["drive", "pull", "upload", "pull", "upload", "notify"])
        self.assertEqual(daily.carrier_exit_code(summary), 3)
        text = (self.root / "runs" / "2026-10-10" / "summary.txt").read_text()
        self.assertIn("could not read the saved login, permission problem", text)
        self.assertNotIn("SecretManagerAccessError", text)
        self.assertNotIn("utica_password", text)
        self.assertNotIn("stopped with an error", text)

    def test_main_returns_3_for_a_failure_and_2_when_the_environment_refuses(self):
        def run_one(name, **_kw):
            if name == "geico":
                return {"display": "GEICO", "status": "HELD", "reason": "session was not signed in",
                        "downloaded": 0, "held": []}
            return {"display": name, "status": "OK", "downloaded": 0, "held": []}

        with mock.patch.object(daily, "run_carrier", side_effect=run_one):
            code = daily.main(["--root", str(self.root), "--as-of", "2026-10-10", "--carriers", "geico,natgen,guard"])
        self.assertEqual(code, 0)
        with mock.patch.object(daily, "run_carrier", side_effect=lambda name, **kw: {
            "display": "Utica First", "status": "FAILED", "downloaded": 0, "held": [], "error": "boom",
        }):
            code = daily.main(["--root", str(self.root), "--as-of", "2026-10-10", "--carriers", "uticafirst"])
        self.assertEqual(code, 3)
        refused = self.root / "refused"
        with mock.patch.dict(os.environ, {"ROBIE_ENV": "PROD"}):
            code = daily.main(["--root", str(refused), "--as-of", "2026-10-10", "--carriers", "geico"])
        self.assertEqual(code, 2)
        self.assertFalse((refused / "runs").exists())

    def test_secret_manager_error_during_sign_in_is_a_failed_carrier_and_does_not_start_the_pull(self):
        from robie_job_engine.gcp_secret_reader import SecretManagerAccessError

        ran = []

        def open_tab(name, _url):
            raise SecretManagerAccessError("NEEDS_AUTH: the runtime service account cannot read secret 'geico_password'")

        summary = daily.run_daily(
            day=date(2026, 10, 10), root=self.root, carriers=("geico",),
            run_one=lambda name, **kw: ran.append(name) or self._ok(name),
            close_tabs=lambda url: [], open_tab=open_tab,
        )
        self.assertEqual(ran, [])
        self.assertEqual(summary["carriers"]["geico"]["status"], "FAILED")
        self.assertEqual(daily.carrier_exit_code(summary), 3)
        text = summary["text"]
        self.assertIn("GEICO: failed, could not read the saved login, permission problem", text)
        self.assertNotIn("geico_password", text)

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
            close_tabs=lambda url: [], open_tab=lambda name, url: None,
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
            run_one=lambda name, **kw: self._ok(name), close_tabs=lambda url: [], open_tab=lambda name, url: None, drive_factory=no_drive,
        )
        self.assertEqual(summary["carriers"]["natgen"]["status"], "OK")
        self.assertIn("not configured", summary["carriers"]["natgen"]["drive"]["reason"])

    def test_production_host_runs_only_with_filing_forced_off(self):
        with mock.patch.object(daily.socket, "gethostname", return_value="hermes-poc-01"), \
                mock.patch.object(daily.socket, "getfqdn", return_value="hermes-poc-01"), \
                mock.patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION", daily.KILL_SWITCH_ENV: "1"}):
            summary = daily.run_daily(
                day=date(2026, 10, 9), root=self.root, carriers=("geico",),
                run_one=lambda name, **kw: self._ok(name),
                close_tabs=lambda url: [], open_tab=lambda name, url: None,
            )
        self.assertEqual(summary["carriers"]["geico"]["status"], "OK")
        self.assertEqual(os.environ[daily.KILL_SWITCH_ENV], "0")

    def test_refuses_outside_test(self):
        with mock.patch.dict(os.environ, {"ROBIE_ENV": "PROD"}):
            with self.assertRaises(Exception):
                daily.run_daily(day=date(2026, 10, 9), root=self.root, run_one=self._ok, close_tabs=lambda u: [])

    def test_only_local_cdp(self):
        with self.assertRaises(Exception):
            daily.close_stale_tabs("http://10.0.0.5:9223", http=lambda *a, **k: [])

    def test_never_attaches_to_the_ezlynx_chrome_on_9222(self):
        touched = []
        with self.assertRaisesRegex(Exception, "9223"):
            daily.close_stale_tabs("http://127.0.0.1:9222", http=lambda *a, **k: touched.append(a))
        self.assertEqual(touched, [])
        with self.assertRaisesRegex(Exception, "9223"):
            daily.run_daily(day=date(2026, 10, 9), root=self.root, cdp_url="http://127.0.0.1:9222",
                            run_one=self._ok, close_tabs=lambda u: [])
        with mock.patch.dict(os.environ, {"ROBIE_BROWSER_CDP_URL": "http://127.0.0.1:9222"}):
            self.assertEqual(daily.build_parser().parse_args([]).cdp_url, "http://127.0.0.1:9223")
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.dict(os.environ, {"ROBIE_BROWSER_CDP_URL": "http://127.0.0.1:9222"}):
            daily.run_carrier("natgen", day=date(2026, 10, 9), root=self.root,
                              cdp_url="http://127.0.0.1:9223", runner=runner)
        self.assertEqual(seen["env"]["ROBIE_BROWSER_CDP_URL"], "http://127.0.0.1:9223")
        self.assertIn("http://127.0.0.1:9223", seen["cmd"])

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
        self.assertIn("--notify", exec_line)
        self.assertIn("--upload-drive", exec_line)
        self.assertIn("/var/lib/robie-carrier-pull-test/release", service)
        self.assertIn("--cdp-url http://127.0.0.1:9223", exec_line)
        self.assertNotIn("9222", exec_line)
        self.assertIn("Mon..Fri *-*-* 07:30:00 America/New_York", timer)
        prod_service = (base / "robie-carrier-pull.service").read_text()
        prod_timer = (base / "robie-carrier-pull.timer").read_text()
        self.assertIn("ROBIE_ENV=PRODUCTION", prod_service)
        self.assertIn("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0", prod_service)
        self.assertNotIn("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1", prod_service)
        self.assertIn("--cdp-url http://127.0.0.1:9223", prod_service)
        self.assertIn("--upload-drive", prod_service)
        self.assertIn("--notify", prod_service)
        self.assertIn("NOT enabled", prod_timer)
        self.assertIn("Mon..Fri *-*-* 07:30:00 America/New_York", prod_timer)
        self.assertIn("Persistent=false", prod_timer)
        from robie_job_engine.document_retrieval_filing import PROD_FILING_CARRIERS
        self.assertNotIn("bop", PROD_FILING_CARRIERS)


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


class BopClExpressHomeTests(unittest.TestCase):
    """Live 2026-10-08: BOP started with the FAO tab left on a CL Express policy page."""

    def test_cl_express_tab_goes_to_manage_policies_landing(self):
        from robie_job_engine import progressive_bop as bop

        page = mock.Mock()
        page.url = "https://clpolicy.foragentsonly.com/Express/Default.aspx"

        def goto(url, **kw):
            page.url = url

        page.goto.side_effect = goto
        with mock.patch.object(bop, "assert_authenticated"):
            bop.ensure_fao_shell_home(page)
        page.goto.assert_called_once()
        self.assertEqual(page.url, bop.MANAGE_POLICIES_LANDING_URL)

    def test_shell_home_is_a_no_op(self):
        from robie_job_engine import progressive_bop as bop

        page = mock.Mock(url=bop.MANAGE_POLICIES_LANDING_URL)
        bop.ensure_fao_shell_home(page)
        page.goto.assert_not_called()


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


class OpenCarrierTabTests(unittest.TestCase):
    def test_calls_geico_and_natgen_sign_in_helpers(self):
        called = []

        def sign_in(url):
            called.append(url)
            return "https://gateway2.geico.com/"

        opened = daily.ensure_carrier_tab(
            "geico", "http://127.0.0.1:9223", http=lambda *a, **k: [], sign_in=sign_in
        )
        self.assertEqual(opened, "https://gateway2.geico.com/")
        self.assertEqual(called, ["http://127.0.0.1:9223"])
        self.assertIsNone(
            daily.ensure_carrier_tab("utica", "http://127.0.0.1:9223", http=lambda *a, **k: [], sign_in=sign_in)
        )

    def test_refuses_the_ezlynx_chrome(self):
        with self.assertRaises(Exception):
            daily.ensure_carrier_tab(
                "geico", "http://127.0.0.1:9222", http=lambda *a, **k: [], sign_in=lambda u: u
            )

    def test_run_daily_opens_tabs_and_turns_failed_sign_in_into_plain_hold(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["ROBIE_ENV"] = "TEST"
        opened = []

        def run_one(name, **_):
            if name == "geico":
                return {"display": "GEICO", "status": "HELD", "downloaded": 0, "held": [],
                        "reason": "Geico Gateway session is not authenticated"}
            return {"display": name, "status": "OK", "downloaded": 0, "held": []}

        summary = daily.run_daily(
            day=date(2026, 10, 9), root=Path(tmp.name), carriers=("geico", "natgen"),
            run_one=run_one, close_tabs=lambda url: [],
            open_tab=lambda name, url: opened.append(name) or "signed-in",
        )
        self.assertEqual(opened, ["geico", "natgen"])
        self.assertIn("GEICO: held. GEICO sign-in did not leave a usable session", summary["text"])


class GeicoNatgenLoginTests(unittest.TestCase):
    def test_geico_credentials_parse_producer_shapes(self):
        from robie_job_engine import geico_login as gl

        with mock.patch.object(gl, "_get_secret", side_effect=lambda n: {
            "geico-gateway-producer": '{"username":"I001234","password":"x"}',
        }[n]):
            self.assertEqual(gl.geico_credentials(), ("I001234", "x"))
        with mock.patch.object(gl, "_get_secret", side_effect=lambda n: {
            "geico-gateway-producer": "I009999",
            "geico-extend-password": "pw",
        }[n]):
            self.assertEqual(gl.geico_credentials(), ("I009999", "pw"))

    def test_geico_login_skips_typing_when_already_signed_in(self):
        from robie_job_engine import geico_login as gl

        page = mock.MagicMock()
        page.url = "https://gateway2.geico.com/"
        page.evaluate.return_value = "Welcome to GEICO Gateway"
        page.locator.return_value.count.return_value = 0
        ctx = mock.MagicMock()
        ctx.pages = []
        ctx.new_page.return_value = page
        out = gl.login_geico(ctx, sleep=lambda s: None, credentials=lambda: ("u", "p"))
        self.assertIs(out, page)
        page.locator.return_value.first.fill.assert_not_called()

    def test_natgen_login_reads_email_code_on_mfa(self):
        from robie_job_engine import natgen_login as nl

        page = mock.MagicMock()
        page.url = "https://natgenagency.com/login"
        page.evaluate.return_value = "Multi-factor verification required"
        # locator chain used for fills / clicks
        loc = mock.MagicMock()
        loc.count.return_value = 1
        loc.first.is_visible.return_value = True
        page.locator.return_value = loc
        page.get_by_text.return_value.first.click.return_value = None
        page.get_by_role.return_value.first.click.return_value = None
        ctx = mock.MagicMock()
        ctx.pages = []
        ctx.new_page.return_value = page
        codes = []

        def reader(**kw):
            codes.append(kw.get("not_before"))
            return "123456"

        # After MFA, goto reports lands signed in
        def set_url(url, **kw):
            page.url = url
        page.goto.side_effect = set_url
        # Force MFA path: first is_signed_in False (login path), then True after MFA
        with mock.patch.object(nl, "is_signed_in", side_effect=[False, True]):
            out = nl.login_natgen(
                ctx, sleep=lambda s: None, clock=lambda: 1000.0,
                credentials=lambda: ("user", "pass"), code_reader=reader,
            )
        self.assertIs(out, page)
        self.assertEqual(codes, [1000.0])
        loc.first.fill.assert_any_call("123456")

    def test_natgen_other_window_holds_without_clicking_enable_login(self):
        from robie_job_engine import natgen_login as nl
        from robie_job_engine.intake_core import IntakeHold

        page = mock.MagicMock()
        page.url = "https://natgenagency.com/"
        page.evaluate.return_value = "You appear to be logged in via another window. Enable Login"
        box = mock.Mock()
        box.count.return_value = 1
        box.is_disabled.return_value = True
        page.locator.return_value = box
        ctx = mock.MagicMock()
        ctx.pages = []
        ctx.new_page.return_value = page
        with self.assertRaisesRegex(IntakeHold, "NatGen session was taken by another window; Enable Login needed"):
            nl.login_natgen(ctx, sleep=lambda s: None, credentials=lambda: ("user", "pass"))
        box.fill.assert_not_called()

    def test_natgen_error_panel_holds_without_clicking_enable_login(self):
        from robie_job_engine import natgen_login as nl
        from robie_job_engine.intake_core import IntakeHold

        clicks = []

        class Node:
            def __init__(self, text="", disabled=False):
                self.text = text
                self.disabled = disabled

            def count(self):
                return 1

            @property
            def first(self):
                return self

            def inner_text(self):
                return self.text

            def is_disabled(self):
                return self.disabled

            def click(self, **_kwargs):
                clicks.append("enable")

            def fill(self, *_args, **_kwargs):
                clicks.append("fill")

        page = mock.Mock()
        page.url = "https://natgenagency.com/"
        page.evaluate.return_value = "National General home"

        def locator(selector):
            if selector == "#pnlErrorsAllstate":
                return Node("WARNING: You appear to be logged in via another window or browser.")
            if selector == "#btnEnableLogin":
                return Node()
            if selector == "#txtUserID":
                return Node(disabled=True)
            return Node()

        page.locator.side_effect = locator
        ctx = mock.MagicMock()
        ctx.pages = []
        ctx.new_page.return_value = page
        with self.assertRaisesRegex(IntakeHold, "Enable Login needed"):
            nl.login_natgen(ctx, sleep=lambda s: None, credentials=lambda: ("user", "pass"))
        self.assertEqual(clicks, [])


class PlainSummaryTests(unittest.TestCase):
    def test_summary_has_no_field_names_selectors_or_exception_names(self):
        summary = {
            "as_of": "2026-10-09",
            "carriers": {
                "progressive": {"display": "Progressive (FAO)", "status": "FAILED", "downloaded": 0, "held": [],
                                "error": "exit -15"},
                "progressive_bop": {"display": "Progressive BOP", "status": "HELD", "downloaded": 0, "held": [],
                                    "reason": 'a[data-at="header-nav__parent-link"] matched 0; page https://x'},
                "geico": {"display": "GEICO", "status": "FAILED", "downloaded": 0, "held": [],
                          "error": "TimeoutExpired: timed out after 1800 s"},
                "utica": {"display": "Utica First", "status": "OK", "downloaded": 2,
                          "held": ["KeyError: 'policy_number'", "Utica First policy X has no notice document"],
                          "drive": {"status": "HELD", "reason": "RefreshError: invalid_grant"}},
            },
            "totals": {"pdfs": 2, "uploaded": 0, "failed": 2},
        }
        text = daily.render_summary(summary)
        for bad in ("[", "=", "http", "Error", "Expired", "policy_number", "invalid_grant", "exit -15"):
            self.assertNotIn(bad, text)
        self.assertIn("Progressive (FAO): failed, stopped before it finished", text)
        self.assertIn("GEICO: failed, did not finish in time", text)
        self.assertIn("Progressive BOP: held. a carrier page did not look as expected", text)
        self.assertIn("not uploaded to Drive: Drive was not reachable", text)
        self.assertIn("1 held: Utica First policy X has no notice document", text)

    def test_secret_manager_permission_is_plain_english(self):
        summary = {
            "as_of": "2026-10-10",
            "carriers": {
                "uticafirst": {
                    "display": "Utica First",
                    "status": "FAILED",
                    "downloaded": 0,
                    "held": [],
                    "error": "SecretManagerAccessError: NEEDS_AUTH: cannot read secret 'utica_password'",
                },
            },
            "totals": {"pdfs": 0, "uploaded": 0, "failed": 1, "partial": 0},
        }
        text = daily.render_summary(summary)
        self.assertIn("Utica First: failed, could not read the saved login, permission problem", text)
        self.assertNotIn("SecretManagerAccessError", text)
        self.assertNotIn("NEEDS_AUTH", text)
        self.assertNotIn("stopped with an error", text)

    def test_partial_is_not_reported_as_ok(self):
        summary = {
            "as_of": "2026-10-09",
            "carriers": {
                "progressive": {
                    "display": "Progressive (FAO)",
                    "status": "PARTIAL",
                    "downloaded": 3,
                    "held": [],
                    "reason": "the pull stopped early",
                    "unprocessed": 26,
                },
            },
            "totals": {"pdfs": 3, "uploaded": 0, "failed": 0, "partial": 1},
        }
        text = daily.render_summary(summary)
        self.assertIn("Progressive (FAO): partial. 3 downloaded, 26 policies left.", text)
        self.assertNotIn("OK", text)
        payload = {"carriers": {"progressive": {
            "status": "PARTIAL", "downloaded": 3, "held": [], "reason": "the pull stopped early",
            "unprocessed": 26,
        }}}
        result = daily.normalize_result("progressive", payload, returncode=1, stderr="")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["unprocessed"], 26)


class GeicoSessionExpiredTests(unittest.TestCase):
    def test_expired_gateway_page_is_not_signed_in_and_holds_the_pull(self):
        from robie_job_engine import geico_login as gl
        from robie_job_engine import geico_pending_cancellation_noc as geico
        from robie_job_engine.intake_core import IntakeHold

        page = mock.MagicMock()
        page.url = "https://gateway2.geico.com/client-alerts"
        page.evaluate.return_value = (
            "Session expired\nFor the protection of your agency and customers, your GEICO session has ended."
        )
        page.locator.return_value.count.return_value = 0
        self.assertFalse(gl.is_signed_in(page))
        with self.assertRaises(IntakeHold):
            geico.assert_authenticated(page)
