import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GoogleChatCardTests(unittest.TestCase):
    def test_decorated_text_widget_supports_button(self):
        adapter_code = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('if widget.get("button"):', adapter_code)
        self.assertIn('decorated["button"] = _button_to_chat(widget["button"])', adapter_code)

    def test_send_clarify_wraps_long_choices_and_patches_card(self):
        adapter_code = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("has_long_choice = any(len(str(c).strip()) > 24 for c in choices)", adapter_code)
        self.assertIn('"type": "decorated_text"', adapter_code)
        self.assertIn('"wrap_text": True', adapter_code)
        self.assertIn('"UPDATE_MESSAGE"', adapter_code)
        self.assertIn('"cardsV2": []', adapter_code)

    def test_chat_guard_autonomous_execution_directive(self):
        chat_guard_code = (ROOT / "robie_job_engine/chat_guard.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("proceed immediately with autonomous execution", chat_guard_code)
        self.assertIn("Do not ask for confirmation or call clarify before starting", chat_guard_code)


if __name__ == "__main__":
    unittest.main()
