"""Decision + why-record tests for the hourly EZLynx session owner.

No SSH. No live Chrome. No passwords. Logout cause stays UNVERIFIED.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from scripts.ezlynx_session_monitor import (
    CAP_EVALUATED,
    CAP_UNEVALUATED,
    REMOTE_STATE_PATH,
    UNVERIFIED,
    apply_after,
    decide,
    is_logged_out,
    load_last_state,
    parse_chrome_show,
)


ROOT = Path(__file__).resolve().parents[1]
LOGIN_TAB = {
    "id": "E7FD9BADD6E996313D607D2BD08D00DD",
    "title": "Login",
    "url": "https://app.ezlynx.com/auth/account/login?redirectURL=https%3A%2F%2Fapp.ezlynx.com%2Fweb%2F",
}
POLICIES_TAB = {
    "id": "AAAA",
    "title": "ROBIE Test LLC - Policies",
    "url": "https://app.ezlynx.com/web/account/220250093/policies",
}
CHROME_SHOW = (
    "MainPID=18821\n"
    "ActiveEnterTimestamp=Sat 2026-09-12 06:53:16 EDT\n"
    "ExecMainStartTimestamp=Sat 2026-09-12 06:53:16 EDT\n"
)


def _logged_out_check() -> dict:
    return {
        "state": "LOGGED_OUT",
        "exit_code": 2,
        "reason": "SESSION_LOGGED_OUT",
        "pages": [LOGIN_TAB],
        "probe_result": {"probe": "LOGGED_OUT"},
    }


def _ok_check() -> dict:
    return {
        "state": "SESSION_PRESENT",
        "exit_code": 0,
        "reason": "not proof",
        "pages": [POLICIES_TAB],
    }


class ParseChromeShowTests(unittest.TestCase):
    def test_reads_pid_and_both_start_timestamps(self):
        chrome = parse_chrome_show(CHROME_SHOW)
        self.assertEqual(chrome["chrome_pid"], "18821")
        self.assertEqual(chrome["chrome_active_enter"], "Sat 2026-09-12 06:53:16 EDT")
        self.assertEqual(chrome["chrome_exec_start"], "Sat 2026-09-12 06:53:16 EDT")


class DecideLoginCapTests(unittest.TestCase):
    def test_first_logged_out_allows_exactly_one_login(self):
        decision = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            {},
            cap_state=CAP_UNEVALUATED,
        )
        self.assertTrue(decision["attempt_login"])
        self.assertFalse(decision["fail_consecutive"])
        self.assertEqual(decision["why"]["consecutive_logged_out"], 1)
        self.assertEqual(decision["why"]["cap_state"], CAP_UNEVALUATED)
        self.assertIsInstance(decision["attempt_login"], bool)

    def test_second_consecutive_logged_out_stops_and_fails(self):
        last = {
            "exit_code": 2,
            "state": "LOGGED_OUT",
            "chrome_pid": "18821",
            "chrome_exec_start": "Sat 2026-09-12 06:53:16 EDT",
        }
        decision = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            last,
            cap_state=CAP_EVALUATED,
        )
        self.assertFalse(decision["attempt_login"])
        self.assertTrue(decision["fail_consecutive"])
        self.assertEqual(decision["why"]["consecutive_logged_out"], 2)
        self.assertEqual(decision["why"]["cap_state"], CAP_EVALUATED)

    def test_failed_bootstrap_recheck_counts_as_logged_out_for_next_hour(self):
        before = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            {},
            cap_state=CAP_UNEVALUATED,
        )
        after = apply_after(before["next_state"], _logged_out_check(), login_attempted=True)
        self.assertTrue(after["login_attempted"])
        self.assertTrue(is_logged_out(after))
        next_hour = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            after,
            cap_state=CAP_EVALUATED,
        )
        self.assertFalse(next_hour["attempt_login"])
        self.assertTrue(next_hour["fail_consecutive"])

    def test_ok_then_logged_out_allows_one_login(self):
        last = {
            "exit_code": 0,
            "state": "SESSION_PRESENT",
            "chrome_pid": "18821",
            "chrome_exec_start": "Sat 2026-09-12 06:53:16 EDT",
        }
        decision = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            last,
            cap_state=CAP_EVALUATED,
        )
        self.assertTrue(decision["attempt_login"])
        self.assertFalse(decision["fail_consecutive"])
        self.assertEqual(decision["why"]["cap_state"], CAP_EVALUATED)

    def test_undetermined_does_not_login_guess(self):
        check = {"state": "UNREACHABLE", "exit_code": 1, "pages": []}
        decision = decide(
            check, parse_chrome_show(CHROME_SHOW), {}, cap_state=CAP_UNEVALUATED
        )
        self.assertFalse(decision["attempt_login"])
        self.assertFalse(decision["fail_consecutive"])

    def test_missing_or_unreadable_last_state_is_unevaluated_and_still_allows_one_login(self):
        chrome = parse_chrome_show(CHROME_SHOW)
        with durable_temporary_directory() as td:
            root = Path(td)
            missing, missing_cap = load_last_state(root / "absent.json")
            (root / "empty.json").write_text("{}\n", encoding="utf-8")
            empty, empty_cap = load_last_state(root / "empty.json")
            (root / "junk.json").write_text("not-json", encoding="utf-8")
            junk, junk_cap = load_last_state(root / "junk.json")
        for last, cap in (
            (missing, missing_cap),
            (empty, empty_cap),
            (junk, junk_cap),
        ):
            self.assertEqual(cap, CAP_UNEVALUATED)
            decision = decide(_logged_out_check(), chrome, last, cap_state=cap)
            self.assertTrue(decision["attempt_login"])
            self.assertFalse(decision["fail_consecutive"])
            self.assertEqual(decision["why"]["cap_state"], CAP_UNEVALUATED)


class WhyRecordTests(unittest.TestCase):
    def test_why_records_tabs_pid_start_and_pid_change_without_claiming_cause(self):
        last = {
            "exit_code": 0,
            "state": "SESSION_PRESENT",
            "chrome_pid": "10001",
            "chrome_active_enter": "Fri 2026-09-11 03:30:00 EDT",
            "chrome_exec_start": "Fri 2026-09-11 03:30:00 EDT",
        }
        decision = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            last,
            cap_state=CAP_EVALUATED,
        )
        why = decision["why"]
        self.assertEqual(why["tabs_before"][0]["url"], LOGIN_TAB["url"])
        self.assertEqual(why["chrome_pid"], "18821")
        self.assertEqual(why["chrome_active_enter"], "Sat 2026-09-12 06:53:16 EDT")
        self.assertEqual(why["chrome_exec_start"], "Sat 2026-09-12 06:53:16 EDT")
        self.assertTrue(why["pid_changed_since_last"])
        self.assertTrue(why["start_time_changed_since_last"])
        self.assertEqual(why["last_check_pid"], "10001")
        self.assertEqual(why["logout_cause"], UNVERIFIED)
        self.assertNotIn("03:30 restart caused", json.dumps(why))

    def test_same_pid_is_recorded_as_not_changed(self):
        last = {
            "exit_code": 0,
            "state": "SESSION_PRESENT",
            "chrome_pid": "18821",
            "chrome_exec_start": "Sat 2026-09-12 06:53:16 EDT",
        }
        why = decide(
            _logged_out_check(),
            parse_chrome_show(CHROME_SHOW),
            last,
            cap_state=CAP_EVALUATED,
        )["why"]
        self.assertFalse(why["pid_changed_since_last"])
        self.assertFalse(why["start_time_changed_since_last"])
        self.assertEqual(why["logout_cause"], UNVERIFIED)

    def test_state_file_path_is_the_small_iap_writable_tmp_file(self):
        self.assertEqual(
            REMOTE_STATE_PATH,
            "/var/tmp/robie-ezlynx-session-monitor/last-check.json",
        )

    def test_cli_decide_and_apply_after_round_trip(self):
        from scripts.ezlynx_session_monitor import main

        with durable_temporary_directory() as td:
            root = Path(td)
            (root / "check.json").write_text(json.dumps(_logged_out_check()), encoding="utf-8")
            (root / "chrome.txt").write_text(CHROME_SHOW, encoding="utf-8")
            (root / "last.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "decide",
                        "--check-json",
                        str(root / "check.json"),
                        "--chrome-show",
                        str(root / "chrome.txt"),
                        "--last-state",
                        str(root / "last.json"),
                        "--decision-out",
                        str(root / "decision.json"),
                        "--next-state",
                        str(root / "next.json"),
                        "--why-out",
                        str(root / "why.json"),
                    ]
                ),
                0,
            )
            why = json.loads((root / "why.json").read_text(encoding="utf-8"))
            self.assertTrue(why["attempt_login"])
            self.assertEqual(why["cap_state"], CAP_UNEVALUATED)
            (root / "after.json").write_text(json.dumps(_ok_check()), encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "apply-after",
                        "--next-state",
                        str(root / "next.json"),
                        "--after-json",
                        str(root / "after.json"),
                        "--login-attempted",
                    ]
                ),
                0,
            )
            nxt = json.loads((root / "next.json").read_text(encoding="utf-8"))
            self.assertEqual(nxt["exit_code"], 0)
            self.assertTrue(nxt["login_attempted"])
            self.assertEqual(nxt["consecutive_logged_out"], 0)

    def test_module_has_no_password_literals(self):
        text = (ROOT / "scripts" / "ezlynx_session_monitor.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"""(?i)(?:password|passwd)\s*[:=]\s*['\"][^'\"]+['\"]""")


if __name__ == "__main__":
    unittest.main()
