"""Installer for the unmatched-notice digest timer."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from robie_job_engine import ascend_unmatched_digest as digest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-ascend-unmatched-digest.sh"
UNIT = "robie-ascend-unmatched-digest.service"
TIMER = "robie-ascend-unmatched-digest.timer"
# Monday 10:00 ET, after the 9:30 health deadline.
LATE_WEEKDAY = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def _marker(prefix: Path) -> Path:
    return (
        prefix
        / "opt"
        / "streetsmart-hermes"
        / "robie-job-engine"
        / "data"
        / "ascend-api"
        / "unmatched-digest-installed"
    )


def _health(marker: Path) -> dict:
    with mock.patch.object(digest, "timer_is_enabled", return_value=False):
        with mock.patch.dict(os.environ, {digest.MARKER_ENV: str(marker)}):
            return digest.evaluate_digest_health(
                marker.parent / "missing-state.json",
                now=LATE_WEEKDAY,
            )


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class InstallAscendUnmatchedDigestTests(unittest.TestCase):
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
                if [[ "$1" == "start" && "$2" == "robie-ascend-unmatched-digest.service" ]]; then
                  live="$STUB_PREFIX/etc/systemd/system/robie-ascend-unmatched-digest.service.d/30-live.conf"
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
        _stub(analyze, "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$STUB_LOG\"\nexit 0\n")
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

    def test_dry_run_once_starts_without_the_live_drop_in(self):
        with self._tmpdir() as raw:
            prefix = Path(raw)
            log = prefix / "stub.log"
            result = self._run(prefix, log, "--dry-run-once")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            text = log.read_text(encoding="utf-8")
            self.assertIn("DRY_AT_START", text)
            self.assertNotIn("LIVE_PRESENT_AT_START", text)
            self.assertNotIn("enable --now robie-ascend-unmatched-digest.timer", text)
            self.assertIn("TIMER_NOT_ENABLED", result.stdout)
            unit = (prefix / "etc" / "systemd" / "system" / UNIT).read_text(encoding="utf-8")
            self.assertIn("WorkingDirectory=/", unit)
            self.assertIn("/opt/streetsmart-hermes/venv/bin/python", unit)
            self.assertIn("User=streetsmart-hermes", unit)
            self.assertNotIn("ASCEND_UNMATCHED_DIGEST_LIVE=", unit)
            timer = (prefix / "etc" / "systemd" / "system" / TIMER).read_text(encoding="utf-8")
            self.assertIn("08:30:00 America/New_York", timer)
            live = prefix / "etc" / "systemd" / "system" / f"{UNIT}.d" / "30-live.conf"
            self.assertFalse(live.exists())
            marker = _marker(prefix)
            self.assertFalse(marker.exists())
            self.assertNotIn("MARKER_WRITTEN", result.stdout)
            quiet = _health(marker)
            self.assertTrue(quiet["ok"])
            self.assertFalse(quiet["installed"])
            self.assertIn("not installed", quiet["detail"])

    def test_enable_timer_is_a_separate_step_without_live_mail(self):
        with self._tmpdir() as raw:
            prefix = Path(raw)
            log = prefix / "stub.log"
            result = self._run(prefix, log, "--enable-timer")
            text = log.read_text(encoding="utf-8")
            live = prefix / "etc" / "systemd" / "system" / f"{UNIT}.d" / "30-live.conf"
            marker = _marker(prefix)
            mode = stat.S_IMODE(marker.stat().st_mode)
            folder_mode = stat.S_IMODE(marker.parent.stat().st_mode)
            watched = _health(marker)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("enable --now robie-ascend-unmatched-digest.timer", text)
        self.assertNotIn("DRY_AT_START", text)
        self.assertFalse(live.exists())
        self.assertIn("LIVE=0", result.stdout)
        self.assertIn("MARKER_WRITTEN", result.stdout)
        self.assertEqual(mode, 0o644)
        self.assertEqual(folder_mode, 0o755)
        self.assertFalse(watched["ok"])
        self.assertTrue(watched["installed"])
        self.assertIn("did not run today", watched["detail"])

    def test_live_installs_the_drop_in_after_the_dry_start(self):
        with self._tmpdir() as raw:
            prefix = Path(raw)
            log = prefix / "stub.log"
            result = self._run(prefix, log, "--live")
            text = log.read_text(encoding="utf-8")
            live = prefix / "etc" / "systemd" / "system" / f"{UNIT}.d" / "30-live.conf"
            live_text = live.read_text(encoding="utf-8")
            marker = _marker(prefix)
            self.assertTrue(marker.is_file())
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o644)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("DRY_AT_START", text)
        self.assertLess(text.index("DRY_AT_START"), text.index("enable --now"))
        self.assertIn("ASCEND_UNMATCHED_DIGEST_LIVE=1", live_text)
        self.assertIn("LIVE=1", result.stdout)

    def test_rollback_restores_the_previous_unit_and_stops_the_timer(self):
        with self._tmpdir() as raw:
            prefix = Path(raw)
            etc = prefix / "etc" / "systemd" / "system"
            etc.mkdir(parents=True)
            (etc / UNIT).write_text("OLD-UNIT\n", encoding="utf-8")
            (etc / TIMER).write_text("OLD-TIMER\n", encoding="utf-8")
            log = prefix / "stub.log"
            installed = self._run(prefix, log, "--dry-run-once")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            backup = [
                line.split("=", 1)[1].strip()
                for line in installed.stderr.splitlines()
                if line.startswith("BACKUP=")
            ][-1]
            rolled = subprocess.run(
                [
                    "bash",
                    str(INSTALLER),
                    "--prefix",
                    str(prefix),
                    "--systemctl",
                    str(prefix / "systemctl"),
                    "--systemd-analyze",
                    str(prefix / "systemd-analyze"),
                    "--rollback",
                    backup,
                ],
                check=False,
                env={**os.environ, "STUB_LOG": str(log), "STUB_PREFIX": str(prefix)},
                text=True,
                capture_output=True,
            )
            restored = (etc / UNIT).read_text(encoding="utf-8")
            log_text = log.read_text(encoding="utf-8")
        self.assertEqual(rolled.returncode, 0, rolled.stderr + rolled.stdout)
        self.assertEqual(restored, "OLD-UNIT\n")
        self.assertIn(f"disable --now {TIMER}", log_text)
        self.assertLess(
            rolled.stdout.index("STEP disable --now"),
            rolled.stdout.index("STEP replace unit files"),
        )

    def test_rollback_removes_the_marker_and_health_stays_quiet(self):
        with self._tmpdir() as raw:
            prefix = Path(raw)
            log = prefix / "stub.log"
            installed = self._run(prefix, log, "--enable-timer")
            self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
            marker = _marker(prefix)
            self.assertTrue(marker.is_file())
            backup = [
                line.split("=", 1)[1].strip()
                for line in installed.stderr.splitlines()
                if line.startswith("BACKUP=")
            ][-1]
            rolled = subprocess.run(
                [
                    "bash",
                    str(INSTALLER),
                    "--prefix",
                    str(prefix),
                    "--systemctl",
                    str(prefix / "systemctl"),
                    "--systemd-analyze",
                    str(prefix / "systemd-analyze"),
                    "--rollback",
                    backup,
                ],
                check=False,
                env={**os.environ, "STUB_LOG": str(log), "STUB_PREFIX": str(prefix)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(rolled.returncode, 0, rolled.stderr + rolled.stdout)
            self.assertIn("MARKER_REMOVED", rolled.stdout)
            self.assertFalse(marker.exists())
            quiet = _health(marker)
        self.assertTrue(quiet["ok"])
        self.assertFalse(quiet["installed"])
        self.assertIn("not installed", quiet["detail"])

    def _tmpdir(self):
        import tempfile

        return tempfile.TemporaryDirectory()
