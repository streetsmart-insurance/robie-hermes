"""Unit tests for EZLynx discussion discovery and auto-threading."""

from unittest.mock import patch, MagicMock
import pytest
from src.ezlynx.api_client import EZLynxApiClient, ROBIE_SIGNATURE


@pytest.fixture
def client():
    return EZLynxApiClient(
        base_url="https://app.ezlynx.com",
        client_id="test_client",
        client_secret="test_secret",
        username="test_user",
        password="test_password",
        app_secret="test_app_secret",
        integration_group_id="159",
    )


MOCK_DISCUSSIONS = [
    {
        "discussionId": 840692604,
        "title": "Renewal Manual Workers comp | PWC1239278 Associated Specialty",
        "noteCount": 6,
        "taskCount": 1,
        "lastModifiedByName": "Eimy Ramos",
    },
    {
        "discussionId": 841874728,
        "title": "Manual Workers Comp Renewal",
        "noteCount": 1,
        "taskCount": 0,
        "lastModifiedByName": "Robie AI",
    },
    {
        "discussionId": 826326535,
        "title": "Business Owners Manual Renewal",
        "noteCount": 11,
        "taskCount": 2,
        "lastModifiedByName": "Carlo Ferrara",
    },
]


def test_find_matching_discussion_by_policy_number(client):
    with patch.object(client, "get_applicant_discussions", return_value=MOCK_DISCUSSIONS):
        match = client.find_matching_discussion(
            applicant_id="21588091",
            policy_number="PWC1239278",
            line_of_business="Workers comp",
            carrier_name="Associated Specialty"
        )
        assert match is not None
        assert match["discussionId"] == 840692604
        assert match["title"] == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"


def test_find_matching_discussion_by_tokens_prefers_active_human_card(client):
    with patch.object(client, "get_applicant_discussions", return_value=MOCK_DISCUSSIONS):
        match = client.find_matching_discussion(
            applicant_id="21587333",
            policy_number=None,
            line_of_business="Business Owners",
            carrier_name="Coterie"
        )
        assert match is not None
        assert match["discussionId"] == 826326535
        assert match["title"] == "Business Owners Manual Renewal"


def test_resolve_discussion_title_uses_matched_card(client):
    with patch.object(client, "get_applicant_discussions", return_value=MOCK_DISCUSSIONS):
        resolved = client.resolve_discussion_title(
            applicant_id="21588091",
            discussion_title="Manual Workers Comp Renewal",
            policy_number="PWC1239278",
            line_of_business="Workers comp",
            carrier_name="Associated Specialty"
        )
        assert resolved == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"


def test_resolve_discussion_title_agency_naming_fallback(client):
    with patch.object(client, "get_applicant_discussions", return_value=[]):
        resolved = client.resolve_discussion_title(
            applicant_id="999999",
            discussion_title=None,
            policy_number="ABC987654",
            line_of_business="Commercial Auto",
            carrier_name="Progressive Casualty Ins"
        )
        assert resolved == "Renewal Manual Commercial Auto | ABC987654 Progressive Casualty Ins"


def test_add_note_to_discussion_threads_into_resolved_card(client):
    with patch.object(client, "get_applicant_discussions", return_value=MOCK_DISCUSSIONS), \
         patch.object(client, "authenticate_classic", return_value=True), \
         patch("requests.post") as mock_post:

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"NoteId": 998877}),
            text='{"NoteId": 998877}'
        )

        result = client.add_note_to_discussion(
            applicant_id="21588091",
            discussion_title="Manual Workers Comp Renewal",
            note_text="Underwriter received",
            policy_number="PWC1239278",
            line_of_business="Workers comp",
            carrier_name="Associated Specialty"
        )

        assert result["status"] == "success"
        assert result["discussion_title"] == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"
        assert result["note_id"] == 998877

        called_args = mock_post.call_args
        payload = called_args[1]["json"]
        assert payload["DiscussionTitle"] == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"
        assert payload["ApplicantId"] == 21588091
        assert "Robie was here" in payload["NoteDescription"]


def test_find_matching_discussion_filters_out_loss_runs_auxiliary_thread(client):
    discussions = [
        {
            "discussionId": 830843439,
            "title": "Loss Runs request for ABC TRANSPIRATION LLC | 02TRM066190-01",
            "noteCount": 4,
            "lastModifiedByName": "Jose Cabrera",
        },
        {
            "discussionId": 825365065,
            "title": "Commercial Auto Renewal (2026-2027)",
            "noteCount": 32,
            "lastModifiedByName": "Ricardo Aguilar",
            "discussionNote": {"policyNumber": "02TRM066190-01"},
        },
    ]
    with patch.object(client, "get_applicant_discussions", return_value=discussions):
        match = client.find_matching_discussion(
            applicant_id="193438339",
            policy_number="02TRM066190-01",
            line_of_business="Commercial Auto",
            carrier_name="Berkshire Hathaway Homestate"
        )
        assert match is not None
        assert match["discussionId"] == 825365065
        assert match["title"] == "Commercial Auto Renewal (2026-2027)"


