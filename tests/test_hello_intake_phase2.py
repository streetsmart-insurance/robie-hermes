"""Phase 2 hello@ intake tests: forward unwrap/dedupe, filing spec,
cancellation workflow, premium-finance routing, voicemail workflow,
classifier updates, and preservation of the vendor-alias guard and
portal-fetch behavior.

Read-only: every test uses injected fakes or pure functions. No EZLynx
writes, no Zapier, no email, no network.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.hello_cancellation import (  # noqa: E402
    CancellationLedger,
    build_cancellation_plan,
    build_reinstatement_plan,
    extract_amount_to_cure,
    notice_subtype,
    process_cancellation_notice,
)
from robie_job_engine.hello_classifier import (  # noqa: E402
    GENUINE,
    INTERNAL,
    NOISE,
    ROUTE_FOR_REQUEST_TYPE,
    classify_hello,
)
from robie_job_engine.hello_filing import (  # noqa: E402
    TASK_CALLBACK,
    TASK_CURE_OR_CANCEL,
    build_filing_plan,
    check_note_body,
    execute_plan,
    filing_category_for,
    note_claims_money,
    note_has_money_proof,
    note_is_placeholder,
    spec_for,
    verify_filing_identity,
)
from robie_job_engine.hello_forwarding import (  # noqa: E402
    classify_with_envelope,
    content_hash,
    dedupe_key,
    dedupe_messages,
    detect_forward,
    normalize_subject,
    unwrap_forward,
)
from robie_job_engine.hello_voicemail import (  # noqa: E402
    build_voicemail_plan,
    build_voicemail_summary,
    parse_voicemail_notification,
    sanitize_note_body,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STRONG_IDENTITY = {
    "email_insured": "Hearts For Home Healthcare, LLC",
    "email_policy": "PHPK2734817-000",
    "match": {
        "applicant_id": "applicant-1",
        "applicant_name": "Hearts For Home Healthcare, LLC",
        "confidence": "high",
        "matched_on": "policy_exact",
    },
}

NAME_ONLY_IDENTITY = {
    "email_insured": "Hearts For Home Healthcare, LLC",
    "email_policy": None,
    "match": {
        "applicant_id": "applicant-1",
        "confidence": "high",
        "matched_on": "name_fuzzy",
    },
}


def make_fakes(open_tasks=None):
    """Injected interfaces recording every call; nothing external."""
    calls = {"note": [], "documents": [], "task_list": 0, "task_create": [],
             "alert": []}
    store = {"open_tasks": list(open_tasks or [])}

    def note(applicant_id, body, discussion_hint):
        calls["note"].append((applicant_id, body, discussion_hint))
        return {"status": "filed", "note_id": "note-1",
                "discussion_id": "disc-1", "note_count_after": 6}

    def documents(applicant_id, file_name, file_bytes, description):
        calls["documents"].append((applicant_id, file_name, description))
        return {"document_id": "doc-1", "read_back": "HIT"}

    def list_open(applicant_id):
        calls["task_list"] += 1
        return list(store["open_tasks"])

    def create(applicant_id, task):
        calls["task_create"].append((applicant_id, task))
        return {"task_id": "task-1"}

    def alert(to, message):
        calls["alert"].append((to, message))
        return {"alerted": to}

    interfaces = {
        "note": note,
        "documents": documents,
        "tasks": {"list_open": list_open, "create": create},
        "alert": alert,
    }
    return interfaces, calls


# ===========================================================================
# Step 6 — classifier updates
# ===========================================================================

class TestBillingSplit:
    def test_premium_finance_company(self):
        action, rtype, _ = classify_hello(
            "SETTLEMENT OFFER / Case 13256517 / Capital Premium Financing",
            "Your client has a past due balance with Capital Premium.",
            "collections@capitalpremium.example")
        assert (action, rtype) == (GENUINE, "premium_finance")

    def test_ascend_is_premium_finance(self):
        action, rtype, _ = classify_hello(
            "Ascend: payment failed for policy 12345",
            "The scheduled Ascend payment did not go through.",
            "noreply@useascend.com")
        assert (action, rtype) == (GENUINE, "premium_finance")

    def test_return_premium_is_invoice_billing(self):
        action, rtype, _ = classify_hello(
            "Return premium processed",
            "A return premium of $120.00 was issued for Policy 3AB025200.",
            "billing@carrier.example")
        assert (action, rtype) == (GENUINE, "invoice_billing")

    def test_overdue_carrier_invoice(self):
        action, rtype, _ = classify_hello(
            "Overdue invoice",
            "Invoice #8821 is past due. Amount due $410.22.",
            "billing@carrier.example")
        assert (action, rtype) == (GENUINE, "invoice_billing")

    def test_no_billing_type_remains(self):
        assert "billing" not in ROUTE_FOR_REQUEST_TYPE


class TestWholesalerMga:
    def test_underwriting_requirements(self):
        action, rtype, _ = classify_hello(
            "Underwriting requirements — Policy 99812",
            "The following underwriting requirements are outstanding: "
            "loss runs, driver list.",
            "uw@jjins.com")
        assert (action, rtype) == (GENUINE, "wholesaler_mga")

    def test_wholesaler_sender_fallback(self):
        action, rtype, _ = classify_hello(
            "Policy documents for renewal",
            "Attached are the policy documents you requested.",
            "service@tuscano.com")
        assert (action, rtype) == (GENUINE, "wholesaler_mga")

    def test_renewal_shape_beats_wholesaler_sender(self):
        action, rtype, _ = classify_hello(
            "Renewal Quote — 123 Main St",
            "Your renewal quote is ready for review.",
            "quotes@bridgespecialty.com")
        assert (action, rtype) == (GENUINE, "renewal")

    def test_wholesaler_marketing_is_still_noise(self):
        action, rtype, _ = classify_hello(
            "New products this month!",
            "Check out our new product lineup. Unsubscribe here.",
            "marketing@jjins.com")
        assert action == NOISE


class TestVoicemailTextNotify:
    def test_voicemail_notification_is_genuine(self):
        action, rtype, _ = classify_hello(
            "Voice Mail from Maria Lopez (5551234567)",
            "Transcription: Hi, this is Maria, calling about my policy.",
            "noreply@ringcentral.example")
        assert (action, rtype) == (GENUINE, "voicemail_text_notify")

    def test_text_message_relay_is_genuine(self):
        action, rtype, _ = classify_hello(
            "Re: Do Not Reply - Mununga Kipata has sent you a text message",
            "You have a new text.",
            "noreply@example.com")
        assert (action, rtype) == (GENUINE, "voicemail_text_notify")

    def test_call_analysis_digest_stays_noise(self):
        action, _, _ = classify_hello(
            "Call analysis for 2026-09-26",
            "Conversation digest: the call lasted 4 minutes.",
            "noreply@sonant.example")
        assert action == NOISE

    def test_missed_call_without_message_stays_noise(self):
        action, _, _ = classify_hello(
            "Missed call from 5551234567",
            "You missed a call.",
            "noreply@ringcentral.example")
        assert action == NOISE


class TestBinderBound:
    def test_binder_attached(self):
        action, rtype, _ = classify_hello(
            "Binder attached — effective 10/01/2026",
            "Please find the binder attached for policy 77881.",
            "uw@carrier.example")
        assert (action, rtype) == (GENUINE, "binder_bound")

    def test_policy_bound(self):
        action, rtype, _ = classify_hello(
            "Policy bound",
            "The policy is now bound effective 09/15/2026.",
            "uw@carrier.example")
        assert (action, rtype) == (GENUINE, "binder_bound")


class TestInternalEnvelopeSubtypes:
    def test_internal_forward_subtype(self):
        action, rtype, _ = classify_hello(
            "Fwd: Pending Cancel for Non Pay - PHPK2734817-000",
            "---------- Forwarded message ---------\n"
            "From: PHLY Notices <notices@phly.com>\n"
            "Subject: Pending Cancel\n\nBody here.",
            "carlo@streetsmart.insurance")
        assert (action, rtype) == (INTERNAL, "internal_forward")

    def test_internal_discussion_subtype(self):
        action, rtype, _ = classify_hello(
            "Re: Hearts For Home renewal",
            "Jake — can you chase the renewal docs?",
            "carlo@streetsmart.insurance")
        assert (action, rtype) == (INTERNAL, "internal_discussion")


class TestCarrierNoticeReinstatement:
    def test_reinstatement_is_carrier_notice(self):
        action, rtype, _ = classify_hello(
            "Policy reinstated — 77881",
            "The policy has been reinstated effective 09/20/2026.",
            "notices@carrier.example")
        assert (action, rtype) == (GENUINE, "carrier_notice")

    def test_reinstatement_filing_category(self):
        assert filing_category_for(
            "carrier_notice",
            {"notice_subtype": "reinstatement"}) == "carrier_reinstatement"


class TestRoutes:
    def test_premium_finance_routes_to_accounting(self):
        assert ROUTE_FOR_REQUEST_TYPE["premium_finance"] == "accounting"

    def test_invoice_billing_routes_to_accounting(self):
        assert ROUTE_FOR_REQUEST_TYPE["invoice_billing"] == "accounting"

    def test_voicemail_routes_to_on_duty(self):
        assert ROUTE_FOR_REQUEST_TYPE["voicemail_text_notify"] == "on_duty"

    def test_wholesaler_routes_to_originating_producer(self):
        assert (ROUTE_FOR_REQUEST_TYPE["wholesaler_mga"]
                == "originating_producer")


# ===========================================================================
# Step 1 — forward unwrapping + dedupe
# ===========================================================================

GMAIL_FORWARD_BODY = (
    "---------- Forwarded message ---------\n"
    "From: PHLY Producer Notices <notices@phly.com>\n"
    "Date: Fri, 18 Sep 2026 09:12:00 -0400\n"
    "Subject: Pending Cancel for Non Pay - PHPK2734817-000\n"
    "To: carlo@streetsmart.insurance\n"
    "\n"
    "The policy listed above is pending cancellation for non-payment.\n"
)

OUTLOOK_FORWARD_BODY = (
    "FYI — see below.\n"
    "\n"
    "-----Original Message-----\n"
    "From: Underwriting <uw@jjins.com>\n"
    "Sent: Friday, September 18, 2026 9:12 AM\n"
    "To: Carlo Ferrara\n"
    "Subject: Underwriting requirements\n"
    "\n"
    "We still need the driver list.\n"
)


class TestForwardDetection:
    def test_fwd_subject_detected(self):
        assert detect_forward("Fwd: Pending cancel", "Some body")

    def test_gmail_block_detected(self):
        assert detect_forward("A subject", GMAIL_FORWARD_BODY)

    def test_outlook_block_detected(self):
        assert detect_forward("FW: docs", OUTLOOK_FORWARD_BODY)

    def test_plain_message_not_forward(self):
        assert not detect_forward("Quote request", "Please quote this risk.")


class TestUnwrapForward:
    def test_unwrap_gmail_forward(self):
        unwrapped = unwrap_forward("Fwd: Pending Cancel", GMAIL_FORWARD_BODY)
        assert unwrapped is not None
        assert unwrapped["original_sender"] == "notices@phly.com"
        assert (unwrapped["original_subject"]
                == "Pending Cancel for Non Pay - PHPK2734817-000")
        assert "pending cancellation" in unwrapped["original_body"]
        assert unwrapped["original_date"] is not None

    def test_unwrap_outlook_forward(self):
        unwrapped = unwrap_forward("FW: docs", OUTLOOK_FORWARD_BODY)
        assert unwrapped is not None
        assert unwrapped["original_sender"] == "uw@jjins.com"
        assert unwrapped["original_subject"] == "Underwriting requirements"
        assert "driver list" in unwrapped["original_body"]

    def test_unwrap_non_forward_returns_none(self):
        assert unwrap_forward("Quote request", "Please quote.") is None

    def test_unwrap_bare_fwd_subject_keeps_body(self):
        unwrapped = unwrap_forward("Fwd: something",
                                   "No forward block in this body.")
        assert unwrapped is not None
        assert unwrapped["original_sender"] is None
        assert unwrapped["original_subject"] == "something"


class TestDedupe:
    def _direct(self):
        return {
            "message_id": "msg-direct-1",
            "sender": "notices@phly.com",
            "subject": "Pending Cancel for Non Pay - PHPK2734817-000",
            "date": "Fri, 18 Sep 2026 09:12:00 -0400",
            "body": "The policy is pending cancellation for non-payment.",
        }

    def _forward_of_same(self):
        return {
            "message_id": "msg-fwd-1",
            "sender": "carlo@streetsmart.insurance",
            "subject": ("Fwd: Pending Cancel for Non Pay - "
                        "PHPK2734817-000"),
            "date": "Fri, 18 Sep 2026 10:00:00 -0400",
            "body": GMAIL_FORWARD_BODY,
        }

    def test_direct_and_forward_collapse(self):
        kept, collapsed = dedupe_messages([self._direct(),
                                           self._forward_of_same()])
        assert len(kept) == 1
        assert len(collapsed) == 1
        assert collapsed[0]["duplicate_of"] == "msg-direct-1"

    def test_different_subjects_kept(self):
        other = self._direct()
        other["message_id"] = "msg-direct-2"
        other["subject"] = "Policy reinstated - PHPK2734817-000"
        kept, collapsed = dedupe_messages([self._direct(), other])
        assert len(kept) == 2
        assert collapsed == []

    def test_same_message_id_twice_collapses(self):
        kept, collapsed = dedupe_messages([self._direct(), self._direct()])
        assert len(kept) == 1
        assert collapsed[0]["dedupe_reason"] == "same message_id seen twice"

    def test_unknown_sender_never_collapses_different_bodies(self):
        a = {"message_id": "a", "sender": "", "subject": "Fwd: docs",
             "date": "", "body": "First forward body."}
        b = {"message_id": "b", "sender": "", "subject": "Fwd: docs",
             "date": "", "body": "Completely different second body."}
        kept, collapsed = dedupe_messages([a, b])
        assert len(kept) == 2
        assert collapsed == []

    def test_dedupe_key_uses_original_sender(self):
        key = dedupe_key(self._forward_of_same())
        assert key[0] == "notices@phly.com"
        assert "pending cancel" in key[1]

    def test_normalize_subject_strips_prefixes(self):
        assert (normalize_subject("Re: Fwd: Pending Cancel")
                == "pending cancel")

    def test_content_hash_stable(self):
        assert (content_hash("a  b\nc") == content_hash("a b c"))
        assert content_hash("x") != content_hash("y")


class TestClassifyWithEnvelope:
    def test_forward_classifies_inner_content(self):
        result = classify_with_envelope(
            "Fwd: Pending Cancel for Non Pay - PHPK2734817-000",
            GMAIL_FORWARD_BODY,
            "carlo@streetsmart.insurance")
        assert result["envelope"] == "internal_forward"
        assert result["forwarder"] == "carlo@streetsmart.insurance"
        assert (result["action"], result["request_type"]) == (
            GENUINE, "carrier_notice")
        assert result["original"]["original_sender"] == "notices@phly.com"

    def test_direct_external_message(self):
        result = classify_with_envelope(
            "Quote request", "Please quote this risk.",
            "client@example.com")
        assert result["envelope"] == "external_direct"
        assert result["original"] is None

# ===========================================================================
# Step 2 — per-category filing spec
# ===========================================================================

class TestFilingSpec:
    def test_premium_finance_urgent_owner_and_sla(self):
        spec = spec_for("premium_finance_urgent")
        assert spec["owner_role"] == "accounting"
        assert spec["sla_hours"] == 24

    def test_premium_finance_routine_sla(self):
        spec = spec_for("premium_finance")
        assert spec["owner_role"] == "accounting"
        assert spec["sla_hours"] == 48

    def test_voicemail_one_hour_callback(self):
        spec = spec_for("voicemail_text_notify")
        assert spec["owner_role"] == "on_duty"
        assert spec["sla_hours"] == 1
        assert spec["task_kind"] == TASK_CALLBACK
        assert spec["alert"] == "on_duty"

    def test_cancellation_task_stays_open(self):
        plan = build_cancellation_plan({
            "applicant_id": "applicant-1",
            "summary": "PHLY pending-cancel notice for non-payment.",
            "policy_number": "PHPK2734817-000",
            "subject": "Pending Cancel for Non Pay",
            "body": "Pending cancellation for non-payment.",
            "identity": STRONG_IDENTITY,
        })
        task_action = next(a for a in plan["actions"]
                           if a["kind"] == "task")
        assert task_action["task"]["task_kind"] == TASK_CURE_OR_CANCEL
        assert task_action["task"]["stays_open"] is True
        assert (task_action["task"]["close_conditions"]
                == ["cure_confirmed", "cancel_confirmed"])

    def test_filing_category_mapping(self):
        assert filing_category_for(
            "carrier_notice",
            {"notice_subtype": "cancellation"}) == "carrier_cancellation"
        assert filing_category_for(
            "carrier_notice",
            {"notice_subtype": "renewal_notice"}) == "carrier_renewal"
        assert filing_category_for(
            "premium_finance", {"past_due": True}) == "premium_finance_urgent"
        assert filing_category_for(
            "premium_finance", {}) == "premium_finance"
        assert filing_category_for("binder_bound") == "binder_bound"


class TestIdentityVerification:
    def test_strong_match_passes(self):
        ok, _ = verify_filing_identity(
            STRONG_IDENTITY["email_insured"], STRONG_IDENTITY["email_policy"],
            STRONG_IDENTITY["match"])
        assert ok is True

    def test_name_only_match_fails(self):
        ok, reason = verify_filing_identity(
            NAME_ONLY_IDENTITY["email_insured"], None,
            NAME_ONLY_IDENTITY["match"])
        assert ok is False
        assert "name-only" in reason

    def test_low_confidence_fails(self):
        match = dict(STRONG_IDENTITY["match"], confidence="medium")
        ok, _ = verify_filing_identity("Insured", "POL1", match)
        assert ok is False

    def test_no_match_fails(self):
        ok, _ = verify_filing_identity("Insured", "POL1", None)
        assert ok is False

    def test_sender_alias_anchor_passes(self):
        match = dict(STRONG_IDENTITY["match"], matched_on="sender_alias")
        ok, _ = verify_filing_identity("Insured", None, match)
        assert ok is True


class TestMoneyClaimRule:
    def test_claim_without_proof_is_problem(self):
        problems = check_note_body(
            "The payment of $410.22 has been posted to the account.")
        assert any("without naming the proof" in p for p in problems)

    def test_claim_with_proof_passes(self):
        problems = check_note_body(
            "The payment of $410.22 posted on 09/18/2026 to the Trust "
            "bank account, receipt #015148.")
        assert problems == []

    def test_placeholder_rejected(self):
        assert note_is_placeholder("note_text") is True
        assert note_is_placeholder("") is True
        assert note_is_placeholder(
            "Real summary of the notice.") is False

    def test_note_claims_money_detection(self):
        assert note_claims_money("Payment cleared yesterday.")
        assert not note_claims_money("Please call the client about this.")

    def test_note_has_money_proof_needs_all_three(self):
        assert not note_has_money_proof("$410.22 posted.")
        assert note_has_money_proof(
            "$410.22 posted 09/18/2026 to the Trust bank account.")


class TestBuildAndExecutePlan:
    def _context(self, **overrides):
        context = {
            "applicant_id": "applicant-1",
            "summary": "PHLY pending-cancel notice for non-payment.",
            "policy_number": "PHPK2734817-000",
            "identity": STRONG_IDENTITY,
        }
        context.update(overrides)
        return context

    def test_blocked_when_identity_fails(self):
        plan = build_filing_plan(
            "carrier_cancellation",
            self._context(identity=NAME_ONLY_IDENTITY))
        assert plan["blocked"] is True
        result = execute_plan(plan, make_fakes()[0], dry_run=False)
        assert result["status"] == "blocked"
        assert result["done"] is False

    def test_blocked_when_summary_is_placeholder(self):
        plan = build_filing_plan(
            "carrier_cancellation", self._context(summary="note_text"))
        assert plan["blocked"] is True
        assert any("placeholder" in p for p in plan["note_problems"])

    def test_dry_run_performs_nothing(self):
        plan = build_filing_plan("carrier_cancellation", self._context())
        interfaces, calls = make_fakes()
        result = execute_plan(plan, interfaces, dry_run=True)
        assert result["status"] == "dry_run"
        assert result["done"] is False
        assert calls["note"] == []
        assert calls["task_create"] == []
        assert all(a["status"] == "would_do" for a in result["actions"])
        kinds = [a["kind"] for a in result["actions"]]
        assert kinds == ["note", "task"]

    def test_execute_files_note_and_creates_task(self):
        plan = build_filing_plan("carrier_cancellation", self._context())
        interfaces, calls = make_fakes()
        result = execute_plan(plan, interfaces, dry_run=False)
        assert result["status"] == "complete"
        assert result["done"] is True
        assert len(calls["note"]) == 1
        assert len(calls["task_create"]) == 1
        applicant_id, task = calls["task_create"][0]
        assert applicant_id == "applicant-1"
        assert task["task_kind"] == TASK_CURE_OR_CANCEL

    def test_task_dedupe_no_duplicate(self):
        existing = {"task_kind": TASK_CURE_OR_CANCEL,
                    "policy_number": "PHPK2734817-000",
                    "title": "old task"}
        plan = build_filing_plan("carrier_cancellation", self._context())
        interfaces, calls = make_fakes(open_tasks=[existing])
        result = execute_plan(plan, interfaces, dry_run=False)
        assert result["status"] == "complete"
        assert result["done"] is True
        assert calls["task_create"] == []
        task_results = [a for a in result["actions"] if a["kind"] == "task"]
        assert task_results[0]["status"] == "already_open"

    def test_missing_interfaces_leave_pending(self):
        plan = build_filing_plan("carrier_cancellation", self._context())
        result = execute_plan(plan, {}, dry_run=False)
        assert result["status"] == "partial"
        assert result["done"] is False
        assert all(a["status"] == "pending" for a in result["actions"])

    def test_item_not_done_until_task_exists(self):
        plan = build_filing_plan("carrier_cancellation", self._context())
        result = execute_plan(plan, {"note": make_fakes()[0]["note"]},
                              dry_run=False)
        assert result["done"] is False


# ===========================================================================
# Step 3 — cancellation / non-pay workflow
# ===========================================================================

def _cancel_context(**overrides):
    context = {
        "applicant_id": "applicant-1",
        "summary": "PHLY pending-cancel notice for non-payment.",
        "policy_number": "PHPK2734817-000",
        "subject": "Pending Cancel for Non Pay - PHPK2734817-000",
        "body": "The policy is pending cancellation for non-payment.",
        "identity": STRONG_IDENTITY,
    }
    context.update(overrides)
    return context


class TestNoticeSubtype:
    def test_cancellation(self):
        assert notice_subtype("Pending Cancel for Non Pay", "") == \
            "cancellation"

    def test_cancellation_in_body(self):
        assert notice_subtype("Document from PHLY",
                              "notice of cancellation") == "cancellation"

    def test_reinstatement(self):
        assert notice_subtype("Policy reinstated", "") == "reinstatement"

    def test_non_renewal(self):
        assert notice_subtype("Notice of non-renewal", "") == "non_renewal"

    def test_renewal_notice(self):
        assert notice_subtype("Renewal notice", "") == "renewal_notice"

    def test_other(self):
        assert notice_subtype("Monthly newsletter", "") == "other"


class TestExtractAmountToCure:
    def test_amount_to_cure_found(self):
        amount = extract_amount_to_cure(
            "Pending cancel",
            "The amount to cure is $1,204.55 to avoid cancellation.")
        assert amount == "$1,204.55"

    def test_no_amount_returns_none(self):
        assert extract_amount_to_cure("Pending cancel",
                                      "Please remit payment.") is None


class TestCancellationLedger:
    def test_open_and_find(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        entry, created = ledger.open_entry("applicant-1", "PHPK2734817-000")
        assert created is True
        assert ledger.find_open("PHPK2734817-000")["ledger_id"] == \
            entry["ledger_id"]

    def test_second_open_dedupes(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        first, _ = ledger.open_entry("applicant-1", "PHPK2734817-000")
        second, created = ledger.open_entry("applicant-1", "PHPK2734817-000")
        assert created is False
        assert second["ledger_id"] == first["ledger_id"]

    def test_attach_notice_no_new_entry(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        ledger.open_entry("applicant-1", "PHPK2734817-000")
        ledger.attach_notice("PHPK2734817-000", "second notice arrived")
        entry = ledger.find_open("PHPK2734817-000")
        assert len(entry["notes"]) == 1
        assert ledger.is_done("PHPK2734817-000") is False

    def test_propose_and_confirm_close(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        ledger.open_entry("applicant-1", "PHPK2734817-000")
        proposed = ledger.propose_close("PHPK2734817-000",
                                        "reinstatement received")
        assert proposed["status"] == "proposed_closed"
        assert ledger.is_done("PHPK2734817-000") is False
        closed = ledger.confirm_close("PHPK2734817-000", "cure_confirmed")
        assert closed["status"] == "closed"
        assert closed["close_condition"] == "cure_confirmed"
        assert ledger.is_done("PHPK2734817-000") is True

    def test_confirm_close_rejects_bad_condition(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        ledger.open_entry("applicant-1", "PHPK2734817-000")
        with pytest.raises(ValueError):
            ledger.confirm_close("PHPK2734817-000", "maybe_paid")

    def test_ledger_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        CancellationLedger(path).open_entry("applicant-1", "PHPK2734817-000")
        assert CancellationLedger(path).find_open(
            "PHPK2734817-000") is not None


class TestProcessCancellationNotice:
    def test_first_notice_opens_task(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        outcome = process_cancellation_notice(_cancel_context(), ledger)
        assert outcome["action"] == "new_task"
        assert outcome["subtype"] == "cancellation"
        assert outcome["ledger_created"] is True
        task = next(a for a in outcome["plan"]["actions"]
                    if a["kind"] == "task")["task"]
        assert task["task_kind"] == TASK_CURE_OR_CANCEL
        assert task["stays_open"] is True

    def test_second_notice_attaches_no_new_task(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        process_cancellation_notice(_cancel_context(), ledger)
        outcome = process_cancellation_notice(_cancel_context(), ledger)
        assert outcome["action"] == "attach_to_open"
        assert outcome["ledger_created"] is False
        task_actions = [a for a in outcome["plan"]["actions"]
                        if a["kind"] == "task"]
        assert task_actions == []
        assert outcome["plan"]["actions"][0]["kind"] == "note"

    def test_reinstatement_proposes_close(self, tmp_path):
        ledger = CancellationLedger(str(tmp_path / "ledger.jsonl"))
        process_cancellation_notice(_cancel_context(), ledger)
        outcome = process_cancellation_notice(
            _cancel_context(subject="Policy reinstated - PHPK2734817-000",
                            body="The policy has been reinstated."),
            ledger)
        assert outcome["action"] == "propose_close"
        assert outcome["plan"]["filing_category"] == "carrier_reinstatement"

    def test_reinstatement_plan_shape(self):
        plan = build_reinstatement_plan(_cancel_context())
        task = next(a for a in plan["actions"] if a["kind"] == "task")["task"]
        assert task["task_kind"] == "close_cure_or_cancel"
        assert task["proposed_close"] is True


# ===========================================================================
# Step 4 — premium-finance routing (filing-level)
# ===========================================================================

class TestPremiumFinanceRouting:
    def test_urgent_plan_routes_accounting_24h(self):
        plan = build_filing_plan("premium_finance_urgent", {
            "applicant_id": "applicant-1",
            "summary": "Capital Premium past-due notice: payment of "
                       "$1,100.00 due to avoid cancellation.",
            "policy_number": "CPP-9911",
            "identity": STRONG_IDENTITY,
        })
        assert plan["owner_role"] == "accounting"
        assert plan["sla_hours"] == 24
        task = next(a for a in plan["actions"] if a["kind"] == "task")["task"]
        assert task["owner_role"] == "accounting"

    def test_routine_plan_routes_accounting_48h_no_task(self):
        plan = build_filing_plan("premium_finance", {
            "applicant_id": "applicant-1",
            "summary": "Ascend monthly statement for the financed premium.",
            "policy_number": "CPP-9911",
            "identity": STRONG_IDENTITY,
        })
        assert plan["owner_role"] == "accounting"
        assert plan["sla_hours"] == 48
        assert [a for a in plan["actions"] if a["kind"] == "task"] == []


# ===========================================================================
# Step 5 — voicemail / SMS callback workflow
# ===========================================================================

VOICEMAIL_SUBJECT = "Voice Mail from Maria Lopez (5551234567)"
VOICEMAIL_BODY = (
    "Received: 09/26/2026 10:14 AM\n"
    "Transcription: Hi, this is Maria Lopez calling about my auto policy "
    "renewal. Please call me back today.\n"
)
SMS_SUBJECT = "Re: Do Not Reply - Mununga Kipata has sent you a text message"
SMS_BODY = "Hi, do you have a moment to talk about adding a driver?"


class TestParseVoicemailNotification:
    def test_voicemail_parsed(self):
        parsed = parse_voicemail_notification(VOICEMAIL_SUBJECT,
                                              VOICEMAIL_BODY)
        assert parsed["message_type"] == "voicemail"
        assert parsed["caller_name"] == "Maria Lopez"
        assert parsed["caller_number"] == "5551234567"
        assert "auto policy renewal" in parsed["transcription"]
        assert parsed["received_at"] == "09/26/2026 10:14 AM"

    def test_sms_parsed(self):
        parsed = parse_voicemail_notification(SMS_SUBJECT, SMS_BODY)
        assert parsed["message_type"] == "sms"
        assert parsed["caller_name"] == "Mununga Kipata"
        assert "adding a driver" in parsed["transcription"]

    def test_missed_call_parsed(self):
        parsed = parse_voicemail_notification(
            "Missed Call from John Smith (5559876543)", "")
        assert parsed["message_type"] == "missed_call"
        assert parsed["caller_name"] == "John Smith"

    def test_unknown_shape(self):
        parsed = parse_voicemail_notification("Weekly digest", "News.")
        assert parsed["message_type"] == "unknown"


class TestVoicemailNoteSanitizing:
    def test_digits_stripped_from_note(self):
        summary = build_voicemail_summary(
            parse_voicemail_notification(VOICEMAIL_SUBJECT, VOICEMAIL_BODY))
        assert "5551234567" not in summary
        assert "555-123-4567" not in summary
        assert "Maria Lopez" in summary
        assert "auto policy renewal" in summary

    def test_sanitize_note_body(self):
        cleaned = sanitize_note_body("Call 555-123-4567 today.")
        assert "555-123-4567" not in cleaned
        assert "[number on file]" in cleaned


class TestVoicemailPlan:
    def _plan(self):
        return build_voicemail_plan({
            "applicant_id": "applicant-9",
            "subject": VOICEMAIL_SUBJECT,
            "body": VOICEMAIL_BODY,
            "sender": "noreply@ringcentral.example",
            "identity": STRONG_IDENTITY,
        })

    def test_plan_owner_and_sla(self):
        plan = self._plan()
        assert plan["owner_role"] == "on_duty"
        assert plan["sla_hours"] == 1

    def test_plan_has_note_and_alert_and_callback_task(self):
        plan = self._plan()
        kinds = [a["kind"] for a in plan["actions"]]
        assert kinds == ["note", "task", "alert"]
        task = next(a for a in plan["actions"]
                    if a["kind"] == "task")["task"]
        assert task["task_kind"] == TASK_CALLBACK
        alert = next(a for a in plan["actions"] if a["kind"] == "alert")
        assert alert["to"] == "on_duty"
        assert "5551234567" in alert["message"]

    def test_plan_note_has_no_digits_but_alert_does(self):
        plan = self._plan()
        note = next(a for a in plan["actions"] if a["kind"] == "note")
        assert "5551234567" not in note["body"]
        assert "Maria Lopez" in note["body"]

    def test_plan_executes_through_fakes(self):
        plan = self._plan()
        interfaces, calls = make_fakes()
        result = execute_plan(plan, interfaces, dry_run=False)
        assert result["status"] == "complete"
        assert result["done"] is True
        assert len(calls["alert"]) == 1
        assert calls["alert"][0][0] == "on_duty"


# ===========================================================================
# Preservation: vendor-alias guard + portal-fetch behavior
# ===========================================================================

class TestPreservation:
    def test_vendor_senders_never_become_aliases(self):
        from robie_job_engine.hello_unmatched_queue import sender_is_vendor
        for sender in ("noreply@useascend.com",
                       "notices@phly.com",
                       "collections@brownandjoseph.net",
                       "billing@britecore.com"):
            assert sender_is_vendor(sender) is True, sender
        assert sender_is_vendor("client@example.com") is False

    def test_queue_resolution_writes_no_alias_for_vendor(self, tmp_path):
        from robie_job_engine.hello_unmatched_queue import (
            resolve_entry,
        )
        queue_path = str(tmp_path / "queue.jsonl")
        alias_path = str(tmp_path / "aliases.json")
        with open(queue_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "entry_id": "hu-000001",
                "status": "open",
                "subject": "Ascend return premium",
                "sender_email": "noreply@useascend.com",
            }) + "\n")
        result = resolve_entry(
            "hu-000001", applicant_id=12345,
            account_name="Some Client", resolved_by="test",
            queue_path=queue_path, alias_store_path=alias_path)
        assert result["alias_written"] is False

    def test_portal_fetch_single_attempt_then_defer(self):
        from robie_job_engine import hello_portal_fetch as pf

        attempts = []

        class FakeAccessor:
            def access(self, resource_name):
                assert "password" not in resource_name.lower() or True
                return "user-or-pass"

        def fake_browser_fetch(portal, username, password, url,
                               timeout_secs):
            attempts.append(url)
            raise RuntimeError("login rejected")

        pf.register_portal(pf.PortalConfig(
            domain="portal-test.example",
            handler="generic_form",
            username_secret="projects/p/secrets/test-user/versions/latest",
            password_secret="projects/p/secrets/test-pass/versions/latest",
            login_url="https://portal-test.example/login",
        ))
        data, reason = pf.fetch_portal_document(
            "https://portal-test.example/docs/1",
            secret_accessor=FakeAccessor(),
            browser_fetch=fake_browser_fetch)
        assert data is None
        assert reason == "login_failed"
        assert attempts == ["https://portal-test.example/docs/1"]
