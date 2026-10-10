# Accounting delegated-mailbox access preflight

This candidate reuses `gmail_accountability.build_keyless_delegated_service`.
It adds a bounded access check, not an accounting collector, mail sender,
installed job, scheduler, bank connector, or Production release.

## Operator prerequisites

- Identify the existing runtime/delegated service account and its owner.
- Verify Google Workspace domain-wide authorization for `gmail.readonly` and
  IAM-backed signing. Reuse keyless credentials; do not create JSON keys.
- Review the exact accounting mailbox population in private configuration.
  The probe accepts only explicit addresses within one specified domain. It
  does not discover users, expand a directory, or authorize mailbox access.
- Establish the approved source-read environment and actor. An audit-only GCP
  identity does not acquire runtime Gmail access from this candidate. Use the
  existing runtime operator route; no new IAM grant or workflow is included.
- Keep the index-to-mailbox mapping and output in private business storage.
  No original messages or login identities belong in a PR, fixture, or report.

## Execution

Use the existing runtime's Python environment and Google client dependencies.
Invoke `python -m robie_job_engine.accounting_mailbox_preflight` with the
existing `--service-account` reference, `--approved-domain`, one `--mailbox`
argument per reviewed address, and `--live-read`. Keep argument values private.
No credentials are accepted by the command and it writes no files.

The command verifies `getProfile` matches the intended mailbox, executes a
search limited to one message, then requests that exact message in full format.
Original content is used transiently and omitted from output. The report uses
mailbox indices, timestamp and check verdicts only; even provider exception
text is suppressed. The process exits 2 if any check is unverified.

An empty mailbox proves identity/search access only; full-message access stays
unverified. A wrong profile stops before searching. One failing mailbox does
not hide the remaining checks or make the overall report green.

This is deliberately not paginated: it checks permission for one message,
not complete accounting source coverage. `complete_source_inventory` is always
false. Attachment download, freshness, historical coverage, inbox cleanup,
sending permissions and live accounting outcome remain unverified.

## Next integration steps

1. Obtain private per-mailbox evidence from the approved runtime operator.
2. Extend the existing carrier-statement/intake draft work; do not duplicate
   its collectors, parsers, evidence ledger or queues.
3. Integrate the approved sender with existing outbound-send and Sent readback
   guards. Test recipient identity, reply threads, attachments, duplicate sends,
   stop-on-reply/statement-arrival and unknown send outcomes before activation.
4. Configure recurring jobs only through the actual reviewed release process.
   Each source collector needs its own complete pagination and freshness proof.
5. Establish bank view/statement access separately from payment initiation.

Routine email execution belongs to the configured, released workflow. This
probe does not create a per-email human approval requirement or supply sending
authority. No payments, postings, coverage changes or tax filings are included.
