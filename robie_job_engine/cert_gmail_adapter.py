"""Read-only Gmail adapter for the certificates intake worker.

Talks to the Gmail REST API with hand-rolled ``requests`` calls (the
sandbox/box proxy breaks ``googleapiclient.discovery`` and httplib2 — proven
2026-09-15). Authentication is domain-wide delegation for
``certificates@streetsmart.insurance`` using the service-account key material
in Secret Manager, mirroring ``robie-manual-ops/dwd_gmail.py``.

Scope is strictly ``gmail.readonly`` for intake: the adapter can never
move, send, or delete. The one narrow exception is
:meth:`CertGmailAdapter.mark_read`, which removes the UNREAD label only,
and only after a destination-proven filing (Carlo 2026-09-27: "mark as
read if it is read"). It mints a separate ``gmail.modify`` token for that
single call — the read-only intake session is never widened. The unread
flag is never the processing ledger (see
``cert_intake.discover_messages``); the durable SQLite checkpoint owns that.

``gmail.modify`` must be authorized for the DWD client in Google Workspace
Admin (Security > API controls > Domain-wide delegation); without it the
modify call returns 403 and the message simply stays unread. That is a
safe failure: the filing is already proven and checkpointed.

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
import urllib.error
import urllib.request
from typing import Any

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
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


def _key_file_dwd_token(mailbox: str, scopes: list[str]) -> str:
    """DWD token via the Secret-Manager key flow (fallback)."""
    import google.auth.transport.requests
    from google.oauth2 import service_account

    dwd = _dwd_key(
        _env("CERT_GMAIL_SA_KEY_PATH", "~/.config/gcp/hermes-poc-key.json"),
        _env("CERT_GMAIL_PROJECT", "streetsmart-hermes-poc"),
        _env("CERT_GMAIL_DWD_SECRET", "accountability-google-dwd-key"),
    )
    creds = service_account.Credentials.from_service_account_info(
        dwd, scopes=scopes, subject=mailbox,
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _keyless_dwd_token(mailbox: str, scopes: list[str]) -> str:
    """Mint a DWD access token using the VM's own service account.

    No key files: the VM SA signs a JWT asserting the delegated subject via
    the IAM signJwt API (the SA needs iam.serviceAccounts.signJwt on itself),
    then exchanges it at the OAuth token endpoint. Proven on hermes-poc-01
    2026-09-26.
    """
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest

    creds, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/iam"])
    creds.refresh(AuthRequest())
    sa_email = creds.service_account_email

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    claims = {
        "iss": sa_email,
        "sub": mailbox,
        "scope": " ".join(scopes),
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    # signJwt takes the serialized claims set; it builds the JWT header itself.
    sign_req = urllib.request.Request(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts"
        f"/{sa_email}:signJwt",
        data=json.dumps({"payload": json.dumps(claims)}).encode(),
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(sign_req, timeout=30) as r:
        signed_jwt = json.load(r)["signedJwt"]
    token_req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=("grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer"
              f"&assertion={signed_jwt}").encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(token_req, timeout=30) as r:
        return json.load(r)["access_token"]


def _modify_dwd_token(mailbox: str) -> str:
    """DWD token carrying only ``gmail.modify`` (the mark-read path).

    Separate from the read-only intake session: the intake scope is never
    widened. Tries the keyless VM-SA flow first, then the key flow.
    """
    scopes = [MODIFY_SCOPE]
    try:
        return _keyless_dwd_token(mailbox, scopes)
    except Exception:
        return _key_file_dwd_token(mailbox, scopes)


def _gmail_modify(token: str, mailbox: str, gmail_id: str,
                  body: dict[str, Any]) -> dict[str, Any]:
    """POST users.messages.modify. Raises urllib.error.HTTPError on 4xx/5xx."""
    req = urllib.request.Request(
        f"{GMAIL_BASE}/{mailbox}/messages/{gmail_id}/modify",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _gmail_label_ids(token: str, mailbox: str,
                     gmail_id: str) -> list[str]:
    """Read-back for the mark-read path: the message's current labelIds."""
    req = urllib.request.Request(
        f"{GMAIL_BASE}/{mailbox}/messages/{gmail_id}?format=minimal",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return list(json.load(r).get("labelIds") or [])


def _mark_read_uncertain(token: str, mailbox: str, gmail_id: str,
                       body: dict[str, Any],
                       first_err: Exception) -> tuple[bool, str]:
    """Read-back-first recovery for an uncertain modify outcome.

    Used for BOTH HTTP transport failures and generic exceptions
    (timeout, connection reset, DNS): the remove may have landed despite
    the lost response, so the labels are read back before any re-send.
    Re-sends only when UNREAD is verifiably still present; fails closed
    (stays unread) when the read-back itself cannot complete.
    """
    try:
        labels = _gmail_label_ids(token, mailbox, gmail_id)
    except Exception as rb_exc:
        return False, (f"modify failed ({first_err}) and label "
                       f"read-back failed: {rb_exc} — leaving unread")
    if "UNREAD" not in labels:
        return True, "ok (read-back: UNREAD already removed)"
    try:
        _gmail_modify(token, mailbox, gmail_id, body)
        return True, "ok (re-sent after read-back)"
    except Exception as exc2:
        return False, (f"modify failed after read-back: {exc2} — "
                       "leaving unread")


def mark_message_read(mailbox: str, gmail_id: str) -> tuple[bool, str]:
    """Remove UNREAD from one message. Returns ``(ok, reason)``.

    Retry discipline (Carlo's standing rule): a failed POST is never
    blind-retried — read the labels back first; re-send only when UNREAD
    is verifiably still present; fail closed (stays unread) when the
    read-back itself cannot complete. Never raises: a mark-read failure
    must not fail an already destination-proven filing.
    """
    try:
        token = _modify_dwd_token(mailbox)
    except Exception as exc:
        return False, f"could not mint modify token: {exc}"
    body = {"removeLabelIds": ["UNREAD"]}
    try:
        _gmail_modify(token, mailbox, gmail_id, body)
        return True, "ok"
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return False, (
                "gmail.modify not authorized for the DWD client — "
                "Workspace Admin must authorize the scope; leaving unread")
        # HTTP transport/server failure: read back before any re-send.
        return _mark_read_uncertain(token, mailbox, gmail_id, body, exc)
    except Exception as exc:
        # Generic transport failure (timeout, reset, DNS): the outcome is
        # uncertain — the remove may have landed. Read back first, same as
        # the HTTPError path above; never report failure without checking.
        return _mark_read_uncertain(token, mailbox, gmail_id, body, exc)


def build_dwd_session(mailbox: str = "") -> Any:
    """Return an authorized ``requests.Session`` impersonating ``mailbox``.

    Tries the keyless VM-SA flow first (the worker host), then falls back to
    the Secret-Manager key flow.
    """
    import requests  # lazy: unit tests never need it

    mailbox = mailbox or _env("CERT_GMAIL_MAILBOX", DEFAULT_MAILBOX)
    scopes = [READONLY_SCOPE]
    token = ""
    try:
        token = _keyless_dwd_token(mailbox, scopes)
    except Exception:
        token = ""
    if not token:
        token = _key_file_dwd_token(mailbox, scopes)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "_dwd_mailbox": mailbox})
    return session


