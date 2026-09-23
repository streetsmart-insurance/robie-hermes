import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

with patch("google.cloud.pubsub_v1.PublisherClient"):
    import main


class BridgeTests(unittest.TestCase):
    def assert_empty_json_ack(self, response):
        """Interactive ack: empty body, application/json, not HTML and not `{}`."""
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b"")
        self.assertEqual(response.mimetype, "application/json")
        self.assertNotIn("text/html", response.content_type or "")
        self.assertNotEqual((response.get_data(as_text=True) or "").strip(), "{}")
        self.assertIsNone(response.get_json(silent=True))

    def test_addon_message_payload_is_normalized_for_legacy_hermes(self):
        payload = {
            "authorizationEventObject": {"userOAuthToken": "redacted-test-token"},
            "commonEventObject": {"hostApp": "CHAT"},
            "chat": {
                "messagePayload": {
                    "space": {
                        "name": "spaces/1",
                        "spaceType": "DIRECT_MESSAGE",
                    },
                    "message": {
                        "name": "spaces/1/messages/1",
                        "argumentText": "",
                        "slashCommand": {"commandId": "6"},
                        "sender": {
                            "name": "users/1",
                            "email": "requester@example.com",
                        },
                    },
                }
            },
        }
        original = copy.deepcopy(payload)

        with patch.object(main, "_publish") as publish:
            response = main.app.test_client().post("/", json=payload)

        self.assert_empty_json_ack(response)
        event, event_type = publish.call_args.args
        self.assertEqual(event_type, "google.workspace.chat.event.v1.received")
        self.assertEqual(event["type"], "MESSAGE")
        self.assertEqual(event["space"]["name"], "spaces/1")
        self.assertEqual(event["message"]["slashCommand"]["commandId"], "6")
        self.assertEqual(event["user"]["name"], "users/1")
        self.assertEqual(payload, original)

    def test_addon_message_does_not_overwrite_existing_legacy_fields(self):
        payload = {
            "type": "MESSAGE",
            "message": {"name": "legacy-message"},
            "space": {"name": "legacy-space"},
            "user": {"name": "legacy-user"},
            "chat": {
                "messagePayload": {
                    "message": {"name": "addon-message"},
                    "space": {"name": "addon-space"},
                    "user": {"name": "addon-user"},
                }
            },
        }

        event = main._normalize(payload, None)

        self.assertEqual(event["message"]["name"], "legacy-message")
        self.assertEqual(event["space"]["name"], "legacy-space")
        self.assertEqual(event["user"]["name"], "legacy-user")

    def test_addon_card_payload_is_normalized_and_acknowledged(self):
        payload = {
            "commonEventObject": {"parameters": {"decision_id": "d1"}},
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/1"},
                    "message": {"name": "spaces/1/messages/1"},
                }
            },
        }
        with patch.object(main, "_publish") as publish:
            response = main.app.test_client().post(
                "/actions/robie_decision", json=payload
            )
        self.assert_empty_json_ack(response)
        event = publish.call_args.args[0]
        self.assertEqual(event["common"]["invokedFunction"], "robie_decision")
        self.assertEqual(event["common"]["parameters"]["decision_id"], "d1")
        self.assertEqual(event["space"]["name"], "spaces/1")

    def test_message_is_forwarded_without_payload_logging_or_rewrite(self):
        payload = {"type": "MESSAGE", "message": {"name": "m1"}}
        with patch.object(main, "_publish") as publish:
            response = main.app.test_client().post("/", json=payload)
        self.assert_empty_json_ack(response)
        self.assertEqual(publish.call_args.args[0], payload)

    def test_event_style_does_not_inspect_message_contents(self):
        self.assertEqual(main._event_style({"chat": {"messagePayload": {}}}), "workspace_addon")
        self.assertEqual(main._event_style({"type": "MESSAGE"}), "chat_api")
        self.assertEqual(main._event_style({"message": {"text": "secret"}}), "unknown")


if __name__ == "__main__":
    unittest.main()
