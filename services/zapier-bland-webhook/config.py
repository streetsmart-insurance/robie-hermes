"""Configuration for the Bland dispatcher service.

All secrets come from environment variables. Nothing is hardcoded.
"""
import os


def _truthy(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


class Config:
    # Server
    PORT = int(os.environ.get("PORT", "8080"))

    # Modes / safety gates (fail-closed: live calls require explicit opt-in)
    DRY_RUN = _truthy("DRY_RUN", "1")  # default ON: log only, never dial
    LIVE_AUTODIAL = _truthy("ROBIE_VOICE_AUTODIAL_LIVE", "")
    KILL_SWITCH = any(
        _truthy(v) for v in ("ROBIE_HALT", "ROBIE_READ_ONLY")
    )

    # Bland
    BLAND_API_KEY = os.environ.get("BLAND_API_KEY", "")
    BLAND_BASE = "https://api.bland.ai"
    VOICE_ID = os.environ.get(
        "VOICE_ID", "29158307-9893-4149-8a75-bc9ce313d64e"
    )  # Karen (Jake's pick)
    FROM_NUMBER = os.environ.get("FROM_NUMBER", "+17322986745")
    CALLBACK_NUMBER = os.environ.get("CALLBACK_NUMBER", "732-462-8343")
    TRANSFER_NUMBER = os.environ.get("TRANSFER_NUMBER", "+17324622360")  # Carlo

    # EZLynx Classic API (services.ezlynx.com)
    EZLYNX_USER = os.environ.get("EZLYNX_USER", "")
    EZLYNX_PASSWORD = os.environ.get("EZLYNX_PASSWORD", "")
    EZLYNX_APP_SECRET = os.environ.get("EZLYNX_APP_SECRET", "")

    # EZLynx OAuth2 (app.ezlynx.com) — optional, used for document upload
    EZLYNX_OAUTH_CLIENT_ID = os.environ.get("EZLYNX_OAUTH_CLIENT_ID", "")
    EZLYNX_OAUTH_CLIENT_SECRET = os.environ.get("EZLYNX_OAUTH_CLIENT_SECRET", "")
    EZLYNX_OAUTH_USERNAME = os.environ.get("EZLYNX_OAUTH_USERNAME", "")
    EZLYNX_INTEGRATION_GROUP_ID = os.environ.get("EZLYNX_INTEGRATION_GROUP_ID", "")
    EZLYNX_CONNECT_TOKEN_URL = os.environ.get(
        "EZLYNX_CONNECT_TOKEN_URL", "https://app.ezlynx.com/auth/connect/token"
    )

    # Double-dial
    REDIAL_DELAY_SECONDS = int(os.environ.get("REDIAL_DELAY_SECONDS", "10"))
    CALL_POLL_TIMEOUT_SECONDS = int(os.environ.get("CALL_POLL_TIMEOUT_SECONDS", "300"))
    CALL_POLL_INTERVAL_SECONDS = int(os.environ.get("CALL_POLL_INTERVAL_SECONDS", "10"))

    @classmethod
    def live_calls_allowed(cls) -> tuple[bool, str]:
        """Fail-closed gate. Returns (allowed, reason)."""
        if cls.DRY_RUN:
            return False, "DRY_RUN is enabled"
        if cls.KILL_SWITCH:
            return False, "kill switch active (ROBIE_HALT / ROBIE_READ_ONLY)"
        if not cls.LIVE_AUTODIAL:
            return False, "ROBIE_VOICE_AUTODIAL_LIVE is not set"
        if not cls.BLAND_API_KEY:
            return False, "BLAND_API_KEY is not set"
        return True, "ok"
