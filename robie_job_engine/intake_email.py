"""Read a manually selected Gmail message; never label, archive, or reply."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from .intake_core import IntakeHold, SourceItem, require_test


def read_selected_message(service: Any, *, mailbox: str, message_id: str) -> SourceItem:
    require_test()
    if "@" not in mailbox or not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
        raise IntakeHold("Explicit mailbox and Gmail message ID are required")
    response = service.users().messages().get(userId=mailbox, id=message_id, format="raw").execute()
    if response.get("id") != message_id or not response.get("raw") or not response.get("internalDate"):
        raise IntakeHold("Gmail did not return the selected original message and receipt time")
    raw = str(response["raw"])
    content = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    received = datetime.fromtimestamp(int(response["internalDate"]) / 1000, timezone.utc).isoformat()
    source = SourceItem("gmail", mailbox.strip().casefold(), message_id,
                        f"https://mail.google.com/mail/u/{mailbox}/#all/{message_id}",
                        received, "original-email.eml", content)
    source.validate()
    return source
