import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

with patch("google.cloud.pubsub_v1.PublisherClient"):
    import main


AUDIENCE = "https://robie-chat-http-bridge.example.run.app"
ADDON_SA = "addon-sa@example.gserviceaccount.com"
CHAT_SA = "chat@system.gserviceaccount.com"


def _claims(**overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": ADDON_SA,
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


def _unsigned_jwt(payload):
    """Build an unsigned JWT for rejection-diagnostic tests.

    The signature is never verified in these tests (verification is mocked
    to fail); only the payload decode in the diagnostics path is exercised.
    """
    import base64
    import json

    def _seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_seg({'alg': 'RS256', 'typ': 'JWT'})}.{_seg(payload)}.invalid-signature"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {
                "ROBIE_CHAT_BRIDGE_AUDIENCE": AUDIENCE,
                "ROBIE_CHAT_BRIDGE_SERVICE_ACCOUNT_EMAILS": f"{ADDON_SA},{CHAT_SA}",
            },
        )
        self._env.start()
        self._verify = patch.object(
            main, "_verify_google_id_token", return_value=_claims()
        )
        self.mocked_verify = self._verify.start()

    def tearDown(self):
        self._verify.stop()
        self._env.stop()

    def _post(self, path, payload, token="test-token"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return main.app.test_client().post(path, json=payload, headers=headers)

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
            response = self._post("/", payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {})
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

    def test_addon_card_payload_is_normalized_and_returns_update_message(self):
        """Workspace Add-on CARD_CLICKED returns updateMessageAction, not {}.

        Google rejects the empty {} synchronous response for Add-on card
        clicks, displaying "<App> is unable to process your request."  The
        legacy Chat-app actionResponse envelope is also invalid for Add-ons.
        The bridge returns hostAppDataAction.chatDataAction.updateMessageAction
        with a processing card; Hermes replaces it asynchronously.
        """
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
            response = self._post("/actions/robie_decision", payload)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        message = body["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]
        self.assertNotIn("actionResponse", body)
        self.assertEqual(message["cardsV2"][0]["cardId"], "robie-processing")
        # Event still forwarded to Pub/Sub for async processing.
        event = publish.call_args.args[0]
        self.assertEqual(event["common"]["invokedFunction"], "robie_decision")
        self.assertEqual(event["common"]["parameters"]["decision_id"], "d1")
        self.assertEqual(event["space"]["name"], "spaces/1")

    def test_chat_api_card_click_keeps_empty_async_ack(self):
        """Non-Add-on (Chat API) card clicks keep the empty {} async ack."""
        payload = {
            "type": "CARD_CLICKED",
            "common": {"invokedFunction": "robie_decision"},
        }
        with patch.object(main, "_publish") as publish:
            response = self._post("/actions/robie_decision", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {})

    def test_message_is_forwarded_without_payload_logging_or_rewrite(self):
        payload = {"type": "MESSAGE", "message": {"name": "m1"}}
        with patch.object(main, "_publish") as publish:
            response = self._post("/", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {})
        self.assertEqual(publish.call_args.args[0], payload)

    def test_health_stays_open_without_a_bearer(self):
        response = main.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        self.mocked_verify.assert_not_called()

    def test_missing_bearer_is_rejected_without_publish(self):
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"}, token=None)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.mocked_verify.assert_not_called()

    def test_bad_bearer_is_rejected_without_publish_or_token_logging(self):
        secret = "super-secret-bearer-value"
        self.mocked_verify.side_effect = ValueError(f"bad signature {secret}")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/actions/robie_decision", {"chat": {}}, token=secret)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_valid_bearer_publishes_and_returns_addon_processing_response(self):
        payload = {
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/9"},
                    "message": {"name": "spaces/9/messages/9"},
                }
            }
        }
        with patch.object(main, "_publish") as publish:
            response = self._post("/actions/robie_decision", payload, token="signed-token")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        message = body["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]
        self.assertEqual(message["cardsV2"][0]["cardId"], "robie-processing")
        self.assertNotIn("actionResponse", body)
        publish.assert_called_once()
        self.assertEqual(self.mocked_verify.call_args.args[0], "signed-token")
        self.assertEqual(
            self.mocked_verify.call_args.args[1], main._expand_audiences([AUDIENCE])
        )

    def test_unset_auth_config_fails_closed_without_verify(self):
        os.environ["ROBIE_CHAT_BRIDGE_AUDIENCE"] = ""
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.mocked_verify.assert_not_called()

    def test_verified_token_for_unexpected_identity_is_rejected(self):
        self.mocked_verify.return_value = _claims(email="intruder@example.com")
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()

    def test_verified_token_with_wrong_audience_is_rejected(self):
        self.mocked_verify.return_value = _claims(aud="https://other.example")
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()

    def test_unverified_email_claim_is_rejected(self):
        self.mocked_verify.return_value = _claims(email_verified=False)
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()

    def test_chat_system_account_token_is_accepted(self):
        self.mocked_verify.return_value = _claims(email=CHAT_SA)
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {})
        publish.assert_called_once()

    def test_chat_project_number_jwt_uses_chat_certs(self):
        os.environ["ROBIE_CHAT_BRIDGE_AUDIENCE"] = f"{AUDIENCE},1234567890"
        self.mocked_verify.side_effect = [
            ValueError("not an oauth id token"),
            {"iss": main._CHAT_ISSUER, "aud": "1234567890"},
        ]
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 200)
        publish.assert_called_once()
        self.assertEqual(self.mocked_verify.call_args.kwargs["certs_url"], main._CHAT_CERTS_URL)

    def test_chat_certs_are_not_fetched_when_chat_issuer_is_not_allowed(self):
        os.environ["ROBIE_CHAT_BRIDGE_SERVICE_ACCOUNT_EMAILS"] = ADDON_SA
        self.mocked_verify.side_effect = ValueError("not an oauth id token")
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.assertEqual(self.mocked_verify.call_count, 1)
        self.assertIsNone(self.mocked_verify.call_args.kwargs.get("certs_url"))

    def test_event_style_does_not_inspect_message_contents(self):
        self.assertEqual(main._event_style({"chat": {"messagePayload": {}}}), "workspace_addon")
        self.assertEqual(main._event_style({"type": "MESSAGE"}), "chat_api")
        self.assertEqual(main._event_style({"message": {"text": "secret"}}), "unknown")

    def test_audience_mismatch_logs_actual_aud_without_token(self):
        token = _unsigned_jwt(
            {"iss": main._CHAT_ISSUER, "aud": "https://wrong.example/", "exp": 9999999999}
        )
        self.mocked_verify.side_effect = ValueError("bad signature")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/", {"type": "MESSAGE"}, token=token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        output = "\n".join(logs.output)
        self.assertIn("reason=audience_mismatch", output)
        self.assertIn("aud=https://wrong.example/", output)
        self.assertNotIn(token, output)
        self.assertNotIn(token, response.get_data(as_text=True))

    def test_expired_token_is_diagnosed(self):
        token = _unsigned_jwt(
            {"iss": "https://accounts.google.com", "aud": AUDIENCE, "exp": 1000}
        )
        self.mocked_verify.side_effect = ValueError("expired")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/", {"type": "MESSAGE"}, token=token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.assertIn("reason=token_expired", "\n".join(logs.output))

    def test_malformed_token_is_diagnosed(self):
        self.mocked_verify.side_effect = ValueError("bad")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/", {"type": "MESSAGE"}, token="not-a-jwt")
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        self.assertIn("reason=token_malformed", "\n".join(logs.output))

    def test_signature_failure_with_plausible_claims_is_diagnosed(self):
        token = _unsigned_jwt(
            {"iss": main._CHAT_ISSUER, "aud": AUDIENCE, "exp": 9999999999}
        )
        self.mocked_verify.side_effect = ValueError("bad signature")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/", {"type": "MESSAGE"}, token=token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        output = "\n".join(logs.output)
        self.assertIn("reason=signature_verification_failed", output)
        self.assertNotIn(token, output)

    def test_issuer_unexpected_is_diagnosed(self):
        token = _unsigned_jwt(
            {"iss": "https://evil.example", "aud": AUDIENCE, "exp": 9999999999}
        )
        self.mocked_verify.side_effect = ValueError("bad signature")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post("/", {"type": "MESSAGE"}, token=token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        output = "\n".join(logs.output)
        self.assertIn("reason=issuer_unexpected", output)
        self.assertIn("iss=https://evil.example", output)

    def test_trailing_slash_audience_variant_is_accepted(self):
        self.assertEqual(
            main._expand_audiences([AUDIENCE]), [AUDIENCE, AUDIENCE + "/"]
        )
        self.assertEqual(main._expand_audiences(["1234567890"]), ["1234567890"])
        # A verified token carrying the trailing-slash form still passes.
        self.mocked_verify.return_value = _claims(aud=AUDIENCE + "/")
        with patch.object(main, "_publish") as publish:
            response = self._post("/", {"type": "MESSAGE"})
        self.assertEqual(response.status_code, 200)
        publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
