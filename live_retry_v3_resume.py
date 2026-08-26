import subprocess


prompt = """Continue the unfinished EZLynx operation from this exact session. Do not restart the research or create a new discussion.

First inspect the current server-backed state of account 31897605, Razza Renewal, The Hartford, Workers Compensation. Preserve any correct values already saved and do not duplicate anything.

Finish only the missing work:
- Workers Compensation status must be Quoted.
- Gross quoted premium must be $24,622.00.
- In the EXISTING Razza Renewal discussion (never a new or unrelated discussion), attach '/opt/streetsmart-hermes/robie-job-engine/data/artifacts/5d88bd97-d820-4c26-b1a8-888299aaf3b1/Razza Renewal - The Hartford Workers Compensation Quote.pdf'. Set the actual browser file input before clicking Upload and confirm the readable filename is selected.
- Add the note 'ROBIE was here. The Hartford Workers Compensation quote premium is $24,622.00.' to that attachment.
- The existing renewal discussion title must be meaningful and must not be Untitled. Do not create a new discussion to satisfy this.

After saving, navigate away to the account overview, reopen the exact Razza Renewal submission and existing renewal discussion, and independently read the fresh destination state. Report the observed discussion title, attachment filename, attachment note, Workers Compensation status, and gross premium. Only report COMPLETE if all five are present after this fresh navigation; otherwise report UNVERIFIED."""

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
print(result.stdout)
print(result.stderr)
raise SystemExit(result.returncode)
