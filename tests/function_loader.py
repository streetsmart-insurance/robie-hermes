from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from typing import Any

from durable_temp import durable_temporary_directory


def load_function_tests(namespace: dict[str, Any]) -> unittest.TestSuite:
    """Adapt pytest-style functions with an optional tmp_path for unittest."""
    suite = unittest.TestSuite()
    for name, test in sorted(namespace.items()):
        if not name.startswith("test_") or not callable(test):
            continue

        def run(case=test):
            parameters = inspect.signature(case).parameters
            if "tmp_path" in parameters:
                with durable_temporary_directory() as temp_dir:
                    case(Path(temp_dir))
            else:
                case()

        suite.addTest(unittest.FunctionTestCase(run, description=name))
    return suite
