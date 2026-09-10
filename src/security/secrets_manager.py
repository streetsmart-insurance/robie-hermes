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
            if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("TEST_ENABLE_GCP_NETWORK"):
                return None
            gcp_val = self._get_from_gcp_secrets(service_name, key)
            if gcp_val:
                return gcp_val

        return None

    def get_login_pair(self, service_name: str) -> Dict[str, Optional[str]]:
        """Retrieves username and password pair for carrier/service logins."""
        username = self.get_credential(service_name, "username")
        password = self.get_credential(service_name, "password")

        if username and password:
            return {"username": username, "password": password}

        # Check structured JSON blob in Keychain fallback
        if keyring:
            try:
                blob = keyring.get_password("StreetSmartInsurance", f"{service_name}_login")
                if blob:
                    data = json.loads(blob)
                    return {
                        "username": username or data.get("username"),
                        "password": password or data.get("password")
                    }
            except Exception:
                pass

        return {
            "username": username,
            "password": password
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
        projects_to_try = [
            p for p in [
                "workspace-inbox-tracker",
                "streetsmart-hermes-poc",
                self.gcp_project_id,
                os.getenv("GCP_PROJECT_ID"),
            ] if p
        ]
        # Remove duplicates while preserving order
        seen = set()
        projects = [x for x in projects_to_try if not (x in seen or seen.add(x))]

        if getattr(self, "_gcp_disabled", False):
            return None

        # Fast upfront validation of GCP credentials to avoid gRPC retry hangs
        try:
            import google.auth
            from google.auth.transport.requests import Request
            creds, _ = google.auth.default()
            if hasattr(creds, "refresh") and not creds.valid:
                creds.refresh(Request())
        except Exception as e:
            logger.debug(f"GCP default credentials unavailable ({e}); bypassing GCP secrets.")
            self._gcp_disabled = True
            return None

        try:
            client = secretmanager.SecretManagerServiceClient()
        except Exception as e:
            logger.debug(f"Failed to initialize GCP Secret Manager client: {e}")
            self._gcp_disabled = True
            return None
        svc_norm = service_name.lower().replace(" ", "").replace("-", "").replace("_", "")
        svc_snake = service_name.lower().replace(" ", "_").replace("-", "_")
        svc_kebab = service_name.lower().replace(" ", "-").replace("_", "-")
        fld_norm = field.lower()
        fld_kebab = field.lower().replace("_", "-")

        # Prioritized candidate names
        candidates = [
            f"{svc_snake}_robie_{fld_norm}",
            f"{svc_kebab}-robie-{fld_kebab}",
            f"{svc_norm}_robie_{fld_norm}",
            f"{svc_snake}_{fld_norm}",
            f"{svc_kebab}-{fld_kebab}",
            f"{svc_kebab}-{fld_norm}",
            f"{svc_norm}_{fld_norm}",
            f"{svc_norm}-{fld_norm}",
            f"insurance_{svc_snake}_{fld_norm}",
            f"insurance_{svc_norm}_{fld_norm}",
            f"{svc_norm}-agent-login",
            f"{svc_norm}_agent_login",
            f"{svc_norm.upper()}_{fld_norm.upper()}",
            f"{service_name.lower()}_{fld_norm}"
        ]

        for proj in projects:
            for sec_id in candidates:
                try:
                    # 1. Try latest
                    name = f"projects/{proj}/secrets/{sec_id}/versions/latest"
                    resp = client.access_secret_version(request={"name": name})
                    raw_val = resp.payload.data.decode("UTF-8").strip()
                    
                    # If the secret contains multi-line key-value pairs (e.g. username: ..., password: ...)
                    if "\n" in raw_val and ":" in raw_val:
                        for line in raw_val.splitlines():
                            if line.lower().startswith(f"{fld_norm}:"):
                                return line.split(":", 1)[1].strip()
                    return raw_val
                except Exception as e:
                    err_str = str(e)
                    if any(k in err_str for k in ["Reauthentication is needed", "RefreshError", "invalid_grant", "Unauthenticated", "MetadataPlugin"]):
                        logger.debug(f"GCP authentication unavailable ({e}); disabling GCP secret lookup.")
                        self._gcp_disabled = True
                        return None
                    # If latest fails, inspect enabled versions
                    try:
                        parent = f"projects/{proj}/secrets/{sec_id}"
                        for v in client.list_secret_versions(request={"parent": parent}):
                            if v.state == secretmanager.SecretVersion.State.ENABLED:
                                resp = client.access_secret_version(request={"name": v.name})
                                raw_val = resp.payload.data.decode("UTF-8").strip()
                                if "\n" in raw_val and ":" in raw_val:
                                    for line in raw_val.splitlines():
                                        if line.lower().startswith(f"{fld_norm}:"):
                                            return line.split(":", 1)[1].strip()
                                return raw_val
                    except Exception as inner_e:
                        inner_err = str(inner_e)
                        if any(k in inner_err for k in ["Reauthentication is needed", "RefreshError", "invalid_grant", "Unauthenticated", "MetadataPlugin"]):
                            self._gcp_disabled = True
                            return None
        return None

    def set_secret(self, secret_id: str, secret_value: str, project_id: str = "workspace-inbox-tracker") -> bool:
        """Creates or updates a secret version in Google Cloud Secret Manager."""
        if not secretmanager:
            logger.error("google-cloud-secret-manager not available")
            return False
        try:
            client = secretmanager.SecretManagerServiceClient()
            parent = f"projects/{project_id}"
            secret_path = f"{parent}/secrets/{secret_id}"

            # Check if secret exists, create if not
            try:
                client.get_secret(request={"name": secret_path})
            except Exception:
                client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_id,
                        "secret": {
                            "replication": {"automatic": {}},
                        },
                    }
                )

            # Add new version
            payload = secret_value.encode("UTF-8")
            client.add_secret_version(
                request={
                    "parent": secret_path,
                    "payload": {"data": payload},
                }
            )
            logger.info(f"Successfully stored secret '{secret_id}' in GCP project {project_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to set secret '{secret_id}' in GCP {project_id}: {e}")
            return False

# Global Singleton
secrets_mgr = SecretsManager()
