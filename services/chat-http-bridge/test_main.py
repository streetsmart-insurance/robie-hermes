import copy
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_message_test_marker_after_mention_and_argument_text(self):
        for message in (
            {"text": "[[robie-test]] hello"},
            {"text": "<users/bot> [[robie-test]] hello"},
            {"text": "<users/one> <users/two> [[robie-test]] /stop"},
            {"argumentText": "<users/one> @robie robie-test: use follow up"},
            {"text": "@robie hello", "argumentText": "robie-test: hello"},
        ):
            with self.subTest(message=message):
                self.assertEqual(main._routing_attributes({"message": message}, "MESSAGE")["robie_env"], "test")
        self.assertNotIn("robie_env", main._routing_attributes({"message": {"text": "hello"}}, "MESSAGE"))

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
            self.mocked_verify.call_args.args[1],
            main._expand_audiences(main._audiences_for_request("/actions/robie_decision")),
        )

    def test_processing_card_always_carries_fallback_guidance(self):
        # The processing card replaces the buttons before any gateway owns
        # the click. If nobody patches it, the user must still see what to do.
        def texts(body):
            message = body["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]
            widgets = message["cardsV2"][0]["card"]["sections"][0]["widgets"]
            return [widget["textParagraph"]["text"] for widget in widgets]

        self.assertEqual(
            texts(main._addon_processing_response()),
            [main.PROCESSING_TEXT, main.PROCESSING_FALLBACK_TEXT],
        )
        self.assertIn("message ROBIE", main.PROCESSING_FALLBACK_TEXT)
        payload = {"chat": {"buttonClickedPayload": {"message": {"name": "spaces/9/messages/9"}}}}
        with patch.object(main, "_publish"):
            response = self._post("/actions/robie_confirmation_decision", payload, token="signed-token")
        self.assertEqual(response.status_code, 200)
        self.assertIn(main.PROCESSING_FALLBACK_TEXT, texts(response.get_json()))

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

    def test_action_audience_adds_only_the_request_path(self):
        os.environ["ROBIE_CHAT_BRIDGE_AUDIENCE"] = (
            "https://bridge.example,1234567890,https://bridge.example/actions/already"
        )
        action = "/actions/robie_confirmation_decision"
        self.assertEqual(
            main._audiences_for_request(action),
            [
                "https://bridge.example",
                "1234567890",
                "https://bridge.example/actions/already",
                "https://bridge.example" + action,
            ],
        )
        self.assertEqual(
            main._audiences_for_request("/"),
            [
                "https://bridge.example",
                "1234567890",
                "https://bridge.example/actions/already",
            ],
        )
        self.assertNotIn(
            "https://bridge.example/actions/robie_decision",
            main._audiences_for_request(action),
        )

    def _publish_attrs(self, event, event_type):
        with patch.object(main.publisher, "publish") as publish:
            publish.return_value.result.return_value = "mid"
            main._publish(event, event_type)
        self.assertEqual(publish.call_args.args[0], main.TOPIC_PATH)
        return publish.call_args.kwargs

    def test_click_routing_attribute_is_test_or_prod_only(self):
        clicked = "google.workspace.chat.card.v1.clicked"
        test_attrs = self._publish_attrs(
            {
                "type": "CARD_CLICKED",
                "common": {"parameters": {"robie_env": "test", "decision_token": "secret"}},
            },
            clicked,
        )
        self.assertEqual(test_attrs["ce-type"], clicked)
        self.assertEqual(test_attrs["robie_env"], "test")
        self.assertNotIn("decision_token", test_attrs)

        prod_attrs = self._publish_attrs(
            {
                "type": "CARD_CLICKED",
                "common": {
                    "parameters": [
                        {"key": "robie_env", "value": "prod"},
                        {"key": "decision_token", "value": "secret"},
                    ]
                },
            },
            clicked,
        )
        self.assertEqual(prod_attrs["robie_env"], "prod")

        for value in ("", "staging", "TEST", "production"):
            attrs = self._publish_attrs(
                {"type": "CARD_CLICKED", "common": {"parameters": {"robie_env": value}}},
                clicked,
            )
            self.assertNotIn("robie_env", attrs, msg=value)
            self.assertEqual(attrs["ce-type"], clicked)

        missing = self._publish_attrs(
            {"type": "CARD_CLICKED", "common": {"parameters": {"decision_token": "secret"}}},
            clicked,
        )
        self.assertNotIn("robie_env", missing)

    def test_non_click_messages_are_not_routed_by_robie_env(self):
        received = "google.workspace.chat.event.v1.received"
        attrs = self._publish_attrs(
            {
                "type": "MESSAGE",
                "message": {"name": "spaces/1/messages/1"},
                "common": {"parameters": {"robie_env": "test"}},
            },
            received,
        )
        self.assertEqual(attrs, {"ce-type": received})
        self.assertNotIn("robie_env", attrs)

    def test_listed_space_messages_get_the_test_tag(self):
        received = "google.workspace.chat.event.v1.received"
        with patch.dict(
            os.environ,
            {"ROBIE_TEST_CHAT_SPACES": "spaces/TESTSPACE, other"},
        ):
            listed = self._publish_attrs(
                {
                    "type": "MESSAGE",
                    "space": {"name": "spaces/TESTSPACE"},
                    "message": {"name": "spaces/TESTSPACE/messages/9", "text": "hello"},
                },
                received,
            )
            bare = self._publish_attrs(
                {
                    "type": "MESSAGE",
                    "message": {"name": "spaces/other/messages/2", "text": "hello"},
                },
                received,
            )
            outsider = self._publish_attrs(
                {
                    "type": "MESSAGE",
                    "space": {"name": "spaces/PRODSPACE"},
                    "message": {"name": "spaces/PRODSPACE/messages/3", "text": "hello"},
                    "common": {"parameters": {"robie_env": "test"}},
                },
                received,
            )
        self.assertEqual(listed["robie_env"], "test")
        self.assertEqual(bare["robie_env"], "test")
        self.assertNotIn("robie_env", outsider)
        self.assertEqual(outsider, {"ce-type": received})

    def test_empty_space_list_leaves_messages_untagged(self):
        received = "google.workspace.chat.event.v1.received"
        with patch.dict(os.environ, {"ROBIE_TEST_CHAT_SPACES": "  ,  "}):
            attrs = self._publish_attrs(
                {
                    "type": "MESSAGE",
                    "space": {"name": "spaces/TESTSPACE"},
                    "message": {"name": "spaces/TESTSPACE/messages/1", "text": "hello"},
                },
                received,
            )
        self.assertNotIn("robie_env", attrs)

    def test_listed_space_does_not_override_card_click_routing(self):
        clicked = "google.workspace.chat.card.v1.clicked"
        with patch.dict(os.environ, {"ROBIE_TEST_CHAT_SPACES": "spaces/TESTSPACE"}):
            untagged = self._publish_attrs(
                {
                    "type": "CARD_CLICKED",
                    "space": {"name": "spaces/TESTSPACE"},
                    "common": {"parameters": {"decision_token": "secret"}},
                },
                clicked,
            )
            prod = self._publish_attrs(
                {
                    "type": "CARD_CLICKED",
                    "space": {"name": "spaces/TESTSPACE"},
                    "common": {"parameters": {"robie_env": "prod"}},
                },
                clicked,
            )
        self.assertNotIn("robie_env", untagged)
        self.assertEqual(prod["robie_env"], "prod")

    def test_forwarded_click_publishes_button_robie_env(self):
        payload = {
            "chat": {
                "buttonClickedPayload": {
                    "message": {"name": "spaces/1/messages/1"},
                    "action": {
                        "parameters": [
                            {"key": "robie_env", "value": "test"},
                            {"key": "decision_token", "value": "secret-token"},
                        ]
                    },
                }
            }
        }
        with patch.object(main.publisher, "publish") as publish:
            publish.return_value.result.return_value = "mid"
            response = self._post("/actions/robie_confirmation_decision", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(publish.call_args.kwargs["robie_env"], "test")
        self.assertEqual(
            publish.call_args.kwargs["ce-type"],
            "google.workspace.chat.card.v1.clicked",
        )
        body = publish.call_args.args[1].decode("utf-8")
        self.assertIn("secret-token", body)
        self.assertNotIn("secret-token", str(publish.call_args.kwargs))

    def test_forwarded_message_omits_robie_env_attribute(self):
        payload = {
            "type": "MESSAGE",
            "message": {"name": "spaces/1/messages/1", "text": "hello"},
            "common": {"parameters": {"robie_env": "test"}},
        }
        with patch.object(main.publisher, "publish") as publish:
            publish.return_value.result.return_value = "mid"
            response = self._post("/", payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("robie_env", publish.call_args.kwargs)

    def test_robie_test_marker_publishes_test_and_plain_text_does_not(self):
        received = "google.workspace.chat.event.v1.received"
        marked = self._publish_attrs(
            {
                "type": "MESSAGE",
                "space": {"name": "spaces/AAQAZbLJO78"},
                "message": {
                    "name": "spaces/AAQAZbLJO78/messages/1",
                    "text": "[[robie-test]] add a note for Buster Brown",
                },
            },
            received,
        )
        self.assertEqual(marked["robie_env"], "test")
        prefixed = self._publish_attrs(
            {
                "type": "MESSAGE",
                "message": {
                    "name": "spaces/AAQAZbLJO78/messages/2",
                    "text": "robie-test: look up john smith",
                },
            },
            received,
        )
        self.assertEqual(prefixed["robie_env"], "test")
        plain = self._publish_attrs(
            {
                "type": "MESSAGE",
                "space": {"name": "spaces/AAQAZbLJO78"},
                "message": {
                    "name": "spaces/AAQAZbLJO78/messages/3",
                    "text": "add a note for Buster Brown",
                },
            },
            received,
        )
        self.assertNotIn("robie_env", plain)
        self.assertEqual(plain, {"ce-type": received})


def _signing_material():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bridge-test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return private_pem, cert_pem


class SignedClaimTests(unittest.TestCase):
    """Auth policy against real RS256 verification, not a stubbed verifier.

    Google's cert fetch is replaced with a test certificate. Signature,
    expiry, audience, and issuer checks still run through google-auth.
    """

    ACTION = "/actions/robie_confirmation_decision"

    @classmethod
    def setUpClass(cls):
        cls.private_pem, cls.cert_pem = _signing_material()

    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {
                "ROBIE_CHAT_BRIDGE_AUDIENCE": AUDIENCE,
                "ROBIE_CHAT_BRIDGE_SERVICE_ACCOUNT_EMAILS": f"{ADDON_SA},{CHAT_SA}",
            },
        )
        self._env.start()
        self._certs = patch(
            "google.oauth2.id_token._fetch_certs",
            return_value={"test-key": self.cert_pem},
        )
        self._certs.start()

    def tearDown(self):
        self._certs.stop()
        self._env.stop()

    def _token(self, **overrides) -> str:
        from google.auth import jwt as google_jwt
        from google.auth.crypt import RSASigner

        moment = int(time.time())
        payload = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE + self.ACTION,
            "azp": ADDON_SA,
            "email": ADDON_SA,
            "email_verified": True,
            "sub": "112233445566778899",
            "iat": moment,
            "exp": moment + 3600,
        }
        payload.update(overrides)
        signer = RSASigner.from_string(self.private_pem, key_id="test-key")
        return google_jwt.encode(signer, payload).decode("ascii")

    def _post(self, token, path=None):
        return main.app.test_client().post(
            path or self.ACTION,
            json={
                "chat": {
                    "buttonClickedPayload": {
                        "space": {"name": "spaces/1"},
                        "message": {"name": "spaces/1/messages/1"},
                    }
                }
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_addon_click_with_full_action_url_aud_is_accepted(self):
        token = self._token()
        with patch.object(main, "_publish") as publish:
            response = self._post(token)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        message = body["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]
        self.assertEqual(message["cardsV2"][0]["cardId"], "robie-processing")
        publish.assert_called_once()
        self.assertNotIn(token, response.get_data(as_text=True))

    def test_explicit_full_action_url_in_env_is_accepted(self):
        full = AUDIENCE + self.ACTION
        os.environ["ROBIE_CHAT_BRIDGE_AUDIENCE"] = full
        token = self._token(aud=full)
        with patch.object(main, "_publish") as publish:
            response = self._post(token)
        self.assertEqual(response.status_code, 200)
        publish.assert_called_once()

    def test_wrong_aud_is_rejected_and_logged_without_the_token(self):
        token = self._token(aud="https://evil.example/actions/robie_confirmation_decision")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post(token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        logged = "\n".join(logs.output)
        self.assertIn("reason=audience_mismatch", logged)
        self.assertIn("aud=https://evil.example/actions/robie_confirmation_decision", logged)
        self.assertIn("iss=https://accounts.google.com", logged)
        self.assertIn(f"email={ADDON_SA}", logged)
        self.assertNotIn(token, logged)
        self.assertNotIn(token, response.get_data(as_text=True))

    def test_different_action_path_is_rejected(self):
        token = self._token(aud=AUDIENCE + "/actions/robie_decision")
        with patch.object(main, "_publish") as publish:
            response = self._post(token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()

    def test_wrong_issuer_is_rejected(self):
        token = self._token(iss="https://evil.example")
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post(token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        logged = "\n".join(logs.output)
        self.assertIn("reason=issuer_unexpected", logged)
        self.assertIn("iss=https://evil.example", logged)
        self.assertNotIn(token, logged)

    def test_email_not_on_allowlist_is_rejected(self):
        intruder = "intruder@example.gserviceaccount.com"
        token = self._token(email=intruder, azp=intruder)
        with patch.object(main, "_publish") as publish:
            with self.assertLogs("robie-chat-http-bridge", level="INFO") as logs:
                response = self._post(token)
        self.assertEqual(response.status_code, 401)
        publish.assert_not_called()
        logged = "\n".join(logs.output)
        self.assertIn("reason=unexpected_bearer_identity", logged)
        self.assertIn(f"email={intruder}", logged)
        self.assertIn("aud=" + AUDIENCE + self.ACTION, logged)
        self.assertNotIn(token, logged)


if __name__ == "__main__":
    unittest.main()
