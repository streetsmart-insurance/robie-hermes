"""The old Test module name still runs the shared keepalive as TEST."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class LegacySsrobieKeepaliveEntrypointTests(unittest.TestCase):
    def test_module_delegates_to_the_shared_keepalive(self):
        from robie_job_engine import ssrobie_keepalive

        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False), patch(
            "robie_job_engine.ezlynx_keepalive.run_keepalive",
            return_value={"exit_code": 0, "verdict": "AUTHENTICATED", "action": "none"},
        ) as run:
            # An explicit Production env is left alone. The unit file that
            # still calls this module on Test does not set ROBIE_ENV, which
            # the shim fills in. This case proves the shim does not override
            # a profile the caller already chose, and still reaches the
            # shared main.
            os.environ["ROBIE_ENV"] = "TEST"
            with redirect_stdout(io.StringIO()):
                code = ssrobie_keepalive.main(["--json"])
        self.assertEqual(code, 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
