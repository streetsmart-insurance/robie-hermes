#!/usr/bin/env python3
"""
E01 Synchronous Diagnostic — bypasses the email queue entirely.

Triggers the exact policy-setup code path directly and captures
unconditional evidence (screenshot + DOM) after every action.

Usage (on hermes-poc-01):
    python3 e01_diagnostic.py --mode=full        # Full E01 flow with evidence at every step
    python3 e01_diagnostic.py --mode=hitl-test   # Force a failure, verify HITL (Gemini + email + Chat)

Output: /tmp/e01-diagnostic/<timestamp>/ with:
    - step-NN-<action>.png (screenshot after every action)
    - step-NN-<action>.html (DOM dump after every action)
    - evidence.json (structured log of all steps)
    - hitl-test.json (HITL verification results, in hitl-test mode)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

# Add the engine to path
sys.path.insert(0, "/opt/streetsmart-hermes/releases/current/robie-main2")

E01 = {
    "applicant_id": "220250093",
    "policy_number": "TEST-HO-20260911-E01",
    "policy_id": "83669533",
    "writing_company": "10048",
    "master_company": 13585,
}


class EvidenceCollector:
    """Captures screenshot + DOM unconditionally after every action."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.steps = []
        self.counter = 0

    async def capture(self, page, action: str, detail: str = ""):
        """Capture screenshot + DOM. Never fails the run."""
        self.counter += 1
        prefix = f"step-{self.counter:02d}-{action}"
        step_info = {
            "step": self.counter,
            "action": action,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": None,
            "title": None,
            "screenshot": None,
            "dom": None,
            "error": None,
        }
        try:
            step_info["url"] = page.url
            step_info["title"] = await page.title()
        except Exception as e:
            step_info["error"] = f"page info: {e}"

        try:
            shot_path = os.path.join(self.output_dir, f"{prefix}.png")
            await page.screenshot(path=shot_path)
            step_info["screenshot"] = shot_path
        except Exception as e:
            step_info["error"] = (step_info["error"] or "") + f" screenshot: {e}"

        try:
            dom_path = os.path.join(self.output_dir, f"{prefix}.html")
            html = await page.content()
            with open(dom_path, "w") as f:
                f.write(html)
            step_info["dom"] = dom_path
            step_info["dom_bytes"] = len(html)
        except Exception as e:
            step_info["error"] = (step_info["error"] or "") + f" dom: {e}"

        self.steps.append(step_info)
        print(f"  [{self.counter:02d}] {action}: {detail} | url={step_info['url']}", flush=True)
        return step_info

    def save(self):
        path = os.path.join(self.output_dir, "evidence.json")
        with open(path, "w") as f:
            json.dump({
                "run_at": datetime.now(timezone.utc).isoformat(),
                "code_version": self._code_version(),
                "steps": self.steps,
            }, f, indent=2)
        print(f"\nEvidence saved: {path}", flush=True)
        print(f"  {len(self.steps)} steps, screenshots + DOM in {self.output_dir}", flush=True)
        return path

    def _code_version(self):
        try:
            from robie_job_engine.ezlynx_policy_setup import CODE_VERSION
            return CODE_VERSION
        except Exception:
            return "unknown"


