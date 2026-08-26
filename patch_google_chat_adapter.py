from pathlib import Path


path = Path("/opt/streetsmart-hermes/.hermes/hermes-agent/plugins/platforms/google_chat/adapter.py")
text = path.read_text()
old = "job_id = open_chat_job(ROBIE_JOB_DB, message_id, text)"
new = """job_id = open_chat_job(
                ROBIE_JOB_DB,
                message_id,
                text,
                attachments=list(zip(event.media_urls or [], event.media_types or [])),
            )"""
if old not in text and new not in text:
    raise SystemExit("expected Google Chat Job Engine call site was not found")
if old in text:
    backup = path.with_suffix(".py.pre-doc-ingestion")
    if not backup.exists():
        backup.write_text(text)
    path.write_text(text.replace(old, new, 1))
print("patched" if old in text else "already-patched")
