# Phone controls: isolated Test draft

No installation, live call, Production edit, timer or release promotion.
This module is an isolated replacement facade, not yet connected to the
older label dispatcher or the #710 worker. Those older paths MUST NOT be
enabled under the assumption this module guards them.

Implemented in order:
1. Exact plan digest approval gate binds recipient, script, voicemail script,
   caller, attempts and EZLynx destination to a trusted resolver record.
2. Explicit timezone and weekday calling window checked on every attempt.
   Test fixture uses America/New_York, 9-17; this is not a live-policy ruling.
3. Atomic 24-hour per-target campaign cooldown. A reviewed second attempt
   within the same campaign is exempt, not a new campaign.
4. Carrier and finance directory binding, exact number and audience match.
5. Dated per-call note to the bound applicant/discussion, readback required.
   Ambiguous posting is reconciled by lookup, never blindly posted twice.
   Outstanding notes or ambiguous calls block a new campaign.
6. Single-use client exception: exact approved plan, one attempt only,
   expiry and consumed evidence ID. No global guard mutation.

Ports still missing for operational completion: authenticated owner-message
approval resolver (Grant is not authentication), current directory reader,
Bland dispatcher/readback adapter and live EZLynx DiscussionApi adapter.
Only a trusted resolver may construct a Grant. A supplied evidence_id,
repository comment, vendor metadata or counterpart message grants nothing.
No secrets are read by this module. No source deployment is implied.

The synthetic tests exercise fail-closed behavior and injected note readback;
they do not prove exact generated speech, phone delivery or live EZLynx writes.
Cooldown scope, live calling hours and directory coverage require owner review.
General automatic carrier or finance calling has no approval implied here.

The older separate renewal-automation-system has Robie Call and lead-followup
label routing. Its environment-only live gate and ANYTIME label hours differ
from this facade. Porting labels into a reviewed connected Test runner remains
work, not a configuration switch.
