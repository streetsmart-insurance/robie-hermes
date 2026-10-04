"""Prod and Test intake wiring for the Bland call path.

ROBIE_PHONE_LIVE_CALLS defaults off, so the worker dry-runs. Nothing here
opens a socket or reads a secret unless a caller passes a reader and turns
the live flag on.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Callable, Optional

from .bland_call_port import BlandTransportCallPort
from .ezlynx_applicant_phone import EzlynxApplicantPhoneLookup
from .ringcentral_transfer_lookup import RingCentralTransferLookup, run_live_check

ApplicantFetch = Callable[[str], Mapping[str, Any] | None]


def live_calls_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("ROBIE_PHONE_LIVE_CALLS") == "1"


def build_call_dependencies(
    *,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
    fetch_applicant: ApplicantFetch | None = None,
    secret_reader: Callable[[str], str] | None = None,
    urlopen: Callable[..., Any] | None = None,
    fetch_ringcentral: Callable[[], list] | None = None,
) -> tuple[EzlynxApplicantPhoneLookup, BlandTransportCallPort, RingCentralTransferLookup, bool]:
    """Phone lookup, Bland port, transfer lookup, and the worker dry-run flag.

    The dry-run flag is True unless ROBIE_PHONE_LIVE_CALLS=1. The Bland
    port's execute flag matches that, so a default process cannot dial.
    """
    source = os.environ if env is None else env
    live = live_calls_enabled(source)
    phone = EzlynxApplicantPhoneLookup(fetch_applicant or (lambda _applicant_id: None))
    bland = BlandTransportCallPort(
        env=source,
        hostname=hostname,
        secret_reader=secret_reader,
        urlopen=urlopen,
        execute=live,
    )
    transfer = RingCentralTransferLookup()
    if fetch_ringcentral is not None:
        run_live_check(
            transfer._directory,
            env=source,
            secret_reader=secret_reader,
            fetch_records=fetch_ringcentral,
        )
    return phone, bland, transfer, not live
