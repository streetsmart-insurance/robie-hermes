"""Sender policy for the Robie email agent.

2026-09-14: the agent replied to its own robie@streetsmart.insurance messages
~40 times in one night. is_allowed_sender() accepted any @streetsmart.insurance
address, so the agent's own replies (new unread messages from an "allowed"
sender) were treated as fresh inbound tasks — each spawning a new job and
another reply, forever. This module makes the mailbox's own address fail the
sender check, permanently.
"""

#: The mailbox the agent watches. Our own outbound mail must never be treated
#: as an inbound task, and we must never reply to ourselves.
MAILBOX_ADDRESS = "robie@streetsmart.insurance"

#: Explicitly allowed human senders.
ALLOWED_SENDERS = {"carlo@streetsmart.insurance", "jake@streetsmart.insurance"}

#: The mailbox's own address(es) — never allowed, never replied to.
SELF_SENDERS = {MAILBOX_ADDRESS}


def is_self_sender(sender: str) -> bool:
    """True if the sender is the agent's own mailbox address."""
    return sender.lower().strip() in SELF_SENDERS


def is_allowed_sender(sender: str) -> bool:
    """True if the sender may task the email agent.

    The mailbox's own address is always rejected, even though it matches the
    @streetsmart.insurance suffix rule below.
    """
    clean_sender = sender.lower().strip()
    if is_self_sender(clean_sender):
        return False
    if clean_sender in ALLOWED_SENDERS:
        return True
    if clean_sender.endswith("@streetsmart.insurance") or clean_sender.endswith("@streetsmartinsurance.com"):
        return True
    return False
