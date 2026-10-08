"""EZLynx API client for the Bland dispatcher.

Three API surfaces, all server-friendly (no browser needed):

1. Classic Web Services (services.ezlynx.com) — EZToken auth via
   EZUser / EZPassword / EZAppSecret headers:
   - GET /ezlynxapi/api/Applicant/v2/{id}            -> applicant profile
   - GET /ezlynxapi/api/policy/applicant/{id}/{from}/{to} -> policies

2. Discussion API (app.ezlynx.com/DiscussionApi) — OAuth2 client-credentials-ish
   grant (client_id, client_secret, username, integration_group_id):
   - GET v8/discussions/by-applicant?applicantId={id} -> discussions + notes
   - POST v8/discussions/{id}/notes {"type":"Note","body":...} -> append note
   NOTE: the Discussion API refuses bodies containing phone-number-like
   values, so writeback notes must not include literal phone numbers.

3. Document API (app.ezlynx.com/DocumentApi) — same OAuth2 token:
   - POST /DocumentApi/documents/v1/account/{id}/document (multipart:
     DocumentName, File, PolicyMasterId) -> numeric document id

Credentials come from environment variables (see config.py). Nothing is
hardcoded.
"""
import logging
import time
from typing import Any, Dict, List, Optional
from urllib import parse

import requests

from config import Config

logger = logging.getLogger("bland_dispatcher.ezlynx")

CLASSIC_BASE = "https://services.ezlynx.com/ezlynxapi/api"
DISCUSSION_BASE = "https://app.ezlynx.com/DiscussionApi"
DOCUMENT_BASE = "https://app.ezlynx.com"

# Exceptions worth retrying with backoff (never retry other 4xx).
_TRANSIENT_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


def _with_retries(op, *, tries=3, what="request"):
    """Run op() — a zero-arg callable returning a requests Response — with
    retries on transient failures.

    Retries timeouts, connection errors, HTTP 429 and 5xx with exponential
    backoff (1s, 2s, 4s... capped at 8s). Returns the final Response, or
    None when every attempt failed transiently. Other 4xx responses are
    returned immediately (not retried).
    """
    for i in range(tries):
        try:
            resp = op()
        except _TRANSIENT_EXC as e:
            logger.warning("%s transient error (%s); retry %d/%d",
                           what, e, i + 1, tries)
            time.sleep(min(2 ** i, 8))
            continue
        except Exception as e:
            logger.error("%s unexpected error: %s", what, e)
            return None
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            logger.warning("%s HTTP %s; retry %d/%d",
                           what, resp.status_code, i + 1, tries)
            time.sleep(min(2 ** i, 8))
            continue
        return resp
    logger.error("%s failed after %d attempts", what, tries)
    return None


