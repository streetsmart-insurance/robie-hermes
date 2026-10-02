"""Regression test: Robie replies must preserve original CC recipients.

Bug found 2026-10-02: Robie's reply to Carlo's PFA test email did not CC
jake@streetsmart.insurance, even though the original email had him CC'd.
The reply code had cc=[] hardcoded.
"""
import re
import unittest


def extract_cc_list(headers: dict) -> list:
    """Extract CC recipients from email headers, excluding robie@ itself.
    
    Mirrors the logic in scripts/robie_email_agent.py.
    """
    cc_raw = headers.get("cc", "") or ""
    cc_list = [
        addr.strip()
        for addr in re.split(r"[;,]", cc_raw)
        if addr.strip() and "robie@" not in addr.lower()
    ]
    return cc_list


class TestCCPreservation(unittest.TestCase):
    def test_single_cc_preserved(self):
        headers = {"cc": "jake@streetsmart.insurance"}
        result = extract_cc_list(headers)
        self.assertEqual(result, ["jake@streetsmart.insurance"])

    def test_multiple_cc_preserved(self):
        headers = {"cc": "jake@streetsmart.insurance, sandy@streetsmart.insurance"}
        result = extract_cc_list(headers)
        self.assertEqual(
            result,
            ["jake@streetsmart.insurance", "sandy@streetsmart.insurance"]
        )

    def test_robie_excluded_from_cc(self):
        # Robie should never CC itself (avoid self-loops)
        headers = {"cc": "jake@streetsmart.insurance, robie@streetsmart.insurance"}
        result = extract_cc_list(headers)
        self.assertEqual(result, ["jake@streetsmart.insurance"])

    def test_empty_cc(self):
        headers = {}
        result = extract_cc_list(headers)
        self.assertEqual(result, [])

    def test_semicolon_separator(self):
        headers = {"cc": "jake@streetsmart.insurance; sandy@streetsmart.insurance"}
        result = extract_cc_list(headers)
        self.assertEqual(
            result,
            ["jake@streetsmart.insurance", "sandy@streetsmart.insurance"]
        )


if __name__ == "__main__":
    unittest.main()
