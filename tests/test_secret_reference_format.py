"""Every secret reference a workflow writes must be a full resource name.

secret_manager.GoogleSecretManagerAccessor.access() refuses anything that is
not projects/.../secrets/.../versions/...:

    if not resource_name.startswith("projects/") or "/versions/" not in resource_name:
        raise ValueError("Secret Manager reference must be a full secret-version resource name")

That validation is right - a bare name is ambiguous about the project. But two
workflows wrote these references with two different conventions:
configure-ezlynx-api-env.yml used full resource names, and
configure-production-workers.yml wrote bare 'ezlynx-username'. Anything loading
the bare one raised ValueError before doing any work, which is the Secret
Manager failure that has been killing manual_renewal_verification.

Nothing caught it because the two files were never compared. This does.
Stdlib unittest: runs without pytest.
"""
import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# ROBIE_..._SECRET=<value>, as written inside a shell quoted literal.
_ASSIGNMENT = re.compile(r"(ROBIE_[A-Z0-9_]*_SECRET)=([^'\"\s\\]+)")


def _looks_like_a_full_resource_name(value: str) -> bool:
    return value.startswith("projects/") and "/versions/" in value


def _is_a_shell_expansion(value: str) -> bool:
    # ${PROJECT_ID} and friends are resolved at run time; we cannot judge them
    # statically, and refusing them would push people back to bare names.
    return "$" in value


class SecretReferencesAreFullResourceNames(unittest.TestCase):
    def test_workflows_directory_exists(self):
        self.assertTrue(WORKFLOWS.is_dir(), f"missing {WORKFLOWS}")

    def test_every_written_secret_reference_is_a_full_resource_name(self):
        offenders: list[str] = []
        checked = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                # Only assignments, not the sed lines that delete them.
                if "sed -i" in line:
                    continue
                for name, value in _ASSIGNMENT.findall(line):
                    checked += 1
                    if _is_a_shell_expansion(value):
                        continue
                    if not _looks_like_a_full_resource_name(value):
                        offenders.append(
                            f"{path.name}:{line_no} {name}={value!r} is not a "
                            "projects/.../secrets/.../versions/... resource name; "
                            "GoogleSecretManagerAccessor.access() raises ValueError on it"
                        )
        self.assertTrue(checked, "parsed no ROBIE_*_SECRET assignments — has the shape changed?")
        self.assertEqual(offenders, [], "\n  " + "\n  ".join(offenders))

    def test_the_validator_still_rejects_a_bare_name(self):
        # If this ever stops raising, the test above is guarding nothing.
        from robie_job_engine.secret_manager import GoogleSecretManagerAccessor

        accessor = GoogleSecretManagerAccessor(client=object())
        with self.assertRaises(ValueError):
            accessor.access("ezlynx-username")

    def test_the_validator_accepts_a_full_resource_name_shape(self):
        from robie_job_engine.secret_manager import GoogleSecretManagerAccessor

        class _Client:
            def access_secret_version(self, request):
                class _R:
                    class payload:
                        data = b"value"
                return _R()

        accessor = GoogleSecretManagerAccessor(client=_Client())
        self.assertEqual(
            accessor.access("projects/1/secrets/ezlynx-username/versions/latest"), "value"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
