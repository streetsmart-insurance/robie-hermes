from pathlib import Path


path = Path("/opt/streetsmart-hermes/.hermes/hermes-agent/plugins/platforms/google_chat/adapter.py")
text = path.read_text()
backup = path.with_suffix(".py.pre-execution-contract")
if not backup.exists():
    backup.write_text(text)

old_import = "from robie_job_engine.chat_guard import guard_chat_response, open_chat_job"
new_import = (
    "from robie_job_engine.chat_guard import (build_chat_execution_text, "
    "guard_chat_response, open_chat_job)"
)
if old_import in text:
    text = text.replace(old_import, new_import, 1)
elif new_import not in text:
    raise SystemExit("ROBIE chat guard import was not found")

old_handoff = """jobs[event.message_id] = job_id
            await self.handle_message(event)"""
new_handoff = """jobs[event.message_id] = job_id
            execution_text = build_chat_execution_text(ROBIE_JOB_DB, job_id, text)
            try:
                event.text = execution_text
            except Exception:
                from dataclasses import replace
                event = replace(event, text=execution_text)
            await self.handle_message(event)"""
if old_handoff in text:
    text = text.replace(old_handoff, new_handoff, 1)
elif new_handoff not in text:
    raise SystemExit("ROBIE Google Chat execution handoff was not found")

old_filename = 'filename = name.split("/")[-1] if name else "attachment"'
new_filename = (
    'filename = attachment.get("contentName") or '
    '(name.split("/")[-1] if name else "attachment")'
)
if old_filename in text:
    text = text.replace(old_filename, new_filename, 1)
elif new_filename not in text:
    raise SystemExit("Google Chat attachment filename assignment was not found")

path.write_text(text)
print("patched")
