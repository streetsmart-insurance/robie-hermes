"""Cardinal rules for every StreetSmart ROBIE automation.

These are not advisory. A violation raises and the job dies UNVERIFIED.
Rule text is duplicated in CARDINAL_RULES.md and each project's AGENTS.md;
this module is the machine-enforced copy.
"""

from __future__ import annotations


class CardinalViolation(RuntimeError):
    """Raised when an agent attempts an action the cardinal rules forbid.

    Never catch this to continue. Catch it only to record the receipt and abort.
    """

    def __init__(self, rule_id: str, message: str, detail: dict | None = None):
        self.rule_id = rule_id
        self.detail = detail or {}
        super().__init__(f"[{rule_id}] {message}")


# ---------------------------------------------------------------------------
# CR-1  NEVER DELETE
# ---------------------------------------------------------------------------
# ROBIE destroys nothing in EZLynx. Not a policy, not an applicant, not a
# document, not a contact, not a note, not a task, not a driver or vehicle or
# location. The ONE exception is a duplicate PENDING renewal (RWL) transaction
# shell that ROBIE itself created, and only through
# destructive_guard.guarded_transaction_delete() with scope confirmed.
#
# Origin: 2026-09 incident. ROBIE was told to remove a duplicate renewal
# transaction and deleted the entire policy record instead. The instruction was
# correct; the SCOPE resolution was wrong. CR-1 therefore does not turn on what
# the agent intended -- it turns on proving what the click/request will hit.

PROTECTED_OBJECT_CLASSES: frozenset[str] = frozenset({
    "policy",
    "applicant",
    "account",
    "household",
    "document",
    "attachment",
    "document_folder",
    "contact",
    "directory_entry",
    "note",
    "discussion",
    "task",
    "driver",
    "vehicle",
    "location",
    "coverage",
    "mortgagee",
    "prior_carrier",
    "submission",
    "quote",
    "user",
    "carrier",
})

# The only object class that may ever be deleted, and only under conditions.
DELETABLE_OBJECT_CLASS = "policy_transaction"

# Words that mark a control or endpoint as destructive. Matched case-insensitively
# as substrings, so "Delete Policy", "deletePolicy", "btnRemove" all hit.
DESTRUCTIVE_TOKENS: tuple[str, ...] = (
    "delete",
    "remove",
    "void",
    "detach",
    "discard",
    "trash",
    "purge",
    "unlink",
    "erase",
    "destroy",
    "wipe",
    "clear all",
    "unassign",
    "revoke",
)

# Controls whose text contains these are destructive AND unconditionally denied,
# because no allowlisted operation ever needs them.
NEVER_ALLOWED_TOKENS: tuple[str, ...] = (
    "delete policy",
    "delete account",
    "delete applicant",
    "delete household",
    "delete client",
    "remove policy",
    "remove applicant",
    "delete document",
    "delete contact",
    "delete all",
    "remove all",
    "delete permanently",
)

# ---------------------------------------------------------------------------
# CR-2  NEVER BIND, NEVER TAKE MONEY, NEVER EMAIL THE INSURED
# ---------------------------------------------------------------------------
FORBIDDEN_INTENTS: frozenset[str] = frozenset({
    "bind_policy",
    "issue_policy",
    "collect_payment",
    "submit_payment",
    "cancel_policy",
    "reinstate_policy",
    "email_insured",
    "text_insured",
    "change_credentials",
    "delete_user",
})

# ---------------------------------------------------------------------------
# CR-3  NEVER OVERWRITE HUMAN CONTENT
# ---------------------------------------------------------------------------
# Append-only on anything a person authored. A note is added, never edited.
# A document is uploaded beside, never replaced. A directory field is filled
# when blank, never overwritten when populated -- if it is populated and wrong,
# that is a task for a human, not a silent correction.

APPEND_ONLY_OBJECT_CLASSES: frozenset[str] = frozenset({
    "note",
    "discussion",
    "document",
    "attachment",
})

# ---------------------------------------------------------------------------
# CR-4  UNVERIFIED IS AN ACCEPTABLE OUTCOME; A FALSE "DONE" IS NOT
# ---------------------------------------------------------------------------
# Every job re-fetches destination state from the system of record after acting
# and treats its own memory of having acted as untrusted. If it cannot quote the
# literal destination value back, the outcome is UNVERIFIED. See JOB_CONTRACT.md.

TERMINAL_OUTCOMES: frozenset[str] = frozenset({
    "PASS",         # all gates green, independently re-verified
    "UNVERIFIED",   # acted, could not independently confirm -> human looks
    "BLOCKED",      # refused to act, preconditions unmet -> human looks
    "CATASTROPHE",  # a cardinal rule was violated or damage detected -> halt all
})


ALL_RULES: dict[str, str] = {
    "CR-1": "Never delete anything in EZLynx. Only exception: a duplicate PENDING "
            "RWL transaction shell, via guarded_transaction_delete(), scope proven.",
    "CR-2": "Never bind, issue, cancel, reinstate, take payment, or contact the insured.",
    "CR-3": "Append-only on human content. Never overwrite a populated field, "
            "note, or document -- raise a task instead.",
    "CR-4": "Re-verify from the system of record. Report UNVERIFIED rather than "
            "claiming done. A false success is the worst outcome in the system.",
    "CR-5": "Every write is policy-associated, carries literal quoted values, and "
            "ends with the ROBIE signature.",
    "CR-6": "One job, one object. Never fan a single confirmation across multiple "
            "policies or applicants.",
    "CR-7": "Kill switches are re-read before every action, never cached.",
}