class EZLynxClient:
    def __init__(self, cfg: Optional[type] = None):
        self.cfg = cfg or Config
        self._eztoken: Optional[str] = None
        self._eztoken_at: float = 0.0
        self._oauth_token: Optional[str] = None
        self._oauth_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Classic auth (EZToken)
    # ------------------------------------------------------------------
    def _classic_token(self, force: bool = False) -> Optional[str]:
        if (
            not force
            and self._eztoken
            and time.time() - self._eztoken_at < 900
        ):
            return self._eztoken
        if not (self.cfg.EZLYNX_USER and self.cfg.EZLYNX_PASSWORD and self.cfg.EZLYNX_APP_SECRET):
            logger.error("EZLynx Classic credentials not configured")
            return None
        try:
            resp = requests.get(
                f"{CLASSIC_BASE}/authenticate",
                headers={
                    "EZUser": self.cfg.EZLYNX_USER,
                    "EZPassword": self.cfg.EZLYNX_PASSWORD,
                    "EZAppSecret": self.cfg.EZLYNX_APP_SECRET,
                    "EZToken": "authenticate",
                },
                timeout=20,
            )
            token = resp.headers.get("EZToken")
            if token:
                self._eztoken = token
                self._eztoken_at = time.time()
                return token
            logger.error("Classic auth failed: HTTP %s", resp.status_code)
        except Exception as e:
            logger.error("Classic auth exception: %s", e)
        return None

    def _classic_headers(self) -> Optional[Dict[str, str]]:
        token = self._classic_token()
        if not token:
            return None
        return {
            "EZUser": self.cfg.EZLYNX_USER,
            "EZPassword": self.cfg.EZLYNX_PASSWORD,
            "EZAppSecret": self.cfg.EZLYNX_APP_SECRET,
            "EZToken": token,
        }

    # ------------------------------------------------------------------
    # OAuth2 (Discussion + Document APIs)
    # ------------------------------------------------------------------
    def _oauth_token_get(self, force: bool = False) -> Optional[str]:
        if not force and self._oauth_token and time.time() < self._oauth_expires_at - 60:
            return self._oauth_token
        c = self.cfg
        if not (c.EZLYNX_OAUTH_CLIENT_ID and c.EZLYNX_OAUTH_CLIENT_SECRET
                and c.EZLYNX_OAUTH_USERNAME and c.EZLYNX_INTEGRATION_GROUP_ID):
            logger.error("EZLynx OAuth credentials not configured")
            return None
        try:
            resp = requests.post(
                c.EZLYNX_CONNECT_TOKEN_URL,
                data={
                    "client_id": c.EZLYNX_OAUTH_CLIENT_ID,
                    "client_secret": c.EZLYNX_OAUTH_CLIENT_SECRET,
                    "grant_type": "vendor_data_access",
                    "scope": "DiscussionApi openid",
                    "username": c.EZLYNX_OAUTH_USERNAME,
                    "integration_group_id": c.EZLYNX_INTEGRATION_GROUP_ID,
                },
                timeout=20,
            )
            body = resp.json()
            token = body.get("access_token")
            if token:
                self._oauth_token = token
                self._oauth_expires_at = time.time() + int(body.get("expires_in", 3600))
                return token
            logger.error("OAuth token failed: HTTP %s", resp.status_code)
        except Exception as e:
            logger.error("OAuth token exception: %s", e)
        return None

    # ------------------------------------------------------------------
    # Applicant + policies (Classic)
    # ------------------------------------------------------------------
    def get_applicant(self, applicant_id: str) -> Dict[str, Any]:
        headers = self._classic_headers()
        if not headers:
            return {"status": "error", "error": "classic auth failed"}
        url = (f"{CLASSIC_BASE}/Applicant/v2/"
               f"{parse.quote(str(applicant_id), safe='')}")
        resp = _with_retries(
            lambda: requests.get(url, headers=headers, timeout=20),
            what=f"get_applicant({applicant_id})",
        )
        if resp is None:
            return {"status": "error", "error": "request failed after retries"}
        if resp.status_code == 200:
            return {"status": "success", "applicant": resp.json()}
        return {"status": "error", "code": resp.status_code, "error": resp.text[:300]}

    def get_applicant_policies(self, applicant_id: str) -> Dict[str, Any]:
        headers = self._classic_headers()
        if not headers:
            return {"status": "error", "error": "classic auth failed"}
        url = (f"{CLASSIC_BASE}/policy/applicant/"
               f"{parse.quote(str(applicant_id), safe='')}/2020-01-01/2027-12-31")
        resp = _with_retries(
            lambda: requests.get(url, headers=headers, timeout=20),
            what=f"get_applicant_policies({applicant_id})",
        )
        if resp is None:
            return {"status": "error", "error": "request failed after retries"}
        if resp.status_code == 200:
            policies = resp.json()
            return {
                "status": "success",
                "policies": policies if isinstance(policies, list) else [],
            }
        return {"status": "error", "code": resp.status_code, "error": resp.text[:300]}

    # ------------------------------------------------------------------
    # Discussions / notes (Discussion API)
    #
    # PROVEN 2026-10-03 (live probe from hermes-poc-01): the v8 endpoint
    # below is the ONLY working discussion surface. The v1 endpoint
    # (/DiscussionApi/discussion/v1/applicant/{id}) returns HTTP 404.
    # v8 discussion objects carry exactly 13 fields: applicantId, created,
    # createdById, deleted, discussionId, lastModified, lastModifiedById,
    # mostRecentNoteId, noteCount, opportunityId, organizationId, title,
    # watcherUserIds. NOTE: there are NO label fields and NO note bodies —
    # the API never returns either. Label detection stays in the Zapier
    # Zaps; the freeform instruction comes from the discussion TITLE.
    # ------------------------------------------------------------------
    def get_discussions(self, applicant_id: str) -> List[Dict[str, Any]]:
        def _fetch(token: str):
            return _with_retries(
                lambda: requests.get(
                    f"{DISCUSSION_BASE}/v8/discussions/by-applicant",
                    params={"applicantId": applicant_id},
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json"},
                    timeout=20,
                ),
                what=f"get_discussions({applicant_id})",
            )

        token = self._oauth_token_get()
        if not token:
            return []
        resp = _fetch(token)
        if resp is not None and resp.status_code in (401, 403):
            # Token may be stale or have lost its grant — force a refresh
            # once before giving up (this was the 2026-10-03 SSRobie fix
            # class of failure).
            logger.warning("get_discussions HTTP %s; forcing OAuth refresh",
                           resp.status_code)
            token = self._oauth_token_get(force=True)
            if token:
                resp = _fetch(token)
        if resp is None:
            return []
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("discussions", "data", "items", "results"):
                    if isinstance(data.get(key), list):
                        return data[key]
        logger.warning("get_discussions HTTP %s", resp.status_code)
        return []

    @staticmethod
    def discussion_id_of(disc: Dict[str, Any]) -> str:
        """Normalize the v8 discussion id field."""
        return str(disc.get("discussionId") or disc.get("id") or "").strip()

    def get_discussion_by_id(
        self, applicant_id: str, discussion_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find one discussion by id (v8 has no single-discussion GET; filter client-side)."""
        target = (discussion_id or "").strip()
        if not target:
            return None
        for disc in self.get_discussions(applicant_id):
            if self.discussion_id_of(disc) == target:
                return disc
        return None

    def most_recent_discussion(
        self, applicant_id: str
    ) -> Optional[Dict[str, Any]]:
        """The discussion with the latest lastModified (v8 field)."""
        best = None
        best_ts = ""
        for disc in self.get_discussions(applicant_id):
            ts = str(disc.get("lastModified") or "")
            if ts >= best_ts:
                best, best_ts = disc, ts
        return best

    def append_note(self, discussion_id: str, body: str) -> Dict[str, Any]:
        token = self._oauth_token_get()
        if not token:
            return {"status": "error", "error": "oauth failed"}
        url = (f"{DISCUSSION_BASE}/v8/discussions/"
               f"{parse.quote(str(discussion_id), safe='')}/notes")
        resp = _with_retries(
            lambda: requests.post(
                url,
                json={"type": "Note", "body": body},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                timeout=20,
            ),
            what=f"append_note({discussion_id})",
        )
        if resp is None:
            return {"status": "error", "error": "request failed after retries"}
        if resp.status_code in (200, 201):
            return {"status": "success", "data": resp.json()}
        return {"status": "error", "code": resp.status_code, "error": resp.text[:300]}

    # ------------------------------------------------------------------
    # Document upload (Document API) — for the call MP3
    # ------------------------------------------------------------------
    def upload_document(
        self, applicant_id: str, document_name: str, file_bytes: bytes,
        content_type: str = "audio/mpeg",
    ) -> Dict[str, Any]:
        token = self._oauth_token_get()
        if not token:
            return {"status": "error", "error": "oauth failed"}
        url = (f"{DOCUMENT_BASE}/DocumentApi/documents/v1/account/"
               f"{parse.quote(str(applicant_id), safe='')}/document")
        files = {
            "DocumentName": (None, document_name),
            "PolicyMasterId": (None, "0"),
            "File": (document_name, file_bytes, content_type),
        }
        resp = _with_retries(
            lambda: requests.post(
                url,
                files=files,
                headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"},
                timeout=60,
            ),
            what=f"upload_document({applicant_id})",
        )
        if resp is None:
            return {"status": "error", "error": "request failed after retries"}
        if resp.status_code in (200, 201):
            return {"status": "success", "document_id": resp.text.strip()}
        return {"status": "error", "code": resp.status_code, "error": resp.text[:300]}

    # ------------------------------------------------------------------
    # Convenience: normalized applicant view for the dispatcher
    # ------------------------------------------------------------------
    @staticmethod
    def extract_contact(applicant: Dict[str, Any]) -> Dict[str, str]:
        """Best-effort name + phone extraction across personal/commercial shapes.

        Phone values must contain at least 7 digits — garbage like "N/A",
        "none" or "0" is dropped so the dispatcher fails closed instead of
        dialing nonsense.
        """
        first = str(applicant.get("FirstName") or "").strip()
        last = str(applicant.get("LastName") or "").strip()
        business = str(applicant.get("BusinessName") or "").strip()
        name = business or f"{first} {last}".strip() or "the client"
        phones = []
        for key in ("CellPhone", "BusinessPhone", "HomePhone", "Phone", "PrimaryPhone"):
            v = str(applicant.get(key) or "").strip()
            digits = "".join(c for c in v if c.isdigit())
            if v and len(digits) >= 7 and v not in phones:
                phones.append(v)
        return {"name": name, "first_name": first, "phone": phones[0] if phones else "",
                "phones": phones}
