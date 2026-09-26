"""Read-only Gmail adapter for the certificates intake worker.

Talks to the Gmail REST API with hand-rolled ``requests`` calls (the
sandbox/box proxy breaks ``googleapiclient.discovery`` and httplib2 — proven
2026-09-15). Authentication is domain-wide delegation for
``certificates@streetsmart.insurance`` using the service-account key material
in Secret Manager, mirroring ``robie-manual-ops/dwd_gmail.py``.

Scope is strictly ``gmail.readonly``: the adapter can never mark, move, send,
or delete. The unread flag is never the processing ledger (see
``cert_intake.discover_messages``); the durable SQLite checkpoint owns that.

Configuration (environment):
- ``CERT_GMAIL_MAILBOX`` — default ``certificates@streetsmart.insurance``
- ``CERT_GMAIL_SA_KEY_PATH`` — on-disk SA key (default
  ``~/.config/gcp/hermes-poc-key.json`` on the worker host)
- ``CERT_GMAIL_DWD_SECRET`` — Secret Manager secret holding the DWD key JSON
- ``CERT_GMAIL_PROJECT`` — GCP project (default ``streetsmart-hermes-poc``)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.request
from typing import Any

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_MAILBOX = "certificates@streetsmart.insurance"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _gcp_token(sa_key_path: str) -> str:
    """Mint a cloud-platform token from the on-disk SA key (transient)."""
    key = json.load(open(os.path.expanduser(sa_key_path)))

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    pem = "/tmp/cert_gmail_sa.pem"
    with open(pem, "w") as f:
        f.write(key["private_key"])
    os.chmod(pem, 0o600)
    try:
        now = int(time.time())
        seg = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + b64(
            json.dumps({
                "iss": key["client_email"],
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": now,
                "exp": now + 3600,
            }).encode()
        )
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem, "-binary"],
            input=seg.encode(), capture_output=True, timeout=30,
        ).stdout
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=("grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer"
                  "&assertion=" + seg + "." + b64(sig)).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    finally:
        try:
            os.remove(pem)
        except OSError:
            pass


def _dwd_key(sa_key_path: str, project: str, secret: str) -> dict[str, Any]:
    """Read the DWD key JSON from Secret Manager (transient, never logged)."""
    tok = _gcp_token(sa_key_path)
    last: Exception | None = None
    for _ in range(4):
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", "40",
                 "-H", f"Authorization: Bearer {tok}",
                 f"https://secretmanager.googleapis.com/v1/projects/{project}"
                 f"/secrets/{secret}/versions/latest:access"],
                capture_output=True, timeout=60,
            )
            payload = json.loads(out.stdout)
            return json.loads(base64.b64decode(payload["payload"]["data"]).decode())
        except Exception as exc:  # transient network/secret-manager hiccups
            last = exc
            time.sleep(3)
    raise RuntimeError(f"could not read DWD secret {secret}: {last}")


def build_dwd_session(mailbox: str = "") -> Any:
    """Return an authorized ``requests.Session`` impersonating ``mailbox``."""
    import requests  # lazy: unit tests never need it
    import google.auth.transport.requests
    from google.oauth2 import service_account

    mailbox = mailbox or _env("CERT_GMAIL_MAILBOX", DEFAULT_MAILBOX)
    dwd = _dwd_key(
        _env("CERT_GMAIL_SA_KEY_PATH", "~/.config/gcp/hermes-poc-key.json"),
        _env("CERT_GMAIL_PROJECT", "streetsmart-hermes-poc"),
        _env("CERT_GMAIL_DWD_SECRET", "accountability-google-dwd-key"),
    )
    creds = service_account.Credentials.from_service_account_info(
        dwd, scopes=[READONLY_SCOPE], subject=mailbox,
    )
    session = requests.Session()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    # stash a refresher so long runs don't die on token expiry
    session._dwd_creds = creds  # type: ignore[attr-defined]
    session._dwd_auth_req = auth_req  # type: ignore[attr-defined]
    return session


class CertGmailAdapter:
    """Implements the intake port over the Gmail REST API.

    Port (matches ``cert_intake.discover_messages`` expectations):

    - ``list_message_ids(query, page_token, page_size)``
      -> ``(list[str], next_page_token | None)``
    - ``get_full_message(gmail_id)`` -> ``format=full`` payload dict
    - ``get_attachment_bytes(gmail_id, attachment_id)`` -> bytes
    """

    def __init__(self, session: Any, mailbox: str = "") -> None:
        self._s = session
        self._mailbox = mailbox or _env("CERT_GMAIL_MAILBOX", DEFAULT_MAILBOX)

    @classmethod
    def with_dwd(cls, mailbox: str = "") -> "CertGmailAdapter":
        return cls(build_dwd_session(mailbox), mailbox)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{GMAIL_BASE}/{self._mailbox}{path}"
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self._s.get(url, params=params or {}, timeout=60)
            except Exception as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Gmail GET {path} failed: {last}")

    def _refresh(self) -> None:
        creds = getattr(self._s, "_dwd_creds", None)
        if creds is not None:
            creds.refresh(self._s._dwd_auth_req)
            self._s.headers.update({"Authorization": f"Bearer {creds.token}"})

    def list_message_ids(
        self, query: str, page_token: str | None, page_size: int = 50
    ) -> tuple[list[str], str | None]:
        params: dict[str, Any] = {"q": query, "maxResults": page_size}
        if page_token:
            params["pageToken"] = page_token
        body = self._get("/messages", params)
        ids = [m["id"] for m in body.get("messages", []) if m.get("id")]
        return ids, body.get("nextPageToken")

    def get_full_message(self, gmail_id: str) -> dict[str, Any]:
        return self._get(f"/messages/{gmail_id}", {"format": "full"})

    def get_attachment_bytes(self, gmail_id: str, attachment_id: str) -> bytes:
        body = self._get(f"/messages/{gmail_id}/attachments/{attachment_id}")
        data = (body.get("data") or "").replace("-", "+").replace("_", "/")
        return base64.b64decode(data + "=" * (-len(data) % 4))

    def attachment_fetcher(self, gmail_id: str, attachment_id: str) -> bytes:
        return self.get_attachment_bytes(gmail_id, attachment_id)
