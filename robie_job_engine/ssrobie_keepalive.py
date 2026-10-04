"""Compatibility entrypoint for the Test keepalive unit.

``robie-ezlynx-keepalive-test.service`` used to execute this module. The
shared implementation is :mod:`robie_job_engine.ezlynx_keepalive`. A unit
file that still runs ``python -m robie_job_engine.ssrobie_keepalive`` gets
the TEST profile and the same gate, skip, and no-login behavior.

``robie_job_engine.session_preflight`` is the CDP tab classifier both
entrypoints use. It ships next to this file.
"""

from __future__ import annotations

import os


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("ROBIE_ENV", "TEST")
    from .ezlynx_keepalive import main as shared_main

    return shared_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
