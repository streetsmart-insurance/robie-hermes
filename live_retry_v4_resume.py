import re
import subprocess


prompt = """Continue the unfinished EZLynx operation from this exact session. Do not restart research and do not create a new discussion.

A dedicated Chrome process is already running continuously for ROBIE with a persistent profile. Attach to the existing browser at CDP http://127.0.0.1:9222 (or obtain its current PID from the running google-chrome process for cua_browser_prepare). Do not launch a separate ephemeral browser and do not close the persistent browser when finished.

Authentication is available in GCP Secret Manager as two separate existing secrets named 'ezlynx-username' and 'ezlynx-password'. Use those exact secret names if authentication is required. Never print, repeat, or store their values in output or job evidence. Retrieve the existing two-factor code through the established ROBIE Gmail OAuth path and never include that code in output or evidence.

Inspect the current server-backed state of account 31897605, Razza Renewal, The Hartford, Workers Compensation. Preserve correct values and do not duplicate anything.

Finish only missing work:
- Workers Compensation status is Quoted.
- Gross quoted premium is $24,622.00.
- In the EXISTING Razza Renewal discussion (never a new or unrelated discussion), attach '/opt/streetsmart-hermes/robie-job-engine/data/artifacts/5d88bd97-d820-4c26-b1a8-888299aaf3b1/Razza Renewal - The Hartford Workers Compensation Quote.pdf'. Set the actual browser file input before clicking Upload and confirm the readable filename is selected.
- Add note 'ROBIE was here. The Hartford Workers Compensation quote premium is $24,622.00.' to that attachment.
- The existing renewal discussion title is meaningful and not Untitled. Do not create a new discussion to satisfy this.

After saving, navigate away to the account overview, reopen the exact Razza Renewal submission and existing renewal discussion, and independently read the fresh destination state. Report only the observed discussion title, attachment filename, attachment note, Workers Compensation status, and gross premium. Only report COMPLETE if all five are present after fresh navigation; otherwise report UNVERIFIED."""

result = subprocess.run(
    [
        "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/hermes",
        "--resume",
        "20260822_135502_aff89d",
        "-z",
        prompt,
        "--cli",
    ],
    text=True,
    capture_output=True,
    timeout=1800,
)


def safe_log(text: str) -> str:
    return re.sub(r"\b\d{6}\b", "[REDACTED]", text)


print(safe_log(result.stdout))
print(safe_log(result.stderr))
raise SystemExit(result.returncode)