class CertGmailAdapter:
    """Implements the intake port over the Gmail REST API.

    Port (matches ``cert_intake.discover_messages`` expectations):

    - ``list_message_ids(query, page_token, page_size)``
      -> ``(list[str], next_page_token | None)``
    - ``get_full_message(gmail_id)`` -> ``format=full`` payload dict
    - ``get_attachment_bytes(gmail_id, attachment_id)`` -> bytes
    - ``mark_read(gmail_id)`` -> ``(ok, reason)``; removes UNREAD after a
      destination-proven filing only (never raises)
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
        try:
            token = _keyless_dwd_token(
                self._s.headers.get("_dwd_mailbox", self._mailbox),
                [READONLY_SCOPE],
            )
            self._s.headers.update({"Authorization": f"Bearer {token}"})
        except Exception:
            pass

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

    def mark_read(self, gmail_id: str) -> tuple[bool, str]:
        """Remove the UNREAD label after a destination-proven filing.

        The sweep driver calls this only for FILED records — anything
        UNVERIFIED or errored stays unread. Returns ``(ok, reason)`` and
        never raises.
        """
        try:
            return mark_message_read(self._mailbox, gmail_id)
        except Exception as exc:  # belt and suspenders
            return False, f"{type(exc).__name__}: {exc}"
