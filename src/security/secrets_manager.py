"""Pluggable Secret Manager Adapter for Carrier & System Logins.

Supports:
- macOS Keychain (native via python keyring)
- Google Cloud Secret Manager (GCP)
- Local .env / Environment Variables
"""

import os
import json
import logging
from typing import Optional, Dict, Any

try:
    import keyring
except ImportError:
    keyring = None

try:
    from google.cloud import secretmanager
except ImportError:
    secretmanager = None

logger = logging.getLogger("secrets_manager")

class SecretsManager:
    """Unified interface to retrieve and manage credentials from macOS Keychain & GCP Secret Manager."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or os.getenv("SECRET_MANAGER_PROVIDER", "auto")).lower()
        self.gcp_project_id = os.getenv("GCP_PROJECT_ID")

    def get_credential(self, service_name: str, key: str = "password") -> Optional[str]:
        """Retrieves a specific field (e.g. 'username', 'password', 'api_key') for a service."""
        # 1. Check environment variable override first (e.g. EZLYNX_USERNAME, THE_HARTFORD_PASSWORD)
        env_key = f"{service_name.upper().replace(' ', '_').replace('-', '_')}_{key.upper()}"
        env_val = os.getenv(env_key)
        if env_val:
            return env_val

        # 2. Check macOS Keychain
        if self.provider in ["keychain", "macos_keychain", "auto"]:
            kc_val = self._get_from_macos_keychain(service_name, key)
            if kc_val:
                return kc_val

        # 3. Check Google Cloud Secret Manager
        if self.provider in ["gcp", "google", "google_secret_manager", "auto"]:
            gcp_val = self._get_from_gcp_secrets(service_name, key)
            if gcp_val:
                return gcp_val

        return None

    def get_login_pair(self, service_name: str) -> Dict[str, Optional[str]]:
        """Retrieves username and password pair for carrier/service logins."""
        # Check structured JSON blob in Keychain first
        if keyring:
            try:
                blob = keyring.get_password("StreetSmartInsurance", f"{service_name}_login")
                if blob:
                    data = json.loads(blob)
                    return {
                        "username": data.get("username"),
                        "password": data.get("password")
                    }
            except Exception:
                pass

        return {
            "username": self.get_credential(service_name, "username"),
            "password": self.get_credential(service_name, "password")
        }

    def save_to_macos_keychain(self, service_name: str, username: str, password: str) -> bool:
        """Saves credentials for a carrier/service in macOS Keychain."""
        if not keyring:
            logger.error("keyring library not available")
            return False
        try:
            # 1. Save password by service name
            keyring.set_password("StreetSmartInsurance", service_name, password)
            # 2. Save username / account name
            keyring.set_password("StreetSmartInsurance", f"{service_name}_username", username)
            # 3. Save combined JSON bundle
            bundle = json.dumps({"username": username, "password": password, "service": service_name})
            keyring.set_password("StreetSmartInsurance", f"{service_name}_login", bundle)
            logger.info(f"Saved credentials for '{service_name}' ({username}) to macOS Keychain")
            return True
        except Exception as e:
            logger.error(f"Failed to save to macOS Keychain for '{service_name}': {e}")
            return False

    def save_to_gcp_secrets(self, service_name: str, field: str, secret_value: str, project_id: Optional[str] = None) -> bool:
        """Creates or updates a secret in Google Cloud Secret Manager."""
        if not secretmanager:
            logger.error("google-cloud-secret-manager library not available")
            return False
        try:
            proj = project_id or self.gcp_project_id or os.getenv("GCP_PROJECT_ID")
            if not proj:
                logger.error("GCP_PROJECT_ID is required to write to GCP Secret Manager")
                return False

            client = secretmanager.SecretManagerServiceClient()
            secret_id = f"insurance_{service_name.lower().replace(' ', '_').replace('-', '_')}_{field.lower()}"
            parent = f"projects/{proj}"

            try:
                client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_id,
                        "secret": {"replication": {"automatic": {}}},
                    }
                )
            except Exception:
                pass  # Secret already exists

            secret_path = client.secret_path(proj, secret_id)
            client.add_secret_version(
                request={
                    "parent": secret_path,
                    "payload": {"data": secret_value.encode("UTF-8")},
                }
            )
            logger.info(f"Saved '{field}' for '{service_name}' to GCP Secret Manager ({secret_id})")
            return True
        except Exception as e:
            logger.error(f"Failed to write to GCP Secret Manager: {e}")
            return False

    def _get_from_macos_keychain(self, service_name: str, field: str) -> Optional[str]:
        """Fetch password or username from macOS Keychain."""
        if not keyring:
            return None
        try:
            if field.lower() in ["password", "pass", "pwd", "secret"]:
                return keyring.get_password("StreetSmartInsurance", service_name)
            elif field.lower() in ["username", "user", "account", "login"]:
                return keyring.get_password("StreetSmartInsurance", f"{service_name}_username")
            return None
        except Exception as e:
            logger.debug(f"macOS Keychain lookup failed for {service_name}/{field}: {e}")
            return None

    def _get_from_gcp_secrets(self, service_name: str, field: str) -> Optional[str]:
        """Fetch secret from Google Cloud Secret Manager."""
        if not secretmanager:
            return None
        try:
            proj = self.gcp_project_id or os.getenv("GCP_PROJECT_ID")
            if not proj:
                return None
            client = secretmanager.SecretManagerServiceClient()
            
            # 1. Try insurance_coterie_password
            secret_id = f"insurance_{service_name.lower().replace(' ', '_').replace('-', '_')}_{field.lower()}"
            name = f"projects/{proj}/secrets/{secret_id}/versions/latest"
            try:
                response = client.access_secret_version(request={"name": name})
                return response.payload.data.decode("UTF-8").strip()
            except Exception:
                pass

            # 2. Try coterie_password
            alt_secret_id = f"{service_name.lower().replace(' ', '_').replace('-', '_')}_{field.lower()}"
            alt_name = f"projects/{proj}/secrets/{alt_secret_id}/versions/latest"
            response = client.access_secret_version(request={"name": alt_name})
            return response.payload.data.decode("UTF-8").strip()
        except Exception as e:
            logger.debug(f"GCP Secret Manager lookup failed for {service_name}/{field}: {e}")
            return None

# Global Singleton
secrets_mgr = SecretsManager()
