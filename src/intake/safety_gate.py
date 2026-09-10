"""Renewal Safety Gate for detecting cancellation requests, non-renewal intent, and BORs."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

CANCELLATION_KEYWORDS = [
    "cancellation",
    "cancel",
    "cancelling",
    "cancelled",
    "lost policy",
    "lost-policy",
    "lcr",
    "do not renew",
    "don't renew",
    "dont renew",
    "not renewing",
    "non-renewal",
    "nonrenewal",
    "non renewal",
    "broker of record",
    "bor",
]

# Compile regex pattern with word boundaries for precision
CANCELLATION_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in CANCELLATION_KEYWORDS) + r")\b",
    re.IGNORECASE
)


@dataclass
class SafetyCheckResult:
    is_safe: bool
    risk_detected: bool
    reason: Optional[str] = None
    flags: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)


class RenewalSafetyGate:
    """Pre-flight safety inspection gate to prevent quoting, shell-keying, or
    outreach on accounts where an active cancellation request or non-renewal
    intent exists.
    """

    def __init__(self, ezlynx_client: Optional[Any] = None):
        if ezlynx_client is None:
            from src.ezlynx.api_client import EZLynxApiClient
            self.ezlynx = EZLynxApiClient()
        else:
            self.ezlynx = ezlynx_client

    def check_cancellation_risk(
        self,
        applicant_id: str,
        policy_number: Optional[str] = None,
        lookback_discussions: int = 15,
        lookback_docs: int = 25
    ) -> SafetyCheckResult:
        """Inspects EZLynx discussions and documents for cancellation or non-renewal signals.

        Returns SafetyCheckResult with is_safe=False if a risk is detected.
        """
        app_id_str = str(applicant_id)
        flags: List[str] = []
        evidence: List[Dict[str, Any]] = []

        # Clean policy number for candidate matching
        pol_clean = re.sub(r"[^a-zA-Z0-9]", "", policy_number or "").lower()

        # 1. Inspect Applicant Discussions
        try:
            discussions = self.ezlynx.get_applicant_discussions(app_id_str)
            if discussions:
                for disc in discussions[:lookback_discussions]:
                    title = disc.get("title", "")
                    disc_id = disc.get("discussionId")
                    
                    # Check discussion title
                    match_title = CANCELLATION_REGEX.search(title)
                    if match_title:
                        # Check if policy matches or if it's general
                        title_clean = re.sub(r"[^a-zA-Z0-9]", "", title).lower()
                        is_policy_specific = bool(pol_clean and pol_clean in title_clean)
                        flag_msg = f"Discussion title '{title}' contains cancellation keyword '{match_title.group(0)}'"
                        flags.append(flag_msg)
                        evidence.append({
                            "source": "discussion_title",
                            "discussion_id": disc_id,
                            "title": title,
                            "matched_keyword": match_title.group(0),
                            "policy_specific": is_policy_specific
                        })

                    # Check discussion note snippet / description
                    note_info = disc.get("discussionNote") or {}
                    note_desc = note_info.get("noteDescription", "")
                    if note_desc:
                        match_note = CANCELLATION_REGEX.search(note_desc)
                        if match_note:
                            flags.append(f"Discussion note in '{title}' contains '{match_note.group(0)}'")
                            evidence.append({
                                "source": "discussion_note",
                                "discussion_id": disc_id,
                                "snippet": note_desc[:200],
                                "matched_keyword": match_note.group(0)
                            })
        except Exception as e:
            logger.warning(f"SafetyGate: Error checking discussions for applicant {app_id_str}: {e}")

        # 2. Inspect Documents Tab
        try:
            docs = self.ezlynx.list_applicant_documents(app_id_str)
            if docs:
                for doc in docs[:lookback_docs]:
                    fname = doc.get("fileName") or doc.get("name") or ""
                    desc = doc.get("description") or ""
                    
                    match_fname = CANCELLATION_REGEX.search(fname)
                    match_desc = CANCELLATION_REGEX.search(desc) if desc else None
                    
                    if match_fname or match_desc:
                        matched_kw = match_fname.group(0) if match_fname else match_desc.group(0)
                        fname_clean = re.sub(r"[^a-zA-Z0-9]", "", fname).lower()
                        is_policy_specific = bool(pol_clean and pol_clean in fname_clean)
                        flags.append(f"Document '{fname}' contains cancellation keyword '{matched_kw}'")
                        evidence.append({
                            "source": "document",
                            "file_name": fname,
                            "description": desc,
                            "matched_keyword": matched_kw,
                            "policy_specific": is_policy_specific
                        })
        except Exception as e:
            logger.warning(f"SafetyGate: Error checking documents for applicant {app_id_str}: {e}")

        if flags:
            reason = f"Active cancellation/non-renewal signal detected on account ({len(flags)} indicator(s)): {flags[0]}"
            logger.warning(f"[SAFETY GATE] Applicant {app_id_str} (Policy {policy_number}): BLOCKED. Reason: {reason}")
            return SafetyCheckResult(
                is_safe=False,
                risk_detected=True,
                reason=reason,
                flags=flags,
                evidence=evidence
            )

        logger.info(f"[SAFETY GATE] Applicant {app_id_str} (Policy {policy_number}): PASSED safety inspection.")
        return SafetyCheckResult(is_safe=True, risk_detected=False)


class AuditEligibilityGate:
    """Hardened gate enforcing that only auditable lines of business (Workers' Comp)
    enter the audit verification workflow, blocking Commercial Auto, Cargo, Personal lines, etc.
    """
    NON_AUDITABLE_LOBS = {
        "commercial auto", "auto (commercial)", "autob", "autop", "personal auto",
        "cargo", "motor truck cargo", "auto physical damage", "apd", "fleet",
        "homeowners", "home", "dwelling", "bonds", "bonds miscellaneous", "bmisc",
        "commercial umbrella", "umbrella - comm", "cumbr", "business owners", "bop",
        "business owners policy"
    }
    AUDITABLE_LOBS = {
        "workers comp", "workers' comp", "workers compensation", "workers' compensation",
        "work", "wc"
    }

    @classmethod
    def is_eligible_for_audit(cls, lob: Optional[str]) -> Tuple[bool, Optional[str]]:
        if not lob:
            return False, "Line of Business is missing."
        lob_lower = lob.lower().strip()
        if any(non in lob_lower for non in cls.NON_AUDITABLE_LOBS) and not any(aud in lob_lower for aud in cls.AUDITABLE_LOBS):
            return False, f"Line of Business '{lob}' is not subject to payroll audit."
        if not any(aud in lob_lower for aud in cls.AUDITABLE_LOBS):
            return False, f"Line of Business '{lob}' does not match auditable Workers' Compensation criteria."
        return True, None