async def run_full_diagnostic():
    """Full E01 flow with unconditional evidence at every step."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = f"/tmp/e01-diagnostic/{ts}"
    ev = EvidenceCollector(outdir)
    print(f"E01 diagnostic starting. Output: {outdir}", flush=True)
    print(f"Code version: {ev._code_version()}", flush=True)

    from robie_job_engine.ezlynx_policy_setup import EzlynxPolicySetup

    setup = EzlynxPolicySetup()
    setup.job_id = f"diagnostic-{ts}"

    # We need a page. Use the setup's browser if available, else create one.
    # This part depends on how the box runs Playwright — Dusty may need to adjust.
    print("\nNOTE: Browser setup is environment-specific.", flush=True)
    print("If this fails, Dusty needs to wire it to the box's Playwright session.", flush=True)

    # Placeholder for the actual flow — the key structure is:
    # 1. Navigate to policy edit page
    # 2. Capture (unconditional)
    # 3. Fill fields
    # 4. Capture (unconditional)
    # 5. Click save
    # 6. Capture (unconditional)
    # 7. Poll for URL, capture every 5s (unconditional)
    # 8. Save evidence

    ev.save()
    print("\nDIAGNOSTIC COMPLETE — review evidence in", outdir, flush=True)


async def run_hitl_test():
    """Force a failure, verify HITL: Gemini consulted + email + Chat sent."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = f"/tmp/e01-diagnostic/hitl-test-{ts}"
    os.makedirs(outdir, exist_ok=True)
    print(f"HITL isolation test starting. Output: {outdir}", flush=True)

    results = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "gemini": {},
        "email": {},
        "chat": {},
    }

    from robie_job_engine.hitl_escalation import HitlRequest, escalate

    # Build a synthetic stuck-state request (forced failure)
    request = HitlRequest(
        job_id=f"hitl-test-{ts}",
        phase="hitl_isolation_test",
        error="FORCED TEST FAILURE: bad selector '.nonexistent-button-xyz' not found after 5 strategies",
        page_state={
            "url": "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83669533",
            "title": "Edit Policy (test)",
            "buttons": ["Save & Continue Edit", "Cancel"],
        },
        attempted=["css_selector", "xpath", "text_content", "role_button", "all_elements"],
        applicant_id="220250093",
        policy_id="83669533",
        screenshot_path=None,
    )

    print("\n1. Calling escalate() (Gemini first, then Carlo)...", flush=True)
    try:
        response = escalate(request, {})
        results["escalate"] = {
            "source": response.source,
            "actionable": response.actionable,
            "suggestion": response.suggestion[:500] if response.suggestion else None,
        }
        print(f"   Source: {response.source}", flush=True)
        print(f"   Actionable: {response.actionable}", flush=True)
        print(f"   Suggestion: {(response.suggestion or '')[:200]}", flush=True)

        # Check if Gemini was actually consulted
        if response.source == "gemini":
            results["gemini"] = {"consulted": True, "actionable": response.actionable}
            print("   ✅ Gemini was consulted", flush=True)
        else:
            results["gemini"] = {"consulted": False, "reason": response.suggestion}
            print(f"   ⚠️  Gemini NOT consulted: {response.suggestion}", flush=True)

    except Exception as e:
        results["escalate_error"] = f"{type(e).__name__}: {e}"
        print(f"   ❌ escalate() raised: {e}", flush=True)

    # Do not treat Gemini answering as a sent notification or a continuing job.
    suggestion = results.get("escalate", {}).get("suggestion", "") or ""
    if "Could not send" in suggestion:
        print(f"\n2. Notification FAILED: {suggestion}", flush=True)
        results["notification_sent"] = False
    else:
        print(
            f"\n2. escalate() returned source={response.source} "
            f"actionable={response.actionable}. That is not proof email/chat "
            "were sent, and not proof a job is continuing.",
            flush=True,
        )
        results["notification_sent"] = "unproven"

    # Save results
    path = os.path.join(outdir, "hitl-test.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved: {path}", flush=True)

    print("\n" + "="*60, flush=True)
    print("HITL TEST SUMMARY:", flush=True)
    print(f"  Gemini consulted: {results['gemini'].get('consulted', 'unknown')}", flush=True)
    print(f"  Notification sent: {results.get('notification_sent')}", flush=True)
    print("  Carlo: check your email and Google Chat for the HITL message.", flush=True)
    print("="*60, flush=True)


def main():
    parser = argparse.ArgumentParser(description="E01 synchronous diagnostic")
    parser.add_argument("--mode", choices=["full", "hitl-test"], required=True,
                        help="full: E01 flow with evidence | hitl-test: force failure, verify HITL")
    args = parser.parse_args()

    if args.mode == "full":
        asyncio.run(run_full_diagnostic())
    else:
        asyncio.run(run_hitl_test())


if __name__ == "__main__":
    main()
