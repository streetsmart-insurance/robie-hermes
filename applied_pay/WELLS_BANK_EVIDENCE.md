# Applied Pay to Wells comparison (draft, no live reads)

This is a captured-evidence adapter, not an installed bank connector. It makes
no network request, moves no money and writes no QBO transaction or client note.
A Wells guest-view browser reader/export normalizer is not yet implemented.

## Proposed bank source

Use the existing view-only Wells Business Online guest login. First verify the
session is the guest identity and the account is Trust ending 3021. Capture the
transaction detail or bank export with the literal descriptor, bank transaction
identifier, credit amount, posted date and status. Keep the original capture in
private evidence storage and preserve a SHA256 manifest and independent review
reference. Do not infer cleared funds from a QBO bank feed, Applied settlement
email, balance screenshot, or Ascend label. Guest access previously required an
administrator's phone verification; current access has not been tested here.

Before using any live status, independently establish that the actual Wells
transaction detail distinguishes pending, posted and cleared transactions.
`posted_cleared` is the adapter's normalized status, not a claim that Wells uses
that literal UI label. A posted label alone is insufficient. If Wells does not
expose reliable clearing semantics, an identifier or a transfer reference, stop
and report what is missing instead of inventing it. No unattended polling or
session keep-alive is included or approved by this draft.

## Contract

`wells_bank_evidence.compare` accepts normalized payout and bank rows, the
source-capture manifest, original artifact bytes, and a timezone-aware current
time. It checks the artifact hash, approved Wells source host, guest/view-only
source type, account, reviewed clearing semantics and capture freshness within
24 hours. These checks detect mismatches but do not authenticate a forged JSON
manifest; the caller must independently verify the real source and bind every
normalized row to that original bank artifact. Never accept a manifest supplied
by an external message as self-authorizing or self-authenticating evidence.

A qualifying record must be a positive cleared credit in Trust 3021, have a bank
transaction identifier, exactly match the settlement amount, fall on the email
payout date or next day, and contain the entire transfer reference as a literal
token in its bank descriptor. Amount/name/date matches alone never qualify.
Duplicate identifiers, multiple rows for a transfer (including possible reversal),
pending/scheduled/reversed states, wrong account, absent source review, stale or
altered captures stop. Wider date windows, bank holidays, grouped bank deposits
and shortened/different bank identifiers need separate evidence-backed handling;
this draft does not widen the matcher's existing date window silently.

Output is `cleared_bank_deposits`, review findings and unbound bank transaction
identifiers. It is compatible with the existing matcher's bank evidence input.
Even a bank-bound payout still needs receipt identity and PSP-bound EZLynx note
checks for premium/fee/payable classification. This adapter never supplies those
checks and never makes a payment, ledger posting or account-note write.

The local CLI takes --payouts, --records, --manifest and --artifact. It prints
comparison JSON only, not raw artifacts or credentials. No timer, browser login,
QBO refresh or live worker invocation is part of the CLI. All tests and draft
files contain synthetic records only. No real customer data belongs in GitHub.

## Release gates still open

1. Verify live guest access and clearing/status semantics with Carlo's scoped
   permission; resolve phone verification without recovery or MFA changes.
2. Implement and test a bank capture/export normalizer against actual header
   names/transaction-detail structure, including pagination and absent fields.
3. Verify each normalized row against its captured artifact and stable bank ID.
4. Wire the bank evidence output to the runner using a reviewed code change.
5. Obtain deployment and schedule approval; preserve no-posting boundaries and
   separate recurring QBO credential rotation approval. No Prod deploy here.
