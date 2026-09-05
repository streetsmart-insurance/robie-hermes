"""
Context Hydrator for Autonomous Voice Outreach.
Gathers all necessary policy metadata, carrier phone numbers, agency codes,
and contact info from renewals.db, EZLynx API, and carrier directories.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.config import BASE_DIR, settings
from src.database.models import PolicyRenewal
from src.database.session import SessionLocal
from src.voice.call_directory import lookup_producer, lookup_requestor

logger = logging.getLogger("voice_context_hydrator")

CALL_TYPE_CARRIER = "carrier"
CALL_TYPE_CLIENT_FOLLOWUP = "client_followup"
TRANSFER_MODE_WARM = "warm"

_PRODUCER_NAME_KEYS = (
    "Producer",
    "ProducerName",
    "AssignedProducer",
    "producer",
    "producerName",
    "assignedProducer",
    "ProducerFullName",
)
_PRODUCER_EMAIL_KEYS = (
    "ProducerEmail",
    "producerEmail",
    "ProducerMail",
)
_FIRST_NAME_KEYS = (
    "FirstName",
    "firstName",
    "PreferredName",
    "PreferredFirstName",
    "preferredName",
    "NickName",
    "Nickname",
    "nickname",
)
_BUSINESS_NAME_RE = re.compile(
    r"\b(llc|inc|corp|ltd|lp|plc|dba|company|co|insurance|agency|group|"
    r"services|enterprises|associates|holdings)\b",
    re.IGNORECASE,
)


def normalize_call_type(raw: Optional[str]) -> str:
    """Map note/email cues onto client_followup | carrier."""
    if not raw:
        return CALL_TYPE_CARRIER
    cleaned = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    if cleaned.startswith("client"):
        return CALL_TYPE_CLIENT_FOLLOWUP
    return CALL_TYPE_CARRIER


def _looks_like_business_name(name: str) -> bool:
    return bool(_BUSINESS_NAME_RE.search(name))


def extract_client_first_name(
    applicant: Optional[Dict[str, Any]] = None,
    insured_name: Optional[str] = None,
) -> Optional[str]:
    """First name from EZLynx FirstName, then preferred/nickname, then personal display name."""
    if applicant:
        for key in _FIRST_NAME_KEYS:
            value = applicant.get(key)
            if isinstance(value, str) and value.strip():
                token = value.strip().split()[0]
                if token and not _looks_like_business_name(token):
                    return token
    if insured_name and not _looks_like_business_name(insured_name):
        token = insured_name.strip().split()[0]
        if token:
            return token
    return None


def _producer_string_from_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("Name", "FullName", "DisplayName", "name", "fullName"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip() and "@" not in nested:
                return nested.strip()
        return None
    if isinstance(value, str) and value.strip() and "@" not in value:
        return value.strip()
    return None


def extract_producer_name(*sources: Optional[Dict[str, Any]]) -> Optional[str]:
    """Read the EZLynx Producer field from applicant and/or policy objects. No free-text guess."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _PRODUCER_NAME_KEYS:
            name = _producer_string_from_value(source.get(key))
            if name:
                return name
    return None


def extract_producer_email(*sources: Optional[Dict[str, Any]]) -> Optional[str]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _PRODUCER_EMAIL_KEYS:
            value = source.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip()
        producer = source.get("Producer") or source.get("producer")
        if isinstance(producer, str) and "@" in producer:
            return producer.strip()
        if isinstance(producer, dict):
            for key in ("Email", "email", "EMail"):
                value = producer.get(key)
                if isinstance(value, str) and "@" in value:
                    return value.strip()
    return None


def unwrap_policy_list(policies_res: Any) -> List[Dict[str, Any]]:
    if isinstance(policies_res, list):
        return [p for p in policies_res if isinstance(p, dict)]
    if isinstance(policies_res, dict):
        for key in ("policies", "Policies", "data"):
            inner = policies_res.get(key)
            if isinstance(inner, list):
                return [p for p in inner if isinstance(p, dict)]
            if isinstance(inner, dict):
                nested = inner.get("policies") or inner.get("Policies")
                if isinstance(nested, list):
                    return [p for p in nested if isinstance(p, dict)]
    return []


