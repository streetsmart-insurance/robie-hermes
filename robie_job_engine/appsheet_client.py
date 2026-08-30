"""Minimal read-only AppSheet table client for accountability inputs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AppSheetConfig:
    app_id: str
    application_access_key: str
    region: str = "www.appsheet.com"

    @classmethod
    def from_env(cls) -> Optional["AppSheetConfig"]:
        app_id = os.environ.get("APPSHEET_APP_ID", "").strip()
        key = os.environ.get("APPSHEET_APPLICATION_ACCESS_KEY", "").strip()
        if not app_id or not key:
            return None
        return cls(app_id, key, os.environ.get("APPSHEET_REGION", "www.appsheet.com").strip())


class AppSheetReadClient:
    """Invoke only AppSheet's ``Find`` action; no mutation methods exist here."""

    def __init__(self, config: AppSheetConfig, opener: Callable[..., Any] = urlopen):
        self.config = config
        self.opener = opener

    def find_rows(self, table_name: str, *, selector: str | None = None) -> list[dict[str, Any]]:
        url = (
            f"https://{self.config.region}/api/v2/apps/{quote(self.config.app_id, safe='')}"
            f"/tables/{quote(table_name, safe='')}/Action"
        )
        body: dict[str, Any] = {"Action": "Find", "Properties": {"Locale": "en-US"}, "Rows": []}
        if selector:
            body["Properties"]["Selector"] = selector
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "ApplicationAccessKey": self.config.application_access_key,
                "Content-Type": "application/json",
            },
        )
        with self.opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("AppSheet Find response must be a list of row objects")
        return payload
