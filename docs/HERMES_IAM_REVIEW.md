# HERMES IAM review

Least-privilege audit for `streetsmart-hermes-poc`. Expected minimum bindings live in
`deploy/iam/expected-minimum-access.json`.

## Export full inventory (admin)

```sh
bash scripts/audit-hermes-iam.sh
```

Writes JSON dumps + `account-role-summary.md` under `/tmp/hermes-iam-audit-*`.

## Apply remediations

```sh
bash scripts/harden-hermes-iam.sh
```

## Verify Test secret isolation

```sh
bash scripts/prove-test-production-secret-denial.sh
```

## Reviewer limitations

`pawelstasinskiuk@gmail.com` cannot call `projects.getIamPolicy` or
`serviceAccounts.getIamPolicy`. Instance IAM, secret IAM (partial), and VM
attachments were audited directly. Project-level role matrix is **partial** until
admin runs `audit-hermes-iam.sh`.

## Open follow-ups

| Item | Risk | Action |
| --- | --- | --- |
| VM OAuth scope `cloud-platform` on Test | Broad token if SA gains IAM | Narrow scopes on maintenance window |
| `robie-chat-*`, `claude-cloud-diag` SAs | Unknown project roles | Admin export + remove unused |
| Default compute SA | Accidental attachment | Never attach to Hermes VMs |
