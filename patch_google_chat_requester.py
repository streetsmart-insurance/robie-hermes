from pathlib import Path

path = Path("/opt/streetsmart-hermes/.hermes/hermes-agent/plugins/platforms/google_chat/adapter.py")
text = path.read_text()
old = """attachments=list(zip(event.media_urls or [], event.media_types or [])),
            )"""
new = """attachments=list(zip(event.media_urls or [], event.media_types or [])),
                requested_by=(
                    getattr(event.source, "user_name", None)
                    or getattr(event.source, "user_id", None)
                    or "Google Chat user"
                ),
                conversation_id=getattr(event.source, "chat_id", None),
                expected_attachment_count=len(
                    ((getattr(event, "raw_message", None) or {}).get("attachment") or [])
                ),
            )"""
if old in text:
    backup = path.with_suffix(".py.pre-requester-ledger")
    if not backup.exists():
        backup.write_text(text)
    path.write_text(text.replace(old, new, 1))
elif new not in text:
    raise SystemExit("Google Chat Job Engine attachment call was not found")
print("patched")
