import unittest
from unittest.mock import MagicMock, patch
from src.email_outreach.robie_inbox_cleaner import run_daily_inbox_cleanup_and_report

class TestRobieInboxCleaner(unittest.TestCase):

    @patch("src.email_outreach.robie_inbox_cleaner.run_uw_reply_filing")
    @patch("src.email_outreach.robie_inbox_cleaner.GmailRenewalClient")
    def test_cleaner_identifies_noise_and_sends_report(self, mock_client_cls, mock_filer):
        mock_filer.return_value = {"filed": 0, "skipped": 0}
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_service = MagicMock()
        mock_client.inbox_services = {"robie@streetsmart.insurance": mock_service}

        def list_side_effect(userId, q, maxResults=500):
            mock_ret = MagicMock()
            if "mailer-daemon" in q:
                mock_ret.execute.return_value = {"messages": [{"id": "m1"}, {"id": "m2"}]}
            elif "postmaster" in q:
                mock_ret.execute.return_value = {"messages": []}
            elif "Delivery Status Notification" in q:
                mock_ret.execute.return_value = {"messages": []}
            elif "David Clark" in q:
                mock_ret.execute.return_value = {"messages": [{"id": "m3"}]}
            else:
                mock_ret.execute.return_value = {
                    "messages": [{"id": "real1"}, {"id": "real2"}]
                }
            return mock_ret

        mock_service.users().messages().list.side_effect = list_side_effect

        def get_side_effect(userId, id, format="full"):
            mock_ret = MagicMock()
            if id == "real1":
                mock_ret.execute.return_value = {
                    "id": "real1",
                    "snippet": "We have attached the renewal quote for Advance Marble.",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "underwriter@trinity.com"},
                            {"name": "Subject", "value": "Quote for Advance Marble"},
                            {"name": "Date", "value": "Thu, 3 Sep 2026 14:00:00 -0400"}
                        ]
                    }
                }
            elif id == "real2":
                mock_ret.execute.return_value = {
                    "id": "real2",
                    "snippet": "Robie Daily Mailbox Audit & Cleanup Report",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "robie@streetsmart.insurance"},
                            {"name": "Subject", "value": "Robie Daily Mailbox Audit"},
                            {"name": "Date", "value": "Thu, 3 Sep 2026 15:00:00 -0400"}
                        ]
                    }
                }
            return mock_ret

        mock_service.users().messages().get.side_effect = get_side_effect
        mock_service.users().messages().trash().execute.return_value = {}
        mock_client.send_email.return_value = {"id": "sent_report_123"}

        res = run_daily_inbox_cleanup_and_report(
            client=mock_client,
            recipient="carlo@streetsmart.insurance",
            dry_run=False
        )

        self.assertEqual(res["noise_trashed"], 3)
        self.assertEqual(res["real_messages_count"], 1)
        self.assertEqual(res["real_messages"][0]["from"], "underwriter@trinity.com")
        self.assertTrue(res["report_sent"])
        mock_filer.assert_called_once()
        self.assertEqual(res.get("uw_replies_filed"), 0)
        mock_client.send_email.assert_called_once()
        args, kwargs = mock_client.send_email.call_args
        self.assertEqual(kwargs["to_email"], "carlo@streetsmart.insurance")
        self.assertIn("Robie Daily Mailbox Audit", kwargs["subject"])

    def test_gmail_client_mark_read_and_trash(self):
        from src.email_outreach.gmail_client import GmailRenewalClient
        client = GmailRenewalClient.__new__(GmailRenewalClient)
        mock_svc = MagicMock()
        users_mock = MagicMock()
        messages_mock = MagicMock()
        mock_svc.users.return_value = users_mock
        users_mock.messages.return_value = messages_mock
        client.inbox_services = {"robie@streetsmart.insurance": mock_svc}

        # Test mark single message read
        res = client.mark_message_read("msg1", "robie@streetsmart.insurance")
        self.assertTrue(res)
        messages_mock.modify.assert_called_with(
            userId="me", id="msg1", body={"removeLabelIds": ["UNREAD"]}
        )

        # Test batch mark read
        batch_res = client.batch_mark_read(["msg1", "msg2"], "robie@streetsmart.insurance")
        self.assertEqual(batch_res, 2)
        messages_mock.batchModify.assert_called_with(
            userId="me", body={"ids": ["msg1", "msg2"], "removeLabelIds": ["UNREAD"]}
        )

        # Test trash message
        trash_res = client.trash_message("noise1", "robie@streetsmart.insurance")
        self.assertTrue(trash_res)
        messages_mock.trash.assert_called_with(userId="me", id="noise1")


if __name__ == "__main__":
    unittest.main()

