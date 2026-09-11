"""A verifier that fails to construct must still name itself in the job's error.

Before this, a failed import left the slot empty and engine._verify reported
"no independent verifier registered" — indistinguishable from an action type
nobody ever wrote a verifier for. Stdlib unittest: runs without pytest.
"""
import unittest
from unittest import mock

from robie_job_engine import chat_guard
from robie_job_engine.chat_guard import _UnavailableVerifier, _default_chat_verifiers


class RegistrationFailureIsNamed(unittest.TestCase):
    def test_happy_path_registers_the_real_verifier(self):
        verifiers = _default_chat_verifiers()
        self.assertIn("hermes.google_chat_task", verifiers)
        self.assertNotIsInstance(verifiers["hermes.google_chat_task"], _UnavailableVerifier)
        self.assertIn("browser.read", verifiers)
        self.assertNotIsInstance(verifiers["browser.read"], _UnavailableVerifier)

    def test_import_failure_registers_a_placeholder_instead_of_nothing(self):
        real_import = __import__

        def boom(name, *args, **kwargs):
            if "chat_ezlynx_destination_verifier" in name:
                raise ImportError("simulated missing module")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=boom):
            verifiers = _default_chat_verifiers()
        # The slot is occupied, not empty.
        self.assertIn("hermes.google_chat_task", verifiers)
        self.assertIsInstance(verifiers["hermes.google_chat_task"], _UnavailableVerifier)

    def test_placeholder_raises_with_the_real_reason(self):
        v = _UnavailableVerifier("hermes.google_chat_task", "ImportError: simulated missing module")
        with self.assertRaises(RuntimeError) as ctx:
            v.verify({"id": "j1"}, {})
        msg = str(ctx.exception)
        self.assertIn("hermes.google_chat_task", msg)
        self.assertIn("failed to register at startup", msg)
        self.assertIn("simulated missing module", msg)
        # The point: it does NOT read like a missing verifier.
        self.assertNotIn("no independent verifier registered", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
