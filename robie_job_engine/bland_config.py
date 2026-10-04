"""Bland AI voice agent configuration per Jake Ferrara's specs.

Jake's requirements (email 2026-10-02):
1. Voice: Karen (29158307-9893-4149-8a75-bc9ce313d64e)
2. Script: Identify as "an AI assistant calling on behalf of Jake from
   StreetSmart Insurance" + call reason in one short sentence.
   Screener asks for name/reason → answer directly, repeat if asked,
   speak slowly. Stay on line after identifying, wait to be connected —
   end only if told unavailable or voicemail.
3. Redial: Attempt 1 voicemail → hang up silent, retry after 10 seconds.
   Attempt 2 voicemail → leave full message (slow, clear, AI disclosure,
   callback number digit by digit).
4. Test call: approved

Carlo's corrections:
- Callback number: 732-462-8343 (not Jake's 732-481-2520)
- Recording: OFF (per PR #715 design requirement)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Jake's approved voice
KAREN_VOICE_ID = "29158307-9893-4149-8a75-bc9ce313d64e"

# Carlo's corrected callback number (overrides Jake's 732-481-2520)
CALLBACK_NUMBER = "732-462-8343"
CALLBACK_NUMBER_SPOKEN = "7 3 2, 4 6 2, 8 3 4 3"

# Verified caller ID
CALLER_ID = "+17322986745"


@dataclass
class BlandCallConfig:
    """Configuration for a Bland AI outbound call."""
    
    voice_id: str = KAREN_VOICE_ID
    callback_number: str = CALLBACK_NUMBER
    record: bool = False  # Recording OFF per design requirement
    
    # Script template
    intro_template: str = (
        "Hi, this is an AI assistant calling on behalf of Jake "
        "from StreetSmart Insurance. {reason}."
    )
    
    # Screener handling
    screener_instructions: str = (
        "If asked for your name or the reason for calling, answer directly. "
        "Repeat if asked. Speak slowly and clearly. "
        "After identifying yourself, stay on the line and wait to be connected. "
        "Only end the call if told the person is unavailable or if you reach voicemail."
    )
    
    # Voicemail handling
    voicemail_attempt1_action: str = "hangup_silent"  # Hang up without message
    voicemail_attempt1_retry_delay_seconds: int = 10
    voicemail_attempt2_action: str = "leave_message"
    voicemail_message_template: str = (
        "Hi, this is an AI assistant calling on behalf of Jake "
        "from StreetSmart Insurance. {reason}. "
        "Please call us back at {callback_spoken}. "
        "Again, that's {callback_spoken}."
    )


@dataclass
class CallAttempt:
    """Tracks a single call attempt for redial logic."""
    attempt_number: int
    ended_by_voicemail: bool = False
    message_left: bool = False


class BlandRedialPolicy:
    """Implements Jake's redial policy:
    - Attempt 1 voicemail → hang up silent, retry after 10 seconds
    - Attempt 2 voicemail → leave full message
    """
    
    def __init__(self, config: Optional[BlandCallConfig] = None):
        self.config = config or BlandCallConfig()
    
    def should_retry(self, attempt: CallAttempt) -> bool:
        """Determine if we should retry after this attempt."""
        if not attempt.ended_by_voicemail:
            return False  # Only retry on voicemail
        if attempt.attempt_number >= 2:
            return False  # Max 2 attempts
        if attempt.message_left:
            return False  # Already left message, don't retry
        return True
    
    def get_retry_delay(self, attempt: CallAttempt) -> int:
        """Get delay in seconds before retry."""
        if attempt.attempt_number == 1:
            return self.config.voicemail_attempt1_retry_delay_seconds
        return 0
    
    def should_leave_message(self, attempt: CallAttempt) -> bool:
        """Determine if we should leave a voicemail message on this attempt."""
        if not attempt.ended_by_voicemail:
            return False
        # Leave message on attempt 2 (or if attempt 1 already happened)
        return attempt.attempt_number >= 2
    
    def build_voicemail_message(self, reason: str) -> str:
        """Build the voicemail message with AI disclosure and callback."""
        return self.config.voicemail_message_template.format(
            reason=reason,
            callback_spoken=CALLBACK_NUMBER_SPOKEN,
        )
    
    def build_intro(self, reason: str) -> str:
        """Build the call introduction script."""
        return self.config.intro_template.format(reason=reason)


def create_bland_call_payload(
    to_number: str,
    reason: str,
    config: Optional[BlandCallConfig] = None,
) -> dict:
    """Create the Bland API payload for an outbound call.
    
    Args:
        to_number: Destination phone number (E.164 format)
        reason: One-sentence reason for the call
        config: Optional custom configuration
    
    Returns:
        Dict suitable for POST to Bland API /v1/calls
    """
    cfg = config or BlandCallConfig()
    policy = BlandRedialPolicy(cfg)
    
    return {
        "phone_number": to_number,
        "voice": cfg.voice_id,
        "from": CALLER_ID,
        "record": cfg.record,
        "task": (
            f"{policy.build_intro(reason)} "
            f"{cfg.screener_instructions}"
        ),
        "voicemail_action": "hangup",  # Attempt 1: hang up silent
        "max_duration": 12,  # minutes
    }
