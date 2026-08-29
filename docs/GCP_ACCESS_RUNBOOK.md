# Keyless GCP access runbook

## Outcome

Authorized builders operate through this private GitHub repository without
reusing Carlo's Google browser session. GitHub Actions exchanges its OIDC token
for a short-lived Google credential through Workload Identity Federation (WIF).
There are no service-account JSON keys.

The trust persists until StreetSmart revokes the GitHub installation, WIF
binding, or service account. Each credential remains short-lived and scoped to
one workflow run.

## Independently verified baseline

Verified from GitHub on 2026-08-29:

- Repository `streetsmart-insurance/robie-hermes` is private; immutable
  repository ID `1343750842`.
- Existing workflow branch `infra/wif-smoke-test` authenticates through provider
  `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github`.
- It impersonates
  `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com`.
- GitHub Actions run `33062970328` completed successfully on 2026-08-27 and
  read `antigravity-test-01` in project `streetsmart-robie-test`.

That proves keyless access only to the isolated Antigravity Test project. It
does **not** prove permission to project `streetsmart-hermes-poc`, access to
`hermes-test-01` or `hermes-poc-01`, the loaded application version, Secret
Manager isolation, or deployment authority.

## Identity separation

| Identity | Allowed | Explicitly prohibited |
| --- | --- | --- |
| Build auditor | Selected resource-plane reads in Test and Production; redacted status evidence | SSH, deploy, restart, secret payloads, IAM changes, billing changes |
| Test deployer | Immutable release installation and verification on Test after review | Production access, rebuilding during promotion, secret payload reads unrelated to Test runtime |
| Production releaser | Separate Release / Production workflow after Carlo approves the exact QA-certified digest | Use by Engineering / Build, unapproved release, artifact substitution |

Do not reuse the name `robie-test-deployer` as proof that it has deploy rights;
IAM policy is authoritative. The current verified workflow uses it only for a
Compute read.

## Readiness workflow

`.github/workflows/gcp-readiness-audit.yml` performs selected, non-secret
resource-plane reads. It deliberately does not SSH, read VM metadata values,
read Secret Manager payloads, restart services, or deploy.

The report distinguishes:

- `VERIFIED`: the exact requested read succeeded;
- `UNVERIFIED`: permission, resource, or connectivity prevented verification.

Authentication alone is never reported as a verified runtime version. Exact
Test/Production versions require an independently verifiable host status
contract. Until that exists, those version facts stay `UNVERIFIED`.
The workflow exits non-zero when either required Hermes instance cannot be
read, so successful identity exchange cannot create a false-green readiness
gate.

## One-time bootstrap still required

An authenticated GCP administrator must complete this once:

1. Inspect the existing WIF provider's issuer, attribute mapping, and attribute
   condition. Confirm it restricts trust by immutable organization and
   repository IDs, including repository ID `1343750842`; names alone are not
   sufficient.
2. Create a dedicated build-auditor service account or formally document why
   the existing read-only identity is retained.
3. Grant only the resource-read permissions required by the readiness workflow
   in `streetsmart-hermes-poc`. Do not grant Owner, Editor, Service Account Key
   Admin, Secret Manager Secret Accessor, OS Login, IAP tunnel, or deployment
   roles to the auditor.
4. Bind WIF principal access to that service account for this repository and
   approved workflow/ref conditions only.
5. Run the readiness workflow and record the run ID. Any denied read remains
   `UNVERIFIED`; do not widen permissions without reviewing the exact denied
   permission.
6. After the live architecture is verified, create a separate Test deployer
   with the narrow permissions needed by the official Test installer. Test its
   rollback path before treating keyless Test deployment as ready.

The bootstrap operator must save the redacted provider description, IAM policy
diff, workflow run ID, and rollback commands. Never save tokens or credential
payloads.

## Handoff to another LLM

Give the LLM access to this private repository, then instruct it to read
`AGENTS.md` and this runbook. It can prepare branches/PRs and inspect workflow
evidence without Carlo's Google login. It must not receive Carlo's password,
cookies, MFA codes, Cloud Shell home directory, or a downloaded service-account
key.

If the LLM cannot access the private repository or the required GitHub Actions
run, stop. Do not fall back to copying credentials into chat.
