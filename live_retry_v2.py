import subprocess


prompt = """Use the existing EZLynx browser automation path and the relevant Drive-loaded EZLynx skill.

Correct the failed test for EZLynx account 31897605, submission 'Razza Renewal', carrier 'The Hartford', line 'Workers Compensation'.

Inspect the current destination state before writing so this retry is idempotent. The required final state is:
1. Workers Compensation status is Quoted.
2. Gross quoted premium is $24,622.00.
3. The exact PDF at '/opt/streetsmart-hermes/robie-job-engine/data/artifacts/5d88bd97-d820-4c26-b1a8-888299aaf3b1/Razza Renewal - The Hartford Workers Compensation Quote.pdf' is attached to the EXISTING renewal discussion for Razza Renewal.
4. The attachment has the note 'ROBIE was here. The Hartford Workers Compensation quote premium is $24,622.00.'
5. The existing renewal discussion has a meaningful renewal title and is not 'Untitled'.

Hard constraints:
- Do not create a new discussion.
- Do not add the attachment to an unrelated discussion.
- Find and use the existing renewal discussion. If it cannot be identified unambiguously, stop without creating one and report the job UNVERIFIED.
- Before clicking Upload or Save, set the browser file chooser to the exact PDF path above and verify the selected filename is visible.
- Interact with controls as a user would; do not directly mutate framework-managed values.
- Do not duplicate an attachment or note that is already present.

Independent verification is mandatory. After saving, navigate away to the account overview, reopen the exact Razza Renewal submission and its existing renewal discussion, and read the fresh server-backed state. Verify the exact discussion title is not Untitled, the PDF attachment is present in that discussion, the note is attached to that file, Workers Compensation is Quoted, and the gross premium is $24,622.00. Report each observed value. If any item is missing or ambiguous, report UNVERIFIED and never claim completion."""

result = subprocess.run(
    [
        "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/hermes",
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
