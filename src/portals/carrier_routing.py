"""Carrier Directory & Routing Matrix for Renewal Document Retrieval."""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from src.config import BASE_DIR

logger = logging.getLogger("carrier_routing")
DIRECTORY_FILE = BASE_DIR / "data" / "carrier_directory.json"

class CarrierRoutingMatrix:
    """Manages carrier retrieval methods (PORTAL vs EMAIL vs EMAIL_ASK_PORTAL)."""

    @classmethod
    def load_directory(cls) -> Dict[str, Dict[str, Any]]:
        if not DIRECTORY_FILE.exists():
            return {}
        try:
            with open(DIRECTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading carrier directory: {e}")
            return {}

    @classmethod
    def save_directory(cls, data: Dict[str, Dict[str, Any]]) -> None:
        DIRECTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DIRECTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def get_carrier_config(cls, carrier_name: str) -> Dict[str, Any]:
        data = cls.load_directory()
        if not carrier_name:
            return {"channel": "EMAIL", "notes": "Default email outreach"}
        
        # Direct match
        if carrier_name in data:
            return data[carrier_name]

        # Case-insensitive / partial match
        carrier_lower = carrier_name.lower()
        for key, conf in data.items():
            if key.lower() in carrier_lower or carrier_lower in key.lower():
                return conf

        # Default fallback
        return {
            "channel": "EMAIL",
            "notes": "Unregistered carrier; default to email outreach."
        }

    @classmethod
    def update_carrier(
        cls,
        carrier_name: str,
        channel: str,
        portal_url: Optional[str] = None,
        underwriter_email: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        data = cls.load_directory()
        entry = data.get(carrier_name, {})
        entry["channel"] = channel.upper()
        if portal_url:
            entry["portal_url"] = portal_url
        if underwriter_email:
            entry["underwriter_email"] = underwriter_email
        if notes:
            entry["notes"] = notes

        data[carrier_name] = entry
        cls.save_directory(data)
        logger.info(f"Updated carrier directory for: {carrier_name} -> {entry}")
        return entry
