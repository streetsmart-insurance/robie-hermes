"""Synthetic fixtures only. Proves the health check both ways."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

from applied_pay.proof_health_check import probe, main, send_alert


def good_report():
    return {"mode": "scheduled_proof_captured", "verification_label": "UNVERIFIED",
            "bank_actions": 0, "qbo_posts": 0, "ezlynx_writes": 0,
            "notes_written": 0, "transfers": 0, "matches": []}


class HealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.report = os.path.join(self.tmp, "proof-daily.json")

    def write_report(self, data, mtime=None):
        with open(self.report, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        if mtime is not None:
            os.utime(self.report, (mtime, mtime))

    def test_green_quiet(self):
        self.write_report(good_report())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--reports-dir", self.tmp])
        self.assertEqual(0, code)
        self.assertEqual("", buf.getvalue())  # quiet when healthy

    def test_missing_report_red(self):
        green, reason = probe(self.tmp)
        self.assertFalse(green)
        self.assertIn("missing", reason)

    def test_stale_report_red(self):
        self.write_report(good_report(), mtime=time.time() - 30 * 3600)
        green, reason = probe(self.tmp, max_age_s=26 * 3600)
        self.assertFalse(green)
        self.assertIn("stale", reason)

    def test_write_counters_red(self):
        bad = good_report()
        bad["qbo_posts"] = 1
        self.write_report(bad)
        green, reason = probe(self.tmp)
        self.assertFalse(green)
        self.assertIn("write activity", reason)

    def test_missing_unverified_label_red(self):
        bad = good_report()
        bad["verification_label"] = "VERIFIED"
        self.write_report(bad)
        green, reason = probe(self.tmp)
        self.assertFalse(green)
        self.assertIn("UNVERIFIED", reason)

    def test_corrupt_report_red(self):
        with open(self.report, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        green, reason = probe(self.tmp)
        self.assertFalse(green)
        self.assertIn("unreadable", reason)

    def test_alert_posts_to_webhook(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/hook" % server.server_address[1]
            sent = send_alert("proof report missing", webhook_url=url)
        finally:
            server.shutdown()
        self.assertTrue(sent)
        self.assertEqual(1, len(received))
        self.assertIn("proof report missing", received[0]["text"])

    def test_alert_without_webhook_prints(self):
        old = os.environ.pop("APPLIED_PAY_HEALTH_CHAT_WEBHOOK", None)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                sent = send_alert("proof report missing", webhook_url="")
            self.assertFalse(sent)
            self.assertIn("ALERT", buf.getvalue())
        finally:
            if old is not None:
                os.environ["APPLIED_PAY_HEALTH_CHAT_WEBHOOK"] = old

    def test_main_red_exit_code(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--reports-dir", self.tmp])
        self.assertEqual(1, code)
        self.assertIn("RED", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
