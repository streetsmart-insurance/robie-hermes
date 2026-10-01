"""QBO query-only client with explicit environment and OAuth-rotation gates.

No default environment and no production fallback. OAuth refresh may rotate a
credential even without a QBO posting, so both refresh and credential write-back
require a separately approved deployment flag. This code does not grant consent.
"""
import json, os, subprocess, time, urllib.parse, urllib.request, base64, sys
PROJECT = "streetsmart-hermes-poc"
def _sec(name):
    return subprocess.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}", f"--project={PROJECT}"],
                          capture_output=True, text=True, check=True).stdout.strip()
def _add_version(name, value):
    subprocess.run(["gcloud", "secrets", "versions", "add", name, f"--project={PROJECT}", "--data-file=-"],
                   input=value, capture_output=True, text=True, check=True)
class QBO:
    def __init__(self, environment=None, *, allow_token_writeback=None):
        # Select explicitly before reading any secret. Never infer production.
        environment = environment if environment is not None else os.environ.get("APPLIED_QBO_ENVIRONMENT")
        if environment not in ("sandbox", "production"):
            raise ValueError("APPLIED_QBO_ENVIRONMENT must explicitly be sandbox or production; no fallback")
        self.environment = environment
        self.secret_prefix = "qbo_" + environment + "_"
        if allow_token_writeback is None:
            flag = os.environ.get("APPLIED_QBO_ALLOW_TOKEN_WRITEBACK", "false")
            if flag not in ("true", "false"):
                raise ValueError("APPLIED_QBO_ALLOW_TOKEN_WRITEBACK must be true or false")
            allow_token_writeback = flag == "true"
        if type(allow_token_writeback) is not bool:
            raise ValueError("allow_token_writeback must be a boolean")
        self.allow_token_writeback = allow_token_writeback
        stored_environment = _sec(self.secret_prefix + "environment")
        if stored_environment != environment:
            raise ValueError("QBO environment secret does not match explicit selection; stopped")
        self.cid = _sec(self.secret_prefix + "client_id")
        self.csec = _sec(self.secret_prefix + "client_secret")
        self.realm = _sec(self.secret_prefix + "realm_id")
        host = "sandbox-quickbooks.api.intuit.com" if environment == "sandbox" else "quickbooks.api.intuit.com"
        self.base = f"https://{host}/v3/company/{self.realm}"
        self.token = None
    def _refresh(self):
        # Refresh itself may invalidate the saved credential. Do not start it
        # without approval to preserve a rotated token in this environment.
        if not self.allow_token_writeback:
            raise PermissionError("OAuth refresh/rotation and token write-back need separate approval; no request made")
        rt = _sec(self.secret_prefix + "refresh_token")
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt}).encode()
        auth = base64.b64encode(f"{self.cid}:{self.csec}".encode()).decode()
        req = urllib.request.Request("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", data=body,
              headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r: j = json.load(r)
        if j.get("refresh_token") and j["refresh_token"] != rt:
            _add_version(self.secret_prefix + "refresh_token", j["refresh_token"])
            print("[qbo] refresh token rotated; new version written back", file=sys.stderr)
        self.token = j["access_token"]
    def query(self, q, page=1000):
        if not self.token: self._refresh()
        out, start = [], 1
        while True:
            url = f"{self.base}/query?minorversion=75&query=" + urllib.parse.quote(f"{q} STARTPOSITION {start} MAXRESULTS {page}")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r: res = json.load(r)["QueryResponse"]
            rows = next((v for k, v in res.items() if isinstance(v, list)), [])
            out += rows
            if len(rows) < page: return out
            start += page
