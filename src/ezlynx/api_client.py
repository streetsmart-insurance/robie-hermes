"""EZLynx REST API Client for posting notes, discussions, tasks, and document uploads."""

import logging
import requests
from typing import Optional, Dict, Any, List
from pathlib import Path

from src.config import settings

logger = logging.getLogger("ezlynx_api")

class EZLynxApiClient:
    """Handles communication with the EZLynx Integration / Developer API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        agency_id: Optional[str] = None
    ):
        self.base_url = (base_url or settings.ezlynx_api_base_url).rstrip("/")
        self.client_id = client_id or settings.ezlynx_client_id
        self.client_secret = client_secret or settings.ezlynx_client_secret
        self.agency_id = agency_id or settings.ezlynx_agency_id
        self._access_token: Optional[str] = None

    def authenticate(self) -> bool:
        """Exchanges client_id & client_secret for an OAuth2 Bearer token."""
        if not (self.client_id and self.client_secret):
            logger.warning("EZLynx API Client credentials not configured. Running in local simulation mode.")
            return False

        try:
            token_url = f"{self.base_url}/oauth/token"
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret
            }
            resp = requests.post(token_url, data=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data.get("access_token")
                logger.info("Successfully authenticated with EZLynx API.")
                return True
            else:
                logger.error(f"EZLynx token request failed: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Error connecting to EZLynx token endpoint: {e}")
            return False

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self.agency_id:
            headers["X-Agency-Id"] = self.agency_id
        return headers

    def add_note_to_discussion(
        self,
        applicant_id: str,
        discussion_title: str,
        note_text: str,
        policy_number: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Posts a note under the designated discussion title for an applicant.
        EZLynx endpoint: POST /api/v1/applicants/{applicantId}/notes
        """
        payload = {
            "applicantId": applicant_id,
            "title": discussion_title,
            "discussionTitle": discussion_title,
            "text": note_text,
            "policyNumber": policy_number,
            "category": "Renewal"
        }

        if not self._access_token and not self.authenticate():
            logger.info(
                f"[SIMULATION] EZLynx Note recorded for Applicant: {applicant_id} | Discussion: '{discussion_title}'"
            )
            return {
                "status": "simulated",
                "applicant_id": applicant_id,
                "discussion_title": discussion_title,
                "note_id": f"sim_note_{applicant_id[:6]}"
            }

        endpoint = f"{self.base_url}/api/v1/applicants/{applicant_id}/notes"
        try:
            resp = requests.post(endpoint, json=payload, headers=self._get_headers(), timeout=20)
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error(f"Failed to post note to EZLynx ({resp.status_code}): {resp.text}")
                return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            logger.error(f"Exception posting note to EZLynx: {e}")
            return {"status": "error", "error": str(e)}

    def upload_document(
        self,
        applicant_id: str,
        file_path: Path,
        folder_name: str = "Renewals",
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Uploads a PDF document to the Applicant's Document Management folder in EZLynx.
        """
        if not file_path.exists():
            return {"status": "error", "error": f"File not found: {file_path}"}

        if not self._access_token and not self.authenticate():
            logger.info(
                f"[SIMULATION] Document '{file_path.name}' uploaded to EZLynx Applicant: {applicant_id} (Folder: {folder_name})"
            )
            return {
                "status": "simulated",
                "applicant_id": applicant_id,
                "file_name": file_path.name,
                "document_id": f"sim_doc_{file_path.stem}"
            }

        endpoint = f"{self.base_url}/api/v1/applicants/{applicant_id}/documents"
        headers = self._get_headers()
        headers.pop("Content-Type", None)  # Let requests set multipart boundary

        try:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "application/pdf")}
                data = {
                    "folder": folder_name,
                    "description": description or f"Renewal Quote Proposal - {file_path.name}"
                }
                resp = requests.post(endpoint, files=files, data=data, headers=headers, timeout=45)
                if resp.status_code in (200, 201):
                    return resp.json()
                else:
                    logger.error(f"Failed to upload document to EZLynx ({resp.status_code}): {resp.text}")
                    return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            logger.error(f"Exception uploading document to EZLynx: {e}")
            return {"status": "error", "error": str(e)}

    def create_user_task(
        self,
        applicant_id: str,
        title: str,
        description: str,
        assigned_user: Optional[str] = None,
        due_days_out: int = 3
    ) -> Dict[str, Any]:
        """Creates a follow-up task for the account manager in EZLynx."""
        if not self._access_token and not self.authenticate():
            logger.info(
                f"[SIMULATION] EZLynx Task Created for Applicant: {applicant_id} | Title: '{title}' | Assigned: {assigned_user or 'Account Manager'}"
            )
            return {
                "status": "simulated",
                "applicant_id": applicant_id,
                "task_title": title,
                "task_id": f"sim_task_{applicant_id[:6]}"
            }

        endpoint = f"{self.base_url}/api/v1/applicants/{applicant_id}/tasks"
        payload = {
            "applicantId": applicant_id,
            "title": title,
            "description": description,
            "assignedTo": assigned_user,
            "priority": "High"
        }
        try:
            resp = requests.post(endpoint, json=payload, headers=self._get_headers(), timeout=20)
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}
