"""CODE_VERSION must match the loaded release, not a leftover hardcoded SHA.

On hermes-poc-01 the health check loaded 712cdb0a (PR 370) while the live
release dir was c166a9bbb8b7 (PR 375). A later zip must not be able to lie.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from robie_job_engine import ezlynx_policy_setup
from robie_job_engine.code_version import (
    UNKNOWN_CODE_VERSION,
    resolve_code_version,
    sha_from_release_path,
)


STALE_PR370 = "712cdb0a4af2dafdfa38cb5406084b5279fd5f60"
LIVE_375_DIR = "c166a9bbb8b7"
NEWER_DIR = "deadbeefcafe"


def _health_check_would_pass(loaded: str, release_target: str) -> bool:
    """Same rule as scripts/robie_health_check.py on the box."""
    return bool(loaded) and loaded[:8] in release_target


class CodeVersionHonestyTests(unittest.TestCase):
    def test_release_path_sha_comes_from_the_dir_not_a_constant(self) -> None:
        target = (
            f"/opt/streetsmart-hermes/releases/{LIVE_375_DIR}/"
            f"robie-hermes-{LIVE_375_DIR}"
        )
        self.assertEqual(sha_from_release_path(target), LIVE_375_DIR)
        self.assertEqual(
            sha_from_release_path(f"{target}/robie_job_engine/ezlynx_policy_setup.py"),
            LIVE_375_DIR,
        )
        self.assertIsNone(sha_from_release_path("/workspace/robie_job_engine/ezlynx_policy_setup.py"))

    def test_loaded_from_375_release_dir_does_not_report_370(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = (
                Path(tmp)
                / "releases"
                / LIVE_375_DIR
                / f"robie-hermes-{LIVE_375_DIR}"
                / "robie_job_engine"
            )
            release.mkdir(parents=True)
            source = release / "ezlynx_policy_setup.py"
            source.write_text("# loaded copy\n", encoding="utf-8")
            version = resolve_code_version(
                source_file=source,
                env={},
                pointer_paths=(),
                run_git=lambda _cwd: STALE_PR370,
            )
        self.assertEqual(version, LIVE_375_DIR)
        self.assertNotEqual(version, STALE_PR370)
        self.assertFalse(version.startswith("712cdb0a"))
        target = (
            f"/opt/streetsmart-hermes/releases/{LIVE_375_DIR}/"
            f"robie-hermes-{LIVE_375_DIR}"
        )
        self.assertTrue(_health_check_would_pass(version, target))
        self.assertFalse(_health_check_would_pass(STALE_PR370, target))

    def test_later_zip_reports_its_own_release_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = (
                Path(tmp)
                / "releases"
                / NEWER_DIR
                / f"robie-hermes-{NEWER_DIR}"
                / "robie_job_engine"
            )
            release.mkdir(parents=True)
            source = release / "ezlynx_policy_setup.py"
            source.write_text("# next zip\n", encoding="utf-8")
            version = resolve_code_version(
                source_file=source,
                env={},
                pointer_paths=(),
                run_git=lambda _cwd: STALE_PR370,
            )
        self.assertEqual(version, NEWER_DIR)
        self.assertNotIn(LIVE_375_DIR, version)
        self.assertNotIn("712cdb0a", version)
        target = (
            f"/opt/streetsmart-hermes/releases/{NEWER_DIR}/"
            f"robie-hermes-{NEWER_DIR}"
        )
        self.assertTrue(_health_check_would_pass(version, target))

    def test_current_pointer_is_used_when_file_path_has_no_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / LIVE_375_DIR / f"robie-hermes-{LIVE_375_DIR}"
            release.mkdir(parents=True)
            current = root / "releases" / "current"
            current.symlink_to(release)
            flat = root / "flat-copy" / "ezlynx_policy_setup.py"
            flat.parent.mkdir(parents=True)
            flat.write_text("# flattened tree\n", encoding="utf-8")
            version = resolve_code_version(
                source_file=flat,
                env={"ROBIE_CANONICAL_JOB_ENGINE_ROOT": str(current)},
                pointer_paths=(),
                run_git=lambda _cwd: STALE_PR370,
            )
        self.assertEqual(version, LIVE_375_DIR)
        self.assertTrue(
            _health_check_would_pass(
                version,
                f"/opt/streetsmart-hermes/releases/{LIVE_375_DIR}/"
                f"robie-hermes-{LIVE_375_DIR}",
            )
        )

    def test_git_head_used_when_no_release_path_or_pointer(self) -> None:
        version = resolve_code_version(
            source_file=Path("/tmp/checkout/robie_job_engine/ezlynx_policy_setup.py"),
            env={},
            pointer_paths=(),
            run_git=lambda _cwd: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(version, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_unknown_when_nothing_can_be_derived(self) -> None:
        version = resolve_code_version(
            source_file=Path("/tmp/checkout/robie_job_engine/ezlynx_policy_setup.py"),
            env={},
            pointer_paths=(),
            run_git=lambda _cwd: None,
        )
        self.assertEqual(version, UNKNOWN_CODE_VERSION)
        self.assertNotEqual(version, STALE_PR370)

    def test_policy_setup_has_no_hardcoded_sha_constant(self) -> None:
        source = Path(ezlynx_policy_setup.__file__).read_text(encoding="utf-8")
        self.assertNotIn(STALE_PR370, source)
        self.assertIsNone(
            re.search(r'CODE_VERSION\s*=\s*"[0-9a-f]{7,40}"', source),
            "CODE_VERSION must be derived, not a hand-edited SHA",
        )
        self.assertIn("resolve_code_version", source)
        self.assertTrue(ezlynx_policy_setup.CODE_VERSION)
        self.assertNotEqual(ezlynx_policy_setup.CODE_VERSION, STALE_PR370)
        self.assertFalse(ezlynx_policy_setup.CODE_VERSION.startswith("712cdb0a"))


if __name__ == "__main__":
    unittest.main()
