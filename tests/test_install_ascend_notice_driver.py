"""Shell installer for the Ascend notice driver unit, timer, and rollback."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-ascend-notice-driver.sh"
UNIT = "robie-ascend-notice-driver.service"
TIMER = "robie-ascend-notice-driver.timer"


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class InstallAscendNoticeDriverTests(unittest.TestCase):
    def _run(self, prefix: Path, log: Path, *args: str) -> subprocess.CompletedProcess[str]:
        systemctl = prefix / "systemctl"
        analyze = prefix / "systemd-analyze"
        _stub(
            systemctl,
            textwrap.dedent(
                """\
                #!/bin/bash
                printf '%s\\n' "$*" >> "$STUB_LOG"
                case "$1" in
                  is-active|is-enabled) printf '%s\\n' inactive ;;
                esac
                if [[ "$1" == "start" && "$2" == "robie-ascend-notice-driver.service" ]]; then
                  live="$STUB_PREFIX/etc/systemd/system/robie-ascend-notice-driver.service.d/30-live.conf"
                  if [[ -f "$live" ]]; then
                    printf '%s\\n' LIVE_PRESENT_AT_START >> "$STUB_LOG"
                  else
                    printf '%s\\n' DRY_AT_START >> "$STUB_LOG"
                  fi
                fi
                exit 0
                """
            ),
        )
        _stub(
            analyze,
            textwrap.dedent(
                """\
                #!/bin/bash
                printf '%s\\n' "$*" >> "$STUB_LOG"
                exit 0
                """
            ),
        )
        env = os.environ.copy()
        env["STUB_LOG"] = str(log)
        env["STUB_PREFIX"] = str(prefix)
        return subprocess.run(
            [
                "bash",
                str(INSTALLER),
                "--prefix",
                str(prefix),
                "--release-dir",
                str(ROOT),
                "--systemctl",
                str(systemctl),
                "--systemd-analyze",
                str(analyze),
                *args,
            ],
            check=False,
            env=env,
            text=True,
            capture_output=True,
        )

    def _seed_old_unit(self, prefix: Path) -> None:
        etc = prefix / "etc" / "systemd" / "system"
        dropin = etc / f"{UNIT}.d"
        dropin.mkdir(parents=True)
        (etc / UNIT).write_text(
            "OLD-UNIT-BODY\nExecStart=/old/python --live\n",
            encoding="utf-8",
        )
        (etc / TIMER).write_text(
            "[Timer]\nOnUnitActiveSec=15min\n",
            encoding="utf-8",
        )
        (dropin / "mailboxes.conf").write_text(
            "[Service]\n"
            "Environment=ASCEND_DRIVER_MAILBOXES="
            "carlo@streetsmart.insurance,outsider@example.com\n",
            encoding="utf-8",
        )
        (dropin / "one-mailbox.conf").write_text(
            "[Service]\nEnvironment=ASCEND_DRIVER_MAILBOX=solo@example.com\n",
            encoding="utf-8",
        )
        (dropin / "keep-notes.conf").write_text(
            "[Service]\n"
            "# ASCEND_DRIVER_MAILBOXES is not set in this file\n"
            "Environment=ROBIE_PLAYGROUND=1\n",
            encoding="utf-8",
        )

    def test_install_removes_mailbox_dropins_and_stays_dry(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            log = prefix / "stub.log"
            self._seed_old_unit(prefix)
            result = self._run(prefix, log)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            etc = prefix / "etc" / "systemd" / "system"
            dropin = etc / f"{UNIT}.d"
            installed = (etc / UNIT).read_text(encoding="utf-8")
            self.assertIn(
                "ExecStart=/opt/streetsmart-hermes/venv/bin/python "
                "-m robie_job_engine.ascend_notice_driver --due-days 2",
                installed,
            )
            self.assertNotIn(".hermes/hermes-agent/venv/bin/python", installed)
            self.assertNotIn("Environment=ASCEND_DRIVER_LIVE", installed)
            self.assertFalse((dropin / "mailboxes.conf").exists())
            self.assertFalse((dropin / "one-mailbox.conf").exists())
            self.assertTrue((dropin / "keep-notes.conf").exists())
            self.assertTrue((dropin / "10-write-scope.conf").is_file())
            self.assertFalse((dropin / "30-live.conf").exists())
            timer = (etc / TIMER).read_text(encoding="utf-8")
            self.assertIn("Persistent=true", timer)
            self.assertIn("OnBootSec=5min", timer)
            self.assertIn("OnActiveSec=15min", timer)
            self.assertIn("OnUnitActiveSec=15min", timer)
            state = prefix / "var" / "lib" / "robie-ascend-notice-driver"
            self.assertTrue(state.is_dir())
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
            text = log.read_text(encoding="utf-8")
            self.assertIn("DRY_AT_START", text)
            self.assertNotIn("LIVE_PRESENT_AT_START", text)
            self.assertIn("daemon-reload", text)
            self.assertIn(f"verify {etc / UNIT}", text)
            self.assertIn(f"disable --now {TIMER}", text)
            self.assertNotIn(f"enable --now {TIMER}", text)
            self.assertIn("timer left stopped", result.stdout)
            self.assertIn("live drop-in not installed", result.stdout)
            backups = [
                path
                for path in (prefix / "var" / "backups").glob("robie-ascend-notice-driver-*")
                if (path / UNIT).is_file()
                and "OLD-UNIT-BODY" in (path / UNIT).read_text(encoding="utf-8")
            ]
            self.assertEqual(len(backups), 1)
            self.assertIn(
                "outsider@example.com",
                (backups[0] / f"{UNIT}.d" / "mailboxes.conf").read_text(encoding="utf-8"),
            )

            log.write_text("", encoding="utf-8")
            live = self._run(prefix, log, "--live", "--enable-timer")
            self.assertEqual(live.returncode, 0, live.stderr + live.stdout)
            live_text = (dropin / "30-live.conf").read_text(encoding="utf-8")
            self.assertIn("Environment=ASCEND_DRIVER_LIVE=1", live_text)
            second = log.read_text(encoding="utf-8")
            self.assertIn("DRY_AT_START", second)
            self.assertNotIn("LIVE_PRESENT_AT_START", second)
            self.assertIn(f"enable --now {TIMER}", second)
            self.assertIn("live drop-in installed", live.stdout)

            log.write_text("", encoding="utf-8")
            again = self._run(prefix, log)
            self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
            self.assertFalse((dropin / "30-live.conf").exists())
            self.assertIn(f"disable --now {TIMER}", log.read_text(encoding="utf-8"))

            rollback = subprocess.run(
                [
                    "bash",
                    str(INSTALLER),
                    "--prefix",
                    str(prefix),
                    "--rollback",
                    str(backups[0]),
                    "--systemctl",
                    str(prefix / "systemctl"),
                    "--systemd-analyze",
                    str(prefix / "systemd-analyze"),
                ],
                check=False,
                env={**os.environ, "STUB_LOG": str(log), "STUB_PREFIX": str(prefix)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr + rollback.stdout)
            self.assertIn("OLD-UNIT-BODY", (etc / UNIT).read_text(encoding="utf-8"))
            self.assertIn(
                "outsider@example.com",
                (dropin / "mailboxes.conf").read_text(encoding="utf-8"),
            )
            self.assertTrue((dropin / "one-mailbox.conf").is_file())
            self.assertFalse((dropin / "30-live.conf").exists())
            self.assertFalse((dropin / "10-write-scope.conf").exists())
            self.assertIn("timer left stopped", rollback.stdout)

    def test_install_copies_legacy_note_ledger_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            legacy_dir = (
                prefix
                / "opt"
                / "streetsmart-hermes"
                / "robie-job-engine"
                / "data"
            )
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / "discussion-note-ledger.json"
            note = {
                "applicant_id": "220250093",
                "discussion_id": "d-pay",
                "document_id": "",
                "note_text_sha256": "abc123",
                "note_id": "n-1",
            }
            legacy.write_text(
                json.dumps({"version": 1, "notes": [note]}),
                encoding="utf-8",
            )
            legacy.chmod(0o600)
            log = prefix / "stub.log"
            result = self._run(prefix, log)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("note ledger migration: copied", result.stdout)
            dest = (
                prefix
                / "var"
                / "lib"
                / "robie-ascend-notice-driver"
                / "discussion-note-ledger.json"
            )
            self.assertTrue(dest.is_file())
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            state = dest.parent
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
            saved = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(saved["notes"], [note])
            legacy.write_text(
                json.dumps({"version": 1, "notes": [note, {"applicant_id": "9"}]}),
                encoding="utf-8",
            )
            again = self._run(prefix, log)
            self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
            self.assertIn("note ledger migration: kept", again.stdout)
            kept = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(kept["notes"], [note])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "preview"
            prefix.mkdir()
            log = prefix / "stub.log"
            result = self._run(prefix, log, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("dry-run:", result.stdout)
            self.assertIn("ASCEND_DRIVER_MAILBOX", result.stdout)
            self.assertIn("discussion-note-ledger.json mode 0600", result.stdout)
            self.assertIn("owner streetsmart-hermes", result.stdout)
            self.assertIn("disable --now", result.stdout)
            self.assertFalse((prefix / "etc").exists())
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
