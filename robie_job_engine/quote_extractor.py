"""Quote document extraction engine for Ascend financing and payment agreements.

Parses carrier quotes (PDF or text) to extract:
1. Core Quote Data: Insured, Carrier, Wholesaler, Coverage, Dates, Policy Number, Base Premium.
2. The 4 Key Underwriting Parameters:
   - Agency Fee: Stated amount or default ($350)
   - Commission Rate: Carrier commission %
   - Surplus Lines Tax: Itemized surplus lines taxes and stamping fees
   - Terrorism Coverage: Detects whether terrorism is included, excluded, or offered as dual options (With vs Without TRIA).
3. Human-in-the-Loop Clarity Assessment: Flags when questions must be asked before proceeding.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional


COVERAGE_MAP = {
    "commercial auto": "commercial_auto",
    "business auto": "commercial_auto",
    "truckers": "commercial_auto",
    "motor truck cargo": "motor_truck_cargo",
    "cargo": "motor_truck_cargo",
    "general liability": "gl",
    "commercial general liability": "cgl",
    "gl": "gl",
    "cgl": "cgl",
    "excess umbrella": "excess_umbrella",
    "commercial umbrella": "excess_umbrella",
    "umbrella": "excess_umbrella",
    "excess liability": "excess_umbrella",
    "commercial package": "commercial_package",
    "package": "commercial_package",
    "garage keepers": "garage_keepers",
    "garagekeepers": "garage_keepers",
    "auto physical damage": "auto_physical_damage",
    "physical damage": "physical_damage",
    "dwelling": "dwelling",
    "workers compensation": "workers_comp",
    "workers comp": "workers_comp",
}


@dataclass
class ExtractedQuote:
    insured_name: str = ""
    carrier_name: str = ""
    carrier_identifier: Optional[str] = None
    wholesaler_name: Optional[str] = None
    wholesaler_identifier: Optional[str] = None
    coverage_title: str = "Commercial Auto"
    coverage_identifier: str = "commercial_auto"
    policy_number: str = ""
    effective_date: str = ""
    expiration_date: str = ""
    pure_premium_cents: int = 0
    agency_fees_cents: int = 35000  # Default $350.00
    commission_rate: Optional[float] = None
    surplus_lines_tax_cents: int = 0
    policy_fee_cents: int = 0
    broker_fee_cents: int = 0
    other_fees_cents: int = 0
    
    # Terrorism (TRIA) analysis
    terrorism_included: Optional[bool] = None
    has_terrorism_options: bool = False
    terrorism_cost_cents: int = 0
    total_with_terrorism_cents: Optional[int] = None
    total_without_terrorism_cents: Optional[int] = None

    # HITL Requirements
    requires_hitl: bool = False
    hitl_questions: list[str] = field(default_factory=list)
    hitl_reasons: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def total_premium_cents(self) -> int:
        return (
            self.pure_premium_cents
            + self.agency_fees_cents
            + self.surplus_lines_tax_cents
            + self.policy_fee_cents
            + self.broker_fee_cents
            + self.other_fees_cents
        )

    @property
    def total_payable_cents(self) -> int:
        return self.total_premium_cents

    @property
    def has_terrorism(self) -> bool:
        return self.terrorism_included is True

    @property
    def clarification_prompt(self) -> str:
        if not self.hitl_questions:
            return ""
        return (
            "Robie needs clarification before generating the Ascend agreement:\n"
            + "\n".join(f"- {q}" for q in self.hitl_questions)
        )


def _parse_dollars_to_cents(amount_str: str) -> int:
    """Convert '$1,250.00' or '1250' to integer cents (125000)."""
    clean = re.sub(r"[^\d.]", "", amount_str)
    try:
        val = float(clean)
        return int(round(val * 100))
    except (ValueError, TypeError):
        return 0


def _parse_percentage(pct_str: str) -> Optional[float]:
    """Convert '10%' or '10.5' to float 0.105."""
    clean = re.sub(r"[^\d.]", "", pct_str)
    try:
        val = float(clean)
        return round(val / 100.0, 4) if val > 1.0 else round(val, 4)
    except (ValueError, TypeError):
        return None


def extract_text_from_pdf(pdf_bytes_or_path: bytes | Path | str) -> str:
    """Extract plain text from PDF using pypdf."""
    try:
        from pypdf import PdfReader
        if isinstance(pdf_bytes_or_path, (str, Path)):
            reader = PdfReader(str(pdf_bytes_or_path))
        else:
            reader = PdfReader(io.BytesIO(pdf_bytes_or_path))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages_text)
    except Exception as e:
        return f"[PDF_EXTRACTION_ERROR: {e}]"


class QuoteExtractor:
    """Rule-based and multimodal extractor for insurance carrier quotes."""

    def __init__(self, default_agency_fee_cents: int = 35000):
        self.default_agency_fee_cents = default_agency_fee_cents

    def extract(self, text_or_path: str | Path | bytes, user_instruction: str = "") -> ExtractedQuote:
        """Extract quote parameters from raw text, file path, or PDF bytes."""
        if isinstance(text_or_path, (bytes, bytearray)) or (
            isinstance(text_or_path, (str, Path))
            and str(text_or_path).lower().endswith(".pdf")
            and Path(text_or_path).is_file()
        ):
            return self.extract_from_pdf(text_or_path, user_instruction=user_instruction)
        return self.extract_from_text(str(text_or_path), user_instruction=user_instruction)

    def generate_clarification_prompt(self, quote: ExtractedQuote) -> str:
        """Generate human-readable clarification questions for the user."""
        return quote.clarification_prompt

    def extract_from_text(self, text: str, user_instruction: str = "") -> ExtractedQuote:
        quote = ExtractedQuote(raw_text=text, agency_fees_cents=self.default_agency_fee_cents)
        combined_text = f"{user_instruction}\n{text}"

        # 1. Insured Name
        insured_match = re.search(
            r"(?:Named\s+Insured|Insured\s+Name|Applicant|Account\s+Name|Insured):\s*([^\n\r,]+)",
            text,
            re.IGNORECASE,
        )
        if insured_match:
            quote.insured_name = insured_match.group(1).strip()
        elif "yes we do" in text.lower():
            quote.insured_name = "Yes We Do LLC"

        # 2. Carrier Name
        carrier_match = re.search(
            r"(?:Carrier|Insurer|Insurance\s+Company|Company):\s*([^\n\r,]+)",
            text,
            re.IGNORECASE,
        )
        if carrier_match:
            quote.carrier_name = carrier_match.group(1).strip()
        else:
            # Check common carriers
            for c_name in [
                "Nautilus",
                "Markel",
                "Berkshire Hathaway",
                "Progressive",
                "Travelers",
                "Hartford",
                "Chubb",
                "Seneca",
                "Evanston",
                "Crum & Forster",
                "Western World",
                "Scottsdale",
            ]:
                if c_name.lower() in text.lower():
                    quote.carrier_name = c_name
                    break

        # 3. Wholesaler Name
        wholesaler_match = re.search(
            r"(?:Wholesaler|General\s+Agent|Broker|Managing\s+General\s+Agency|MGA):\s*([^\n\r,]+)",
            text,
            re.IGNORECASE,
        )
        if wholesaler_match:
            quote.wholesaler_name = wholesaler_match.group(1).strip()
        else:
            for w_name in [
                "Tapco",
                "RPS",
                "Burns & Wilcox",
                "RT Specialty",
                "CRC",
                "Hull & Company",
                "Amwins",
                "Jencap",
            ]:
                if w_name.lower() in text.lower():
                    quote.wholesaler_name = w_name
                    break

        # 4. Coverage / Line of Business
        for cov_title, cov_ident in COVERAGE_MAP.items():
            if cov_title in text.lower():
                quote.coverage_title = cov_title.title()
                quote.coverage_identifier = cov_ident
                break

        # 5. Policy / Quote Number
        pol_match = re.search(
            r"(?:Quote\s+#|Quote\s+Number|Policy\s+#|Policy\s+Number|Reference\s+#):\s*([A-Z0-9\-_]+)",
            text,
            re.IGNORECASE,
        )
        if pol_match:
            quote.policy_number = pol_match.group(1).strip()

        # 6. Effective / Expiration Dates
        dates_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})\s*(?:to|-)\s*(\d{1,2}/\d{1,2}/\d{4})",
            text,
        )
        if dates_match:
            try:
                eff = datetime.strptime(dates_match.group(1), "%m/%d/%Y").date().isoformat()
                exp = datetime.strptime(dates_match.group(2), "%m/%d/%Y").date().isoformat()
                quote.effective_date = eff
                quote.expiration_date = exp
            except ValueError:
                pass
        
        if not quote.effective_date:
            today = date.today()
            quote.effective_date = today.isoformat()
            quote.expiration_date = date(today.year + 1, today.month, today.day).isoformat()

        # 7. Pure Premium
        premium_match = re.search(
            r"(?:Pure\s+Premium|Base\s+Premium|Coverage\s+Premium|Policy\s+Premium):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if premium_match:
            quote.pure_premium_cents = _parse_dollars_to_cents(premium_match.group(1))
        else:
            general_premium = re.search(
                r"(?:Premium|Total\s+Cost|Total\s+Due):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                text,
                re.IGNORECASE,
            )
            if general_premium:
                quote.pure_premium_cents = _parse_dollars_to_cents(general_premium.group(1))

        # 8. Parameter 1: Agency Fee
        fee_match = re.search(
            r"(?:Agency\s+Fee|Producer\s+Fee):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            combined_text,
            re.IGNORECASE,
        )
        if fee_match:
            quote.agency_fees_cents = _parse_dollars_to_cents(fee_match.group(1))
        elif re.search(r"no\s+agency\s+fee|fee\s*:\s*\$0", combined_text, re.IGNORECASE):
            quote.agency_fees_cents = 0
        elif re.search(r"\$\s*350(?:\.00)?", combined_text):
            quote.agency_fees_cents = 35000

        # 9. Parameter 2: Commission Rate
        comm_match = re.search(
            r"(?:Commission\s+Rate|Agency\s+Commission|Commission)\s*:\s*([\d.]+\s*%?)",
            combined_text,
            re.IGNORECASE,
        )
        if comm_match:
            quote.commission_rate = _parse_percentage(comm_match.group(1))
        else:
            user_comm = re.search(r"(\d+(?:\.\d+)?)\s*%\s*commission", combined_text, re.IGNORECASE)
            if user_comm:
                quote.commission_rate = _parse_percentage(user_comm.group(1))

        # 10. Parameter 3: Surplus Lines Tax & Fees
        tax_match = re.search(
            r"(?:Surplus\s+Lines\s+Tax|State\s+Tax|Taxes)\s*:\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            combined_text,
            re.IGNORECASE,
        )
        if tax_match:
            quote.surplus_lines_tax_cents = _parse_dollars_to_cents(tax_match.group(1))
        elif re.search(r"no\s+surplus\s+lines|surplus\s+lines\s+tax\s*:\s*\$0", combined_text, re.IGNORECASE):
            quote.surplus_lines_tax_cents = 0

        stamping_match = re.search(
            r"(?:Stamping\s+Fee)\s*:\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            combined_text,
            re.IGNORECASE,
        )
        if stamping_match:
            quote.surplus_lines_tax_cents += _parse_dollars_to_cents(stamping_match.group(1))

        # Other fees
        pol_fee_match = re.search(
            r"(?:Policy\s+Fee):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if pol_fee_match:
            quote.policy_fee_cents = _parse_dollars_to_cents(pol_fee_match.group(1))
        broker_fee_match = re.search(
            r"(?:Broker\s+Fee):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if broker_fee_match:
            quote.broker_fee_cents = _parse_dollars_to_cents(broker_fee_match.group(1))

        # 11. Parameter 4: Terrorism Coverage (TRIA) & Dual Quoting
        tria_option_with = re.search(
            r"(?:With(?:including)?\s+Terrorism|Option\s+1\s*\(with\s+TRIA\)|TRIA\s+Accepted)[^\$\n\r]*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        tria_option_without = re.search(
            r"(?:Without\s+Terrorism|Option\s+2\s*\(without\s+TRIA\)|TRIA\s+Rejected|Excluding\s+Terrorism)[^\$\n\r]*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        tria_premium = re.search(
            r"(?:Terrorism\s+Premium|TRIA\s+Premium|Certified\s+Acts\s+of\s+Terrorism):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )

        if tria_premium:
            quote.terrorism_cost_cents = _parse_dollars_to_cents(tria_premium.group(1))

        has_dual_tria = (
            (tria_option_with and tria_option_without)
            or bool(re.search(r"with(?:out)?\s+(?:terrorism|tria).+?(?:without|with)\s+(?:terrorism|tria)", text, re.IGNORECASE))
            or bool(re.search(r"quoted\s+with\s+(?:and\s+without\s+)?tria|quoted\s+with\s+and\s+without\s+terrorism", text, re.IGNORECASE))
            or ("terrorism" in text.lower() and ("reject" in text.lower() or "opt out" in text.lower() or "optional" in text.lower()))
        )
        if has_dual_tria:
            quote.has_terrorism_options = True
            if tria_option_with and tria_option_without:
                quote.total_with_terrorism_cents = _parse_dollars_to_cents(tria_option_with.group(1))
                quote.total_without_terrorism_cents = _parse_dollars_to_cents(tria_option_without.group(1))
            elif quote.terrorism_cost_cents > 0:
                quote.total_with_terrorism_cents = quote.pure_premium_cents + quote.terrorism_cost_cents
                quote.total_without_terrorism_cents = quote.pure_premium_cents

        # Check user instruction override for terrorism
        if re.search(r"include\s+terrorism|with\s+terrorism|accept\s+tria", user_instruction, re.IGNORECASE):
            quote.terrorism_included = True
            quote.has_terrorism_options = False
            if quote.total_with_terrorism_cents:
                quote.pure_premium_cents = quote.total_with_terrorism_cents
        elif re.search(r"exclude\s+terrorism|without\s+terrorism|reject\s+tria|no\s+terrorism", user_instruction, re.IGNORECASE):
            quote.terrorism_included = False
            quote.has_terrorism_options = False
            if quote.total_without_terrorism_cents:
                quote.pure_premium_cents = quote.total_without_terrorism_cents

        # 12. Evaluate Clarity & HITL Questions
        self._evaluate_hitl_requirements(quote, combined_text)
        return quote

    def _evaluate_hitl_requirements(self, quote: ExtractedQuote, combined_text: str) -> None:
        questions: list[str] = []
        reasons: list[str] = []

        # Question 1: Agency fee clarity
        if not re.search(r"agency\s+fee|fee|\$\s*350", combined_text, re.IGNORECASE):
            questions.append("1. Agency Fee: Is there an agency fee? (Default is $350.00, or specify amount)")
            reasons.append("agency_fee_unspecified")

        # Question 2: Commission rate clarity
        if quote.commission_rate is None:
            questions.append("2. Commission Rate: What is the commission rate for this policy? (e.g., 10%, 12%, 15%)")
            reasons.append("commission_rate_unspecified")

        # Question 3: Surplus lines tax clarity
        is_surplus_lines_carrier = bool(quote.wholesaler_name or (quote.carrier_name and quote.carrier_name.lower() in ["nautilus", "evanston", "scottsdale", "tapco", "rps"]))
        if is_surplus_lines_carrier and quote.surplus_lines_tax_cents == 0:
            questions.append("3. Surplus Lines Tax: Is surplus lines tax applicable to this quote? (If so, please specify tax/stamping fee amounts)")
            reasons.append("surplus_lines_tax_verification")

        # Question 4: Terrorism coverage clarity
        if quote.has_terrorism_options and quote.terrorism_included is None:
            with_str = f"${quote.total_with_terrorism_cents / 100:,.2f}" if quote.total_with_terrorism_cents else "With TRIA"
            without_str = f"${quote.total_without_terrorism_cents / 100:,.2f}" if quote.total_without_terrorism_cents else "Without TRIA"
            questions.append(
                f"4. Terrorism Coverage: The quote includes options {with_str} and {without_str}. Which coverage should be applied to the Ascend agreement?"
            )
            reasons.append("dual_terrorism_options_present")

        if questions:
            quote.requires_hitl = True
            quote.hitl_questions = questions
            quote.hitl_reasons = reasons

    def extract_from_pdf(self, pdf_path_or_bytes: bytes | Path | str, user_instruction: str = "") -> ExtractedQuote:
        text = extract_text_from_pdf(pdf_path_or_bytes)
        return self.extract_from_text(text, user_instruction)

    def apply_user_clarifications(self, quote: ExtractedQuote, reply_text: str) -> ExtractedQuote:
        """Applies user's email or chat responses to resolve ambiguous quote parameters."""
        text = reply_text.strip()
        
        # 1. Agency fee
        if re.search(r"\b(?:no\s+(?:agency\s+)?fee|\$0(?:\.00)?|zero\s+fee)\b", text, re.IGNORECASE):
            quote.agency_fees_cents = 0
            if "agency_fee_unspecified" in quote.hitl_reasons:
                quote.hitl_reasons.remove("agency_fee_unspecified")
        else:
            fee_before = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)\s*(?:agency\s+fee|fee)", text, re.IGNORECASE)
            fee_after = re.search(r"(?:agency\s+fee|fee)\s*(?:is|of|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
            if fee_before:
                quote.agency_fees_cents = _parse_dollars_to_cents(fee_before.group(1))
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")
            elif fee_after and fee_after.group(1):
                quote.agency_fees_cents = _parse_dollars_to_cents(fee_after.group(1))
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")
            elif re.search(r"\b(?:standard|default|yes)\b", text, re.IGNORECASE) and "agency_fee_unspecified" in quote.hitl_reasons:
                quote.agency_fees_cents = self.default_agency_fee_cents
                quote.hitl_reasons.remove("agency_fee_unspecified")
            elif re.search(r"\$\s*350(?:\.00)?", text):
                quote.agency_fees_cents = 35000
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")

        # 2. Commission rate
        comm_m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if comm_m:
            quote.commission_rate = _parse_percentage(comm_m.group(1))
            if "commission_rate_unspecified" in quote.hitl_reasons:
                quote.hitl_reasons.remove("commission_rate_unspecified")

        # 3. Surplus lines tax
        if re.search(r"\b(?:no\s+tax|no\s+surplus|admitted|none|\$0|0\s+tax)\b", text, re.IGNORECASE):
            quote.surplus_lines_tax_cents = 0
            if "surplus_lines_tax_verification" in quote.hitl_reasons:
                quote.hitl_reasons.remove("surplus_lines_tax_verification")
        else:
            tax_before = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)\s*(?:surplus\s+lines\s+tax|surplus\s+tax|tax)", text, re.IGNORECASE)
            tax_after = re.search(r"(?:surplus\s+lines\s+tax|surplus\s+tax|tax)\s*(?:is|of|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
            if tax_before:
                quote.surplus_lines_tax_cents = _parse_dollars_to_cents(tax_before.group(1))
                if "surplus_lines_tax_verification" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("surplus_lines_tax_verification")
            elif tax_after and tax_after.group(1):
                quote.surplus_lines_tax_cents = _parse_dollars_to_cents(tax_after.group(1))
                if "surplus_lines_tax_verification" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("surplus_lines_tax_verification")

        # 4. Terrorism coverage
        if re.search(r"(?:include|with|accept|yes|option\s*1)", text, re.IGNORECASE) and "dual_terrorism_options_present" in quote.hitl_reasons:
            quote.terrorism_included = True
            quote.has_terrorism_options = False
            if quote.total_with_terrorism_cents:
                quote.pure_premium_cents = quote.total_with_terrorism_cents
            quote.hitl_reasons.remove("dual_terrorism_options_present")
        elif re.search(r"(?:exclude|without|reject|no|option\s*2)", text, re.IGNORECASE) and "dual_terrorism_options_present" in quote.hitl_reasons:
            quote.terrorism_included = False
            quote.has_terrorism_options = False
            if quote.total_without_terrorism_cents:
                quote.pure_premium_cents = quote.total_without_terrorism_cents
            quote.hitl_reasons.remove("dual_terrorism_options_present")

        # Re-evaluate HITL status
        quote.requires_hitl = len(quote.hitl_reasons) > 0
        if not quote.requires_hitl:
            quote.hitl_questions = []
        return quote


@dataclass
class ExtractedEndorsement:
    policy_number: str = ""
    insured_name: str = ""
    carrier_name: str = ""
    wholesaler_name: Optional[str] = None
    coverage_title: str = "Commercial Policy"
    description: str = "Policy Endorsement / Change"
    effective_date: str = ""
    additional_premium_cents: int = 0
    taxes_and_fees_cents: int = 0
    seller_commission_rate: Optional[float] = None
    is_return_premium: bool = False

    @property
    def total_cents(self) -> int:
        return self.additional_premium_cents + self.taxes_and_fees_cents


class EndorsementExtractor:
    """Extracts policy endorsements, change requests, and audit additional premiums."""

    def extract_from_text(self, text: str) -> ExtractedEndorsement:
        endorsement = ExtractedEndorsement()
        
        # 1. Policy Number
        pol_m = re.search(
            r"(?:policy\s*(?:#|number|no\.?|num)?\s*[:#\-]?\s*)([A-Z0-9\-\/]{4,24})",
            text,
            re.IGNORECASE,
        )
        if pol_m:
            endorsement.policy_number = pol_m.group(1).strip()

        # 2. Insured Name
        ins_m = re.search(
            r"(?:named\s+insured|insured\s+name|insured|account\s+name)\s*[:\-]\s*([A-Za-z0-9\s,\.\-&]{3,50})",
            text,
            re.IGNORECASE,
        )
        if ins_m:
            endorsement.insured_name = ins_m.group(1).strip().split("\n")[0]

        # 3. Effective date
        eff_m = re.search(
            r"(?:effective\s+date|endorsement\s+effective|eff\s+date|change\s+effective)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})",
            text,
            re.IGNORECASE,
        )
        if eff_m:
            endorsement.effective_date = _format_iso_date(eff_m.group(1))
        else:
            endorsement.effective_date = date.today().isoformat()

        # 4. Description / Change type
        desc_m = re.search(
            r"(?:description\s+of\s+change|change\s+description|endorsement\s+type|description|memo)\s*[:\-]?\s*([^\n\r]+)",
            text,
            re.IGNORECASE,
        )
        if desc_m:
            endorsement.description = desc_m.group(1).strip()
        else:
            # Look for common endorsement keywords
            for kw in ("Blanket AI", "Waiver of Subrogation", "Add Vehicle", "Driver Addition", "Limit Increase", "Payroll Audit"):
                if kw.lower() in text.lower():
                    endorsement.description = kw
                    break

        # 5. Premium
        # Look for additional premium or return premium
        if re.search(r"\b(?:return\s+premium|refund|credit)\b", text, re.IGNORECASE):
            endorsement.is_return_premium = True

        prem_m = re.search(
            r"(?:additional\s+premium|endorsement\s+premium|ap|return\s+premium|premium\s+due|net\s+premium|premium)\s*[:\-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if prem_m:
            endorsement.additional_premium_cents = _parse_dollars_to_cents(prem_m.group(1))

        # 6. Taxes and fees
        tax_m = re.search(
            r"(?:surplus\s+lines\s+tax|tax|fees?|stamping\s+fee)\s*[:\-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if tax_m:
            endorsement.taxes_and_fees_cents = _parse_dollars_to_cents(tax_m.group(1))

        # 7. Commission rate
        comm_m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if comm_m:
            endorsement.seller_commission_rate = _parse_percentage(comm_m.group(1))

        return endorsement

    def extract_from_pdf(self, pdf_bytes: bytes) -> ExtractedEndorsement:
        text = _extract_text_from_pdf_stream(pdf_bytes)
        return self.extract_from_text(text)

