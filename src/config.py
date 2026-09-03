"""Application Configuration."""

import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # 1. Renewal Window & Cadence
    renewal_window_min_days: int = 30
    renewal_window_max_days: int = 50
    followup_cadence_min_days: int = 5
    followup_cadence_max_days: int = 7
    max_followups: int = 3

    # 2. Gmail API Outreach & Dual-Inbox Polling Settings
    gmail_outreach_email: str = "robie@streetsmart.insurance"
    gmail_bot_email: str = "robie@streetsmart.insurance"  # Alias for sender
    gmail_poll_inboxes: list = ["robie@streetsmart.insurance", "hello@streetsmart.insurance"]
    gmail_credentials_file: str = "data/credentials/credentials.json"
    gmail_token_file: str = "data/credentials/token_robie.json"  # Robie's token
    gmail_hello_token_file: str = "data/credentials/token_hello.json"  # Hello inbox token
    gmail_token_base64: Optional[str] = None

    # 3. EZLynx Settings
    ezlynx_base_url: str = "https://app.ezlynx.com"
    ezlynx_api_base_url: str = "https://api.ezlynx.com"
    ezlynx_client_id: str = "street_smart_api"
    ezlynx_client_secret: str = ""
    ezlynx_agency_id: str = ""
    ezlynx_username: str = ""
    ezlynx_password: str = ""
    ezlynx_user_data_dir: str = "~/.ezlynx_chrome_profile"
    ezlynx_cdp_endpoint: Optional[str] = None

    # 4. Playwright Headless & Automation
    playwright_headless: bool = True
    playwright_browser_timeout_ms: int = 60000

    # 5. Paths & Database
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'renewals.db'}"
    downloads_dir: str = str(BASE_DIR / "data" / "downloads")
    input_reports_dir: str = str(BASE_DIR / "data" / "input_reports")
    log_level: str = "INFO"

    @property
    def downloads_path(self) -> Path:
        p = Path(self.downloads_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def input_reports_path(self) -> Path:
        p = Path(self.input_reports_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

settings = Settings()
