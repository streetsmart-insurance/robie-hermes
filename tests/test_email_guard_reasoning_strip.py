"""Regression tests for the outbound email reasoning sanitizer.

2026-09-20: the hermes email worker resumed mid-response and dumped its
private reasoning into the worker response under bold headings like
"**Continuing Thought Process**" / "**Analyzing Interrupted Process**",
followed by first-person process narration ("I am currently ...").
run_guarded_email_task() embedded that raw text in the reply email sent to
Carlo (Gmail 1a0c208e8a1432c3). These tests pin the fix: internal reasoning
sections are stripped before the reply is composed, and anything that still
looks like a thought-process leak fails closed to a fixed safe notice.
"""

import importlib.util
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_email_guard():
    """Load robie_job_engine/email_guard.py with its heavy sibling imports stubbed.

    The module under test only needs `re` for the sanitizer; stubbing the
    relative imports keeps this test independent of the rest of the engine.

    The load is fully isolated: it runs under a throwaway module name and
    restores sys.modules afterwards, so it never replaces or shadows the real
    robie_job_engine modules for other tests in the same process.
    """
    saved_modules = dict(sys.modules)
    try:
        pkg_name = "robie_job_engine"
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = []
            sys.modules[pkg_name] = pkg
        stubs = {
            "engine": ["JobEngine"],
            "models": ["ACTION_OUTCOME_UNKNOWN", "JobStatus", "WorkerResult"],
            "store": ["JobStore"],
            "chat_policy": ["SECURITY_GUARD_STOP_RULE"],
            "skill_sync": ["add_synced_context", "submission_center_sop_url"],
        }
        for mod, attrs in stubs.items():
            full = f"{pkg_name}.{mod}"
            if full not in sys.modules:
                stub = types.ModuleType(full)
                for attr in attrs:
                    setattr(stub, attr, object())
                sys.modules[full] = stub
        path = os.path.join(
            os.path.dirname(__file__), "..", "robie_job_engine", "email_guard.py"
        )
        # Throwaway name under the package: relative imports still resolve,
        # but the real robie_job_engine.email_guard entry is never replaced.
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}._reasoning_strip_test_isolated", path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore sys.modules exactly: drop everything added, revive removed.
        for key in list(sys.modules):
            if key not in saved_modules:
                del sys.modules[key]
        sys.modules.update(saved_modules)


_guard = _load_email_guard()
strip = _guard._strip_internal_reasoning

# Mirrors the shape of the 2026-09-20 leak (Gmail 1a0c208e8a1432c3).
LEAKED_RESPONSE = """**Continuing Thought Process**

I am currently processing the system's directive to resume mid-response, which explicitly asks me to continue the same original email job and its existing account scope.
**Analyzing Interrupted Process**

I am currently focused on deciphering the original task from the fragmented input at hand.
**Integrating API for File Updates**

I'm starting by integrating the API for file updates.
**Clarifying Workflow Steps**

My goal is to clarify the workflow steps for the current task.
**Investigating Zapier Alert**

I'm investigating the Zapier alert that was forwarded.
**Reviewing Zapier Alert**

My focus is reviewing the Zapier alert details now.
**Diagnosing Zap Failure**

I am currently diagnosing the Zap failure and its root cause.
**Investigating Zap Failure**

My current focus is investigating the Zap failure end to end.

Hey Carlo,

I reviewed the forwarded Zapier alert for the Inbox Triage Zap. The failure is on the EZLynx step and the applicant ID looks stale. I'll keep the message unread until we confirm the right applicant.

— Robie"""


def test_leaked_reasoning_sections_are_stripped():
    result = strip(LEAKED_RESPONSE)
    lowered = result.lower()
    assert "thought process" not in lowered
    assert "continuing" not in lowered
    assert "**analyzing" not in lowered
    assert "**investigating" not in lowered
    assert "**diagnosing" not in lowered
    assert "**integrating" not in lowered
    assert "**clarifying" not in lowered
    assert "**reviewing" not in lowered
    # The user-facing reply survives intact.
    assert "Hey Carlo," in result
    assert "I reviewed the forwarded Zapier alert" in result


def test_clean_response_passes_through_unchanged():
    clean = "Hey Carlo,\n\nThe task is done. Everything verified.\n\n— Robie"
    assert strip(clean) == clean


def test_reasoning_only_response_falls_back_to_safe_notice():
    only_reasoning = (
        "**Continuing Thought Process**\n\n"
        "I am currently processing the system's directive to resume mid-response.\n"
        "**Analyzing Interrupted Process**\n\n"
        "I am currently focused on deciphering the original task."
    )
    result = strip(only_reasoning)
    assert "thought process" not in result.lower()
    assert result == _guard._REASONING_STRIP_FALLBACK


def test_inline_thought_process_marker_fails_closed():
    # No bold headings, but the marker phrase still appears: fail closed.
    sneaky = "Hey Carlo, my thought process here was to check the Zap first."
    result = strip(sneaky)
    assert "thought process" not in result.lower()
    assert result == _guard._REASONING_STRIP_FALLBACK


def test_empty_and_non_string_inputs_pass_through():
    assert strip("") == ""
    assert strip(None) is None
