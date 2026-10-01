"""Synthetic QBO credentials and HTTP only; no live services or secrets."""
import json
import os
import unittest
from unittest.mock import patch
from applied_pay import qbo_client as client


class Response:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.body).encode()


class EnvironmentSafety(unittest.TestCase):
    def secrets(self, environment):
        values = {"environment": environment, "client_id": "synthetic-client",
                  "client_secret": "synthetic-secret", "realm_id": "synthetic-realm",
                  "refresh_token": "synthetic-refresh"}
        def get(name):
            prefix = "qbo_" + environment + "_"
            if not name.startswith(prefix): raise AssertionError("wrong environment secret")
            return values[name[len(prefix):]]
        return get

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(client, "_sec")
    def test_unset_selection_stops_before_secret_read(self, sec):
        with self.assertRaises(ValueError): client.QBO()
        sec.assert_not_called()

    @patch.object(client, "_sec")
    def test_bad_selection_stops_before_secret_read(self, sec):
        for env in ("", "Sandbox", "prod", " sandbox", "test"):
            with self.assertRaises(ValueError): client.QBO(env)
        sec.assert_not_called()

    @patch.dict(os.environ, {"APPLIED_QBO_ENVIRONMENT": "sandbox"}, clear=True)
    def test_sandbox_host_and_secret_names(self):
        with patch.object(client, "_sec", side_effect=self.secrets("sandbox")) as sec:
            q = client.QBO()
        self.assertEqual("https://sandbox-quickbooks.api.intuit.com/v3/company/synthetic-realm", q.base)
        self.assertFalse(q.allow_token_writeback)
        self.assertTrue(all(c.args[0].startswith("qbo_sandbox_") for c in sec.call_args_list))

    def test_explicit_production_uses_only_production(self):
        with patch.object(client, "_sec", side_effect=self.secrets("production")):
            q = client.QBO("production", allow_token_writeback=False)
        self.assertEqual("https://quickbooks.api.intuit.com/v3/company/synthetic-realm", q.base)

    @patch.object(client, "_sec", return_value="production")
    def test_environment_mismatch_stops(self, sec):
        with self.assertRaises(ValueError): client.QBO("sandbox")
        self.assertEqual(["qbo_sandbox_environment"], [c.args[0] for c in sec.call_args_list])

    @patch.object(client, "_sec", side_effect=RuntimeError("missing sandbox secret"))
    def test_missing_sandbox_never_falls_back(self, sec):
        with self.assertRaises(RuntimeError): client.QBO("sandbox")
        self.assertEqual(["qbo_sandbox_environment"], [c.args[0] for c in sec.call_args_list])

    @patch.dict(os.environ, {"APPLIED_QBO_ALLOW_TOKEN_WRITEBACK": "yes"}, clear=True)
    @patch.object(client, "_sec")
    def test_invalid_approval_flag_stops(self, sec):
        with self.assertRaises(ValueError): client.QBO("sandbox")
        sec.assert_not_called()

    def test_non_boolean_override_refused(self):
        with self.assertRaises(ValueError): client.QBO("sandbox", allow_token_writeback="false")

    @patch.object(client.urllib.request, "urlopen")
    @patch.object(client, "_add_version")
    def test_without_approval_no_refresh_http_or_write(self, add, http):
        with patch.object(client, "_sec", side_effect=self.secrets("sandbox")):
            q = client.QBO("sandbox", allow_token_writeback=False)
            with self.assertRaises(PermissionError): q.query("SELECT * FROM Deposit")
        http.assert_not_called(); add.assert_not_called()

    @patch.object(client, "_add_version")
    def test_approved_refresh_writes_sandbox_only_and_get_query(self, add):
        responses = [Response({"access_token":"synthetic-access", "refresh_token":"synthetic-rotated"}),
                     Response({"QueryResponse":{"Deposit":[]}})]
        with patch.object(client, "_sec", side_effect=self.secrets("sandbox")), \
             patch.object(client.urllib.request, "urlopen", side_effect=responses) as http:
            q = client.QBO("sandbox", allow_token_writeback=True)
            self.assertEqual([], q.query("SELECT * FROM Deposit"))
        add.assert_called_once_with("qbo_sandbox_refresh_token", "synthetic-rotated")
        self.assertEqual("POST", http.call_args_list[0].args[0].get_method())
        req = http.call_args_list[1].args[0]
        self.assertEqual("GET", req.get_method())
        self.assertTrue(req.full_url.startswith("https://sandbox-quickbooks.api.intuit.com/"))

    @patch.object(client, "_add_version", side_effect=RuntimeError("write failed"))
    def test_rotation_storage_failure_stops_before_query(self, add):
        with patch.object(client, "_sec", side_effect=self.secrets("sandbox")), \
             patch.object(client.urllib.request, "urlopen", return_value=Response({"access_token":"synthetic-access", "refresh_token":"synthetic-rotated"})) as http:
            q = client.QBO("sandbox", allow_token_writeback=True)
            with self.assertRaises(RuntimeError): q.query("SELECT * FROM Deposit")
        self.assertEqual(1, http.call_count)

    @patch.object(client, "_add_version")
    def test_unchanged_refresh_token_not_written(self, add):
        with patch.object(client, "_sec", side_effect=self.secrets("sandbox")), \
             patch.object(client.urllib.request, "urlopen", return_value=Response({"access_token":"synthetic-access", "refresh_token":"synthetic-refresh"})):
            q = client.QBO("sandbox", allow_token_writeback=True); q._refresh()
        add.assert_not_called()


if __name__ == "__main__": unittest.main()