DISQUALIFIED_AUX_TITLES = [
    ("Loss Runs request for Insured | POL-1", "loss runs"),
    ("Certificate of Insurance - POL-1", "coi"),
    ("COI Request POL-1", "coi"),
    ("Text Sent to Insured", "text sent"),
    ("Email sent by Automation Center", "automation center"),
    ("Automation Center", "automation center"),
    ("Cancellation Notice POL-1", "cancellation"),
]


def test_find_matching_discussion_skips_all_disqualified_auxiliary_titles(client):
    """UW filing must never latch onto Loss Runs / COI / Text Sent / Automation / cancel cards."""
    renewal_card = {
        "discussionId": 900000001,
        "title": "General Liability Renewal | POL-1 Coterie",
        "noteCount": 8,
        "lastModifiedByName": "Sandy Santana",
        "discussionNote": {"policyNumber": "POL-1"},
    }
    for bad_title, _reason in DISQUALIFIED_AUX_TITLES:
        discussions = [
            {
                "discussionId": 800000000,
                "title": bad_title,
                "noteCount": 99,
                "lastModifiedByName": "Jose Cabrera",
                "discussionNote": {"policyNumber": "POL-1"},
            },
            renewal_card,
        ]
        with patch.object(client, "get_applicant_discussions", return_value=discussions):
            match = client.find_matching_discussion(
                applicant_id="100",
                policy_number="POL-1",
                line_of_business="General Liability",
                carrier_name="Coterie",
            )
            assert match is not None, f"Expected renewal card when auxiliary was '{bad_title}'"
            assert match["discussionId"] == 900000001
            assert match["title"] == renewal_card["title"]


def test_find_matching_discussion_returns_none_when_only_disqualified_cards(client):
    discussions = [
        {
            "discussionId": 1,
            "title": "Loss Runs request | POL-9",
            "noteCount": 4,
            "discussionNote": {"policyNumber": "POL-9"},
        },
        {
            "discussionId": 2,
            "title": "Text Sent",
            "noteCount": 2,
        },
    ]
    with patch.object(client, "get_applicant_discussions", return_value=discussions):
        match = client.find_matching_discussion(
            applicant_id="100",
            policy_number="POL-9",
            line_of_business="General Liability",
            carrier_name="Coterie",
        )
        assert match is None


def test_add_note_require_existing_discussion_blocks_orphan_fallback(client):
    with patch.object(client, "get_applicant_discussions", return_value=[]), \
         patch.object(client, "authenticate_classic", return_value=True), \
         patch("requests.post") as mock_post:
        result = client.add_note_to_discussion(
            applicant_id="21588091",
            discussion_title="Manual Workers Comp Renewal",
            note_text="Underwriter received",
            policy_number="PWC1239278",
            line_of_business="Workers comp",
            carrier_name="Associated Specialty",
            require_existing_discussion=True,
        )
        assert result["status"] == "error"
        assert result["error"] == "no_existing_titled_discussion"
        mock_post.assert_not_called()


def test_add_note_require_existing_discussion_posts_to_matched_titled_card(client):
    with patch.object(client, "get_applicant_discussions", return_value=MOCK_DISCUSSIONS), \
         patch.object(client, "authenticate_classic", return_value=True), \
         patch("requests.post") as mock_post:

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"NoteId": 112233}),
            text='{"NoteId": 112233}',
        )

        result = client.add_note_to_discussion(
            applicant_id="21588091",
            discussion_title="Manual Workers Comp Renewal",
            note_text="Underwriter received",
            policy_number="PWC1239278",
            line_of_business="Workers comp",
            carrier_name="Associated Specialty",
            require_existing_discussion=True,
        )

        assert result["status"] == "success"
        assert result["discussion_title"] == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"
        payload = mock_post.call_args[1]["json"]
        assert payload["DiscussionTitle"] == "Renewal Manual Workers comp | PWC1239278 Associated Specialty"
        assert payload["NoteDescription"].startswith("Policy: #PWC1239278")
        assert payload["NoteDescription"].rstrip().endswith("Robie was here")

