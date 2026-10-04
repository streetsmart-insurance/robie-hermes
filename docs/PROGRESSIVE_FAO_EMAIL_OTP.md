# Progressive FAO email-code path (design only)

This is a design. It does not log in, read a mailbox, type a code, or bypass the
For Agents Only challenge. No code in this change submits a one-time code.

The current FAO pulls (`progressive_fao_memo`, `progressive_bop`) attach to a
browser a person has already signed into. `assert_authenticated` holds when
the page is the login host or a password field is visible. The pull does not
type the password or the SMS code. An email code is the same kind of hold.

## What the path would be for

Some FAO sessions ask for a one-time code by email instead of SMS. The
Document Download route still has to be walked by a person in that already
authenticated Test session. An email code does not make the Directory route
verified, and it does not confirm Progressive.

## What a later path would need

All of the following, and nothing is approved by this document:

1. Host `hermes-test-01` only, `ROBIE_ENV=TEST`, one EZLynx driver checked out
   and back in through Moe. Production and a second agency session stay out.
2. A person completes the FAO user id and password in that Test browser.
   Automation does not type either one.
3. The mailbox that actually receives the FAO code, named by Carlo. The design
   does not guess which inbox, which From address, or which subject.
4. A freshness rule for that message: maximum age, one use, and what a second
   or older code means. A code is never written into a job packet, a log, a
   note, or a screenshot archive.
5. A person reads the code and types it. The job holds while a one-time-code
   field is on screen. It does not fetch the message and it does not fill the
   field.
6. After the person finishes, the session must show the authenticated FAO
   home for agency `CA33617` (the same agency check the memo pull already
   uses). A login host, a password field, or a code field still holds.
7. One `foragentsonly.com` tab. Extra tabs hold, as they do today.

## What this design refuses

- Reading Gmail, or any mailbox, to obtain the code.
- Typing, storing, or retrying the code.
- A bypass, a remembered session cookie planted by the job, or a headless
  login that skips the challenge.
- Treating a memo, an email code, or an endorsement already filed in EZLynx
  as proof that the live Directory Document Download route works.

Until those rules are implemented and a person has completed a code on
`hermes-test-01`, a live Directory walk remains unverified.
