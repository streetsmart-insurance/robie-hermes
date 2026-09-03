"""Gmail API Authentication Helper supporting Service Account (Domain-Wide Delegation) and OAuth2."""

import os
import base64
import json
import logging
from pathlib import Path
from typing import Optional, Dict
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build, Resource

from src.config import settings

logger = logging.getLogger("gmail_auth")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send"
]

def get_service_account_credentials(subject_email: str) -> Optional[service_account.Credentials]:
    """Attempts Domain-Wide Delegation using service_key.json for seamless background auth."""
    service_key_path = Path("data/credentials/service_key.json")
    if service_key_path.exists():
        try:
            creds = service_account.Credentials.from_service_account_file(
                str(service_key_path), scopes=SCOPES, subject=subject_email
            )
            return creds
        except Exception as e:
            logger.debug(f"Service account auth error for {subject_email}: {e}")
    return None

def get_credentials_for_token_file(token_file_path: Path) -> Optional[Credentials]:
    """Retrieves or refreshes credentials for a specific OAuth token file."""
    creds = None

    # 1. Check if token file exists
    if token_file_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file_path), SCOPES)
        except Exception as e:
            logger.error(f"Error reading token file {token_file_path}: {e}")

    # 2. Refresh expired credentials
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_file_path, "w") as tf:
                tf.write(creds.to_json())
            return creds
        except Exception as e:
            logger.warning(f"Failed to refresh token in {token_file_path}: {e}")
            creds = None

    return creds

def interactive_authorize_inbox(token_file_path: Path, account_label: str = "Robie") -> Optional[Credentials]:
    """Runs interactive OAuth2 consent flow to authorize a specific inbox."""
    creds_path = Path(settings.gmail_credentials_file)
    if not creds_path.exists():
        logger.error(f"OAuth credentials file not found at {creds_path}")
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        print(f"\n🔐 Opening browser to authorize {account_label} inbox...")
        creds = flow.run_local_server(port=0)
        token_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_file_path, "w") as tf:
            tf.write(creds.to_json())
        logger.info(f"Successfully saved credentials for {account_label} to {token_file_path}")
        return creds
    except Exception as e:
        logger.error(f"Failed to authorize {account_label}: {e}")
        return None

def get_robie_gmail_service() -> Optional[Resource]:
    """Builds and returns Gmail service for Robie (outreach sender & tracker)."""
    # 1. Try Service Account Domain-Wide Delegation first
    sa_creds = get_service_account_credentials(settings.gmail_outreach_email)
    if sa_creds:
        try:
            svc = build("gmail", "v1", credentials=sa_creds)
            # test validity
            svc.users().getProfile(userId="me").execute()
            return svc
        except Exception:
            pass

    # 2. Try OAuth token file
    token_path = Path(settings.gmail_token_file)
    if not token_path.exists() and Path("data/credentials/token.json").exists():
        token_path = Path("data/credentials/token.json")

    creds = get_credentials_for_token_file(token_path)
    if creds:
        return build("gmail", "v1", credentials=creds)
    return None

def get_hello_gmail_service() -> Optional[Resource]:
    """Builds and returns Gmail service for Hello inbox (data source poller)."""
    # 1. Try Service Account Domain-Wide Delegation first
    sa_creds = get_service_account_credentials("hello@streetsmart.insurance")
    if sa_creds:
        try:
            svc = build("gmail", "v1", credentials=sa_creds)
            svc.users().getProfile(userId="me").execute()
            return svc
        except Exception:
            pass

    # 2. Try OAuth token file
    token_path = Path(settings.gmail_hello_token_file)
    creds = get_credentials_for_token_file(token_path)
    if creds:
        return build("gmail", "v1", credentials=creds)
    return None

def get_all_active_inbox_services() -> Dict[str, Resource]:
    """Returns a mapping of {inbox_email: service} for all connected inboxes."""
    services = {}
    robie_svc = get_robie_gmail_service()
    if robie_svc:
        services[settings.gmail_outreach_email] = robie_svc

    hello_svc = get_hello_gmail_service()
    if hello_svc:
        services["hello@streetsmart.insurance"] = hello_svc

    return services

# Default service & credentials for backward compatibility
def get_gmail_service() -> Optional[Resource]:
    return get_robie_gmail_service() or get_hello_gmail_service()

def get_gmail_credentials() -> Optional[Credentials]:
    token_path = Path(settings.gmail_token_file)
    if not token_path.exists() and Path("data/credentials/token.json").exists():
        token_path = Path("data/credentials/token.json")
    return get_credentials_for_token_file(token_path)
