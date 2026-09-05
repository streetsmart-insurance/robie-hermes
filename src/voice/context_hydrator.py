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

logger = logging.getLogger("voice_context_hydrator")


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

        return CallingDossier(
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
        )

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
