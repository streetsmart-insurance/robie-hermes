"""Read-only QBO client for the Applied Pay matcher. Only GET/query calls. Rotated refresh
tokens are written back to Secret Manager right away so other users
of qbo_production_* keep working. Repeated unattended rotation requires explicit owner approval."""
import json, subprocess, time, urllib.parse, urllib.request, base64, sys
PROJECT = "streetsmart-hermes-poc"
def _sec(name):
    return subprocess.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}", f"--project={PROJECT}"],
                          capture_output=True, text=True, check=True).stdout.strip()
def _add_version(name, value):
    subprocess.run(["gcloud", "secrets", "versions", "add", name, f"--project={PROJECT}", "--data-file=-"],
                   input=value, capture_output=True, text=True, check=True)
class QBO:
    def __init__(self):
        self.cid, self.csec = _sec("qbo_production_client_id"), _sec("qbo_production_client_secret")
        self.realm = _sec("qbo_production_realm_id")
        self.base = f"https://quickbooks.api.intuit.com/v3/company/{self.realm}"
        self.token = None
    def _refresh(self):
        rt = _sec("qbo_production_refresh_token")   # always the latest version
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt}).encode()
        auth = base64.b64encode(f"{self.cid}:{self.csec}".encode()).decode()
        req = urllib.request.Request("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", data=body,
              headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r: j = json.load(r)
        if j.get("refresh_token") and j["refresh_token"] != rt:
            _add_version("qbo_production_refresh_token", j["refresh_token"])
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
