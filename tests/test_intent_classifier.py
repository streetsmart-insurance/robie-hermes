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