def match_policy_record(
    policies: List[Dict[str, Any]], policy_number: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not policies:
        return None
    if not policy_number:
        return policies[0]
    needle = str(policy_number).strip().upper()
    for pol in policies:
        for key in ("policyNumber", "PolicyNumber", "PolicyNum"):
            value = pol.get(key)
            if value and needle in str(value).upper():
                return pol
    return policies[0]


@dataclass
class CallingDossier:
    policy_number: str
    insured_name: str
    carrier_name: str
    line_of_business: str
    expiration_date: Optional[str] = None
    expiring_premium: Optional[float] = None
    carrier_phone: Optional[str] = None
    carrier_extension: Optional[str] = None
    agency_code: Optional[str] = None
    applicant_id: Optional[int] = None
    assigned_csr_email: Optional[str] = None
    custom_instructions: Optional[str] = None
    ivr_instructions: Optional[str] = None
    client_first_name: Optional[str] = None
    producer_name: Optional[str] = None
    producer_phone: Optional[str] = None
    requestor_name: Optional[str] = None
    requestor_email: Optional[str] = None
    requestor_phone: Optional[str] = None
    call_type: str = CALL_TYPE_CARRIER
    transfer_mode: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_number": self.policy_number,
            "insured_name": self.insured_name,
            "carrier_name": self.carrier_name,
            "line_of_business": self.line_of_business,
            "expiration_date": self.expiration_date,
            "expiring_premium": self.expiring_premium,
            "carrier_phone": self.carrier_phone,
            "carrier_extension": self.carrier_extension,
            "agency_code": self.agency_code,
            "applicant_id": self.applicant_id,
            "assigned_csr_email": self.assigned_csr_email,
            "custom_instructions": self.custom_instructions,
            "ivr_instructions": self.ivr_instructions,
            "client_first_name": self.client_first_name,
            "producer_name": self.producer_name,
            "producer_phone": self.producer_phone,
            "requestor_name": self.requestor_name,
            "requestor_email": self.requestor_email,
            "requestor_phone": self.requestor_phone,
            "call_type": self.call_type,
            "transfer_mode": self.transfer_mode,
        }


