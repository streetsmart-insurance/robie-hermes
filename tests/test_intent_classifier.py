"""Tests for Underwriter Intent Classification."""

from src.email_outreach.intent_classifier import UnderwriterIntentClassifier

def test_intent_quote_attached():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="Re: [RENEWAL-REQ-10] Renewal Terms: John Doe",
        body_text="Hi team, please find the attached renewal proposal for John Doe. Let me know if you want to bind.",
        attachment_filenames=["John_Doe_Renewal_Quote.pdf"]
    )
    assert res.intent == "QUOTE_ATTACHED"
    assert res.has_quote_attachment is True

def test_intent_info_requested():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="Re: [RENEWAL-REQ-12] Renewal Terms: Apex Logistics",
        body_text="We need updated loss runs for the last 3 years and driver's license numbers before releasing terms.",
        attachment_filenames=[]
    )
    assert res.intent == "INFO_REQUESTED"
    assert any("loss run" in item for item in res.requested_items)

def test_intent_decline():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="Re: [RENEWAL-REQ-15] Renewal Terms: Coastal Property",
        body_text="Due to market restrictions in Florida, we are unable to offer renewal terms this year.",
        attachment_filenames=[]
    )
    assert res.intent == "NON_RENEWAL_DECLINED"

def test_intent_acknowledged():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="Re: [RENEWAL-REQ-18] Renewal Terms",
        body_text="Received your request, I am working on this and will send over terms by tomorrow.",
        attachment_filenames=[]
    )
    assert res.intent == "ACKNOWLEDGED"

def test_intent_sneed_non_renewal_notice():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="NOTICE OF NON-RENEWAL, Client:  Le Shawn Sneed",
        body_text="Notice of Non-renewal Le Shawn Sneed Jake Ferrara, this email provides notification of important document(s) regarding Non-Trucking Liability Policy CUS062900594. Reason for Non-renewal: Renewal Declined. NON-RENEWAL DATE: 10/20/2026.",
        attachment_filenames=["EmailSig_Logo.png"]
    )
    assert res.intent == "NON_RENEWAL_DECLINED"
    assert "declining to renew" in res.summary.lower()
    assert res.document_type == "non renewal"

def test_intent_loss_runs_attached():
    classifier = UnderwriterIntentClassifier()
    res = classifier.classify(
        subject="Re: Loss Runs request for ABC TRANSPIRATION LLC | 02TRM066190-01",
        body_text="Please find the attached 5-year currently valued loss runs for ABC TRANSPIRATION LLC.",
        attachment_filenames=["02TRM066190-01_Loss_Runs.pdf"]
    )
    assert res.intent == "LOSS_RUNS_ATTACHED"
    assert res.document_type == "loss runs"
    assert "loss runs" in res.summary.lower()

