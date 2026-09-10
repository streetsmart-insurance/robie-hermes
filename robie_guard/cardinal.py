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

DELETABLE_OBJECT_CLASS = "policy_transaction"

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

APPEND_ONLY_OBJECT_CLASSES: frozenset[str] = frozenset({
    "note",
    "discussion",
    "document",
    "attachment",
})

TERMINAL_OUTCOMES: frozenset[str] = frozenset({
    "PASS",
    "UNVERIFIED",
    "BLOCKED",
    "CATASTROPHE",
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