class ContextHydrator:
    def __init__(
        self,
        carrier_directory_path: Optional[Path] = None,
        ezlynx_directory_path: Optional[Path] = None,
    ):
        self.carrier_dir_path = carrier_directory_path or (BASE_DIR / "data" / "carrier_directory.json")
        self.ezlynx_dir_path = ezlynx_directory_path or (BASE_DIR / "data" / "ezlynx_full_extracted_directory.json")
        self._carrier_dict: Dict[str, Any] = self._load_json(self.carrier_dir_path)
        self._ezlynx_dict: Dict[str, Any] = self._load_json(self.ezlynx_dir_path)

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Could not load JSON from {path}: {e}")
        return {}

    def hydrate(
        self,
        policy_number: Optional[str] = None,
        applicant_name: Optional[str] = None,
        phone_override: Optional[str] = None,
        instructions: Optional[str] = None,
        requester_email: Optional[str] = None,
        requestor_name: Optional[str] = None,
        requestor_email: Optional[str] = None,
        call_type: Optional[str] = None,
        applicant_profile: Optional[Dict[str, Any]] = None,
        policy_profile: Optional[Dict[str, Any]] = None,
        enrich_from_ezlynx: bool = False,
    ) -> Optional[CallingDossier]:
        """
        Hydrates a full CallingDossier given partial input (policy number or applicant name).
        Searches renewals.db first, then falls back to EZLynx API if available.
        """
        pol_record: Optional[PolicyRenewal] = None

        # 1. Search local renewals.db
        db = SessionLocal()
        try:
            if policy_number:
                clean_pol = policy_number.strip().upper()
                pol_record = (
                    db.query(PolicyRenewal)
                    .filter(PolicyRenewal.policy_number.ilike(f"%{clean_pol}%"))
                    .first()
                )
            elif applicant_name:
                clean_name = applicant_name.strip()
                pol_record = (
                    db.query(PolicyRenewal)
                    .filter(PolicyRenewal.insured_name.ilike(f"%{clean_name}%"))
                    .first()
                )
        except Exception as exc:
            logger.debug("renewals.db lookup skipped: %s", exc)
            pol_record = None
        finally:
            db.close()

        # 2. Extract base fields from record or fallback
        if pol_record:
            pol_num = pol_record.policy_number
            insured = pol_record.insured_name
            carrier = pol_record.carrier_name
            lob = pol_record.line_of_business or "Commercial"
            exp_date = pol_record.expiration_date.strftime("%Y-%m-%d") if pol_record.expiration_date else None
            exp_prem = pol_record.expiring_premium
            app_id = pol_record.applicant_id
            csr_email = (
                self._resolve_csr_email(pol_record.assigned_agent)
                or requester_email
                or "carlo@streetsmart.insurance"
            )
        else:
            # Fallback to direct EZLynx API search if credentials configured
            api_dossier = self._search_ezlynx_api(policy_number or applicant_name)
            if api_dossier:
                pol_num = api_dossier.get("policy_number", policy_number or "UNKNOWN")
                insured = api_dossier.get("insured_name", applicant_name or "UNKNOWN")
                carrier = api_dossier.get("carrier_name", "UNKNOWN")
                lob = api_dossier.get("line_of_business", "Commercial")
                exp_date = api_dossier.get("expiration_date")
                exp_prem = api_dossier.get("expiring_premium")
                app_id = api_dossier.get("applicant_id")
                csr_email = requester_email or "carlo@streetsmart.insurance"
            else:
                # Minimal fallback when record not found
                if not policy_number and not applicant_name:
                    return None
                pol_num = policy_number or "UNKNOWN"
                insured = applicant_name or "Policyholder"
                carrier = "Unknown Carrier"
                lob = "Commercial"
                exp_date = None
                exp_prem = None
                app_id = None
                csr_email = requester_email or "carlo@streetsmart.insurance"

        # 3. Resolve carrier phone, extension, agency code, and IVR details
        carrier_info = self._resolve_carrier_contact(carrier)
        resolved_phone = phone_override or carrier_info.get("phone")
        resolved_ext = carrier_info.get("extension")
        resolved_agency_code = carrier_info.get("agency_code")
        ivr_notes = carrier_info.get("ivr_instructions")

        dossier = CallingDossier(
            policy_number=pol_num,
            insured_name=insured,
            carrier_name=carrier,
            line_of_business=lob,
            expiration_date=exp_date,
            expiring_premium=exp_prem,
            carrier_phone=resolved_phone,
            carrier_extension=resolved_ext,
            agency_code=resolved_agency_code,
            applicant_id=app_id,
            assigned_csr_email=csr_email,
            custom_instructions=instructions,
            ivr_instructions=ivr_notes,
            call_type=normalize_call_type(call_type),
            requestor_name=requestor_name,
            requestor_email=requestor_email or requester_email,
        )
        self.enrich_identity(
            dossier,
            applicant=applicant_profile,
            policy=policy_profile,
            call_type=call_type,
            requestor_name=requestor_name,
            requestor_email=requestor_email or requester_email,
        )
        if enrich_from_ezlynx:
            self.enrich_identity_from_ezlynx(dossier)
        return dossier

    def enrich_identity(
        self,
        dossier: CallingDossier,
        applicant: Optional[Dict[str, Any]] = None,
        policy: Optional[Dict[str, Any]] = None,
        call_type: Optional[str] = None,
        requestor_name: Optional[str] = None,
        requestor_email: Optional[str] = None,
    ) -> CallingDossier:
        """Apply EZLynx FirstName + Producer (greeting) and requestor transfer.

        Producer comes only from the EZLynx Producer field and is greeting copy
        ("quote {Producer} put together"). Warm-transfer phone is the Robie Call
        invoker (note author / email sender). Missing requestor phone skips
        transfer — never fall back to the Producer or another staff DID.
        """
        if call_type:
            dossier.call_type = normalize_call_type(call_type)
        if requestor_name:
            dossier.requestor_name = requestor_name
        if requestor_email:
            dossier.requestor_email = requestor_email

        if applicant:
            dossier.client_first_name = extract_client_first_name(
                applicant, dossier.insured_name
            )
            if not dossier.producer_name:
                dossier.producer_name = extract_producer_name(policy, applicant)
            producer_email = extract_producer_email(policy, applicant)
            match = lookup_producer(name=dossier.producer_name, email=producer_email)
            if match:
                dossier.producer_name = match.get("name") or dossier.producer_name
                # Greeting-only: do not use this phone for Bland transfer.
                dossier.producer_phone = None
        elif dossier.producer_name:
            match = lookup_producer(name=dossier.producer_name)
            if match:
                dossier.producer_name = match.get("name") or dossier.producer_name
            dossier.producer_phone = None

        self._apply_requestor_transfer(dossier)
        return dossier

    def _apply_requestor_transfer(self, dossier: CallingDossier) -> CallingDossier:
        """Look up the label invoker / email sender. No Producer fallback."""
        name = dossier.requestor_name
        email = dossier.requestor_email
        if not name and not email:
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        if name and "robie" in str(name).lower():
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        match = lookup_requestor(name=name, email=email)
        if not match:
            logger.info(
                "Requestor %s <%s> is not in the voice directory; skipping warm transfer.",
                name,
                email,
            )
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        dossier.requestor_name = match.get("name") or dossier.requestor_name
        dossier.requestor_email = match.get("email") or dossier.requestor_email
        phone = match.get("phone")
        if not phone:
            logger.info(
                "Requestor %s has no E.164 DID in the voice directory; skipping warm transfer.",
                dossier.requestor_name,
            )
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        dossier.requestor_phone = phone
        dossier.transfer_mode = TRANSFER_MODE_WARM
        return dossier

    def enrich_identity_from_ezlynx(
        self,
        dossier: CallingDossier,
        ezlynx_client: Optional[Any] = None,
    ) -> CallingDossier:
        """Fetch applicant/policy from EZLynx and hydrate first name + Producer field."""
        if not dossier.applicant_id:
            return dossier
        try:
            from src.ezlynx.api_client import EZLynxApiClient

            client = ezlynx_client or EZLynxApiClient()
            app_res = client.get_applicant(str(dossier.applicant_id))
            applicant = app_res.get("applicant") if app_res.get("status") == "success" else None
            policy = None
            try:
                pol_res = client.get_applicant_policies(str(dossier.applicant_id))
                policy = match_policy_record(
                    unwrap_policy_list(pol_res), dossier.policy_number
                )
            except Exception as exc:
                logger.debug("EZLynx policy lookup for producer skipped: %s", exc)
            return self.enrich_identity(dossier, applicant=applicant, policy=policy)
        except Exception as exc:
            logger.debug("EZLynx identity enrichment failed gracefully: %s", exc)
            return dossier

    def _resolve_carrier_contact(self, carrier_name: str) -> Dict[str, Any]:
        """Looks up carrier contact info across carrier_directory.json and ezlynx_full_extracted_directory.json."""
        res: Dict[str, Any] = {
            "phone": None,
            "extension": None,
            "agency_code": None,
            "ivr_instructions": None,
        }
        if not carrier_name or carrier_name == "UNKNOWN":
            return res

        clean_carrier = carrier_name.strip().lower()

        # Check carrier_directory.json first (contains curated agent notes & IVR prompts)
        for name, data in self._carrier_dict.items():
            if name.lower() in clean_carrier or clean_carrier in name.lower():
                notes = data.get("notes", "")
                res["agency_code"] = self._extract_agency_code(notes)
                phone_match = self._extract_phone_from_text(notes)
                if phone_match:
                    res["phone"] = phone_match
                res["ivr_instructions"] = self._extract_ivr_instructions(notes)
                break

        # Check ezlynx_full_extracted_directory.json for official phone/ext if phone not found
        for org_name, org_data in self._ezlynx_dict.items():
            if org_name.lower() in clean_carrier or clean_carrier in org_name.lower():
                directory = org_data.get("directory", {})
                ez_phone = directory.get("phone")
                if ez_phone and not res["phone"]:
                    res["phone"] = self._format_phone(ez_phone)
                ez_ext = directory.get("extension")
                if ez_ext and not res["extension"]:
                    res["extension"] = str(ez_ext).strip()
                break

        return res

    @staticmethod
    def _extract_phone_from_text(text: str) -> Optional[str]:
        """Extracts 10-digit phone number formatted as XXX-XXX-XXXX or (XXX) XXX-XXXX."""
        matches = re.findall(r"(?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})", text)
        for m in matches:
            digits = "".join(m)
            if len(digits) == 10:
                return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return None

    @staticmethod
    def _format_phone(raw_phone: str) -> str:
        digits = re.sub(r"\D", "", raw_phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return raw_phone

    @staticmethod
    def _extract_agency_code(notes: str) -> Optional[str]:
        if "X4688" in notes:
            return "X4688"
        if "13653283" in notes:
            return "13653283"
        # Match pattern like Agent Code: 0CHN19 or Agent Code Agt8572 or Code: 7723Y
        matches = re.findall(r"(?:Agent Code|Agency Code|Company Code|Code)[:\s]+([A-Z0-9\-]+)", notes, re.IGNORECASE)
        for m in matches:
            val = m.strip()
            if val.lower() not in ["personal", "commercial", "use", "see", "agent", "this", "value", "proposition", "none"]:
                return val
        return None

    @staticmethod
    def _extract_ivr_instructions(notes: str) -> Optional[str]:
        lines = []
        for line in notes.splitlines():
            if any(k in line.lower() for k in ["press ", "extension", "help line", "line", "option"]):
                lines.append(line.strip())
        return " | ".join(lines) if lines else None

    @staticmethod
    def _resolve_csr_email(agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        low = agent_name.lower()
        if "jake" in low:
            return "jake@streetsmart.insurance"
        if "eimy" in low:
            return "eimy@streetsmart.insurance"
        if "sandy" in low:
            return "sandy@streetsmart.insurance"
        if "nicole" in low:
            return "nicole@streetsmart.insurance"
        if "carlo" in low:
            return "carlo@streetsmart.insurance"
        return None

    def _search_ezlynx_api(self, query: Optional[str]) -> Optional[Dict[str, Any]]:
        """Invokes EZLynx search if available."""
        if not query:
            return None
        try:
            from src.ezlynx.api_client import EZLynxApiClient
            client = EZLynxApiClient()
            results = client.search_applicant(query)
            if not results:
                return None
            first_match = results[0]
            app_id = first_match.get("applicantId") or first_match.get("id")
            insured_name = first_match.get("applicantName") or first_match.get("name")

            if app_id:
                policies = client.get_applicant_policies(app_id)
                for pol in policies:
                    pol_num = pol.get("policyNumber")
                    if not query or query.upper() in (pol_num or "").upper():
                        return {
                            "applicant_id": app_id,
                            "insured_name": insured_name,
                            "policy_number": pol_num,
                            "carrier_name": pol.get("carrierName"),
                            "line_of_business": pol.get("lobName"),
                            "expiration_date": pol.get("expirationDate"),
                            "expiring_premium": pol.get("premium"),
                        }
        except Exception as e:
            logger.debug(f"EZLynx API search failed gracefully: {e}")
        return None
