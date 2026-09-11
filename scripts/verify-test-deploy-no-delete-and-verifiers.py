#!/usr/bin/env python3
"""Post-deploy probe: prove the live Test release has the cardinal
no-delete guard and the Chat verifier wiring.

Run ON hermes-test-01 (via Cloud Shell) after the Test deploy workflow,
pointed at the live release — not at a checkout. Replace <rel> with the
12-character release from the deploy evidence:

    REL=<rel>
    sudo PYTHONPATH=/opt/streetsmart-hermes-test/releases/$REL/robie-hermes-$REL \
      python3 /opt/streetsmart-hermes-test/releases/$REL/robie-hermes-$REL/\
scripts/verify-test-deploy-no-delete-and-verifiers.py

It imports the guard and verifier registry from the DEPLOYED tree
(via PYTHONPATH), so a stale pointer flip fails loudly instead of
looking live. Exits 0 only when every check passes; exits 2 on any
failure. Prints PASS/FAIL per check. No browser, no network, no secrets.
"""

from __future__ import annotations

import sys
import traceback


def check(name, fn):
    try:
        fn()
    except Exception as exc:
        print(f"FAIL {name}: {exc}")
        traceback.print_exc(limit=3)
        return False
    print(f"PASS {name}")
    return True


class _FakeLocator:
    def __init__(self, text=""):
        self._text = text

    def get_attribute(self, name):
        return None

    def inner_text(self):
        return self._text

    def text_content(self):
        return self._text


def t_no_delete_click():
    from robie_job_engine.playwright_write_guard import (
        destructive_action_block_reason,
    )

    reason = destructive_action_block_reason(
        _FakeLocator("Delete policy"), method_name="click"
    )
    assert reason and "never deletes" in reason, "delete-policy click not refused"


def t_no_delete_cancel():
    from robie_job_engine.playwright_write_guard import (
        destructive_action_block_reason,
    )

    reason = destructive_action_block_reason(
        _FakeLocator("Cancel policy"), method_name="click"
    )
    assert reason and "never deletes" in reason, "cancel-policy click not refused"


def t_no_delete_press():
    from robie_job_engine.playwright_write_guard import (
        destructive_action_block_reason,
    )

    reason = destructive_action_block_reason(
        _FakeLocator("Delete policy"), method_name="press", key="Delete"
    )
    assert reason and "never deletes" in reason, "delete-key press not refused"


def t_benign_click_allowed():
    from robie_job_engine.playwright_write_guard import (
        destructive_action_block_reason,
    )

    assert (
        destructive_action_block_reason(
            _FakeLocator("Save changes"), method_name="click"
        )
        is None
    ), "benign click was refused"


def t_chat_verifier_wired():
    from robie_job_engine.browser_read import BrowserReadVerifier
    from robie_job_engine.chat_guard import _default_chat_verifiers

    verifiers = _default_chat_verifiers()
    assert "browser.read" in verifiers, "browser.read verifier not registered"
    assert isinstance(verifiers["browser.read"], BrowserReadVerifier), (
        "browser.read verifier is not a BrowserReadVerifier"
    )
    from robie_job_engine.chat_ezlynx_destination_verifier import (
        HermesChatEzlynxDestinationVerifier,
    )

    assert "hermes.google_chat_task" in verifiers, (
        "hermes.google_chat_task verifier not registered"
    )
    assert isinstance(
        verifiers["hermes.google_chat_task"], HermesChatEzlynxDestinationVerifier
    ), "hermes.google_chat_task verifier is not HermesChatEzlynxDestinationVerifier"


def t_hitl_trigger_catches_raw_guard_error():
    from robie_job_engine.hitl import structured_blocker_reason

    reason = structured_blocker_reason(
        "PLAYWRIGHT_BLOCKED: write target matched 3 fields; refuse to guess"
    )
    assert reason and "matched 3 fields" in reason, (
        "bare PLAYWRIGHT_BLOCKED line does not trigger HITL"
    )


def main():
    ok = True
    ok &= check("no-delete click refused", t_no_delete_click)
    ok &= check("no-delete cancel-policy refused", t_no_delete_cancel)
    ok &= check("no-delete Delete-key press refused", t_no_delete_press)
    ok &= check("benign click allowed", t_benign_click_allowed)
    ok &= check("browser.read verifier registered", t_chat_verifier_wired)
    ok &= check("HITL trigger catches raw guard error", t_hitl_trigger_catches_raw_guard_error)
    print("ALL PASS" if ok else "PROBE FAILED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
