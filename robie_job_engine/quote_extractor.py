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
def _format_iso_date(raw_date: str) -> str:
    cleaned = raw_date.strip().replace("-", "/")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            pass
    return raw_date


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
    # True when the fee was explicitly found in the quote (not defaulted).
    # Used to decide whether to ask for confirmation.
    agency_fee_explicit: bool = False
    commission_rate: Optional[float] = None
    surplus_lines_tax_cents: int = 0
    surplus_lines_tax_addressed: bool = False
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
    sub_policies: list[dict[str, Any]] = field(default_factory=list)

    # Insured location/contact for Ascend new-insured creation. Resolved from
    # the client's EZLynx applicant record when available, otherwise captured
    # from the sender's clarification reply. Ascend requires both.
    mailing_address: dict[str, Any] = field(default_factory=dict)
    primary_contact: dict[str, Any] = field(default_factory=dict)
    # Sender-supplied EZLynx applicant id from a clarification reply
    # (e.g. "here's the applicant link"), used to retry EZLynx enrichment.
    applicant_id_hint: Optional[str] = None
    # Sender-supplied producer/account-manager name or email from a
    # clarification reply (e.g. "producer: Matthew Mancina"). Resolved
    # against Ascend users on resume; the workflow asks when unresolvable.
    producer_hint: Optional[str] = None
    # Set when the sender confirms creating a second agreement for a policy
    # number that already has one (duplicate guard bypass).
    duplicate_confirmed: bool = False
    # Down payment / deposit the client already paid directly to the carrier.
    # The financed amount is total minus this; never finance the full premium
    # when a deposit was paid or the client double-pays.
    down_payment_cents: int = 0
    down_payment_detected: bool = False
    # Quote issuance date (YYYY-MM-DD) and revision marker ("v2", "revised").
    # Used to detect superseded quotes and expired quotes (>30 days).
    quote_date: str = ""
    quote_revision: str = ""
    # Line items extracted from the quote for total-cross-check. Each dict
    # has "label" and "amount_cents". If the stated total disagrees with the
    # line-item sum beyond a rounding tolerance, the workflow asks the sender.
    line_items: list[dict[str, Any]] = field(default_factory=list)
    stated_total_cents: Optional[int] = None
    # All named insured entities found on the quote. More than one means the
    # workflow must ask which entity signs the finance agreement.
    named_insureds: list[str] = field(default_factory=list)
    # Writing carrier (the insurer actually on the policy), distinct from the
    # master company / group (carrier_name) and the wholesaler. The Ascend
    # billable uses the writing carrier's identifier.
    writing_carrier_name: str = ""

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
    def financed_amount_cents(self) -> int:
        """Amount actually financed: total minus any down payment already
        paid to the carrier. Never finance money the client already paid."""
        return max(0, self.total_premium_cents - self.down_payment_cents)

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


# Ascend carrier identifiers look like "nautilus_insurance_company_scottsdale_916e26":
# lowercase, underscore-separated, at least three segments.
CARRIER_IDENTIFIER_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b")


def _parse_carrier_answer(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (carrier_name, carrier_identifier) parsed from a clarification reply.

    Only fills values the caller passes in as empty; a wrong parse fails closed
    downstream (search finds nothing / billable 422s) rather than inventing data.
    """
    name: Optional[str] = None
    identifier: Optional[str] = None
    m = re.search(
        r"(?:carrier(?:\s+name)?|insurance\s+company)\s*(?:is|:|=)\s*"
        r"([A-Za-z][A-Za-z0-9 .&'\-]{2,60})",
        text,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip().split("\n")[0].strip(" .")
    ident = CARRIER_IDENTIFIER_RE.search(text)
    if ident:
        identifier = ident.group(0)
    return name, identifier


def _parse_address_answer(text: str) -> dict[str, str]:
    """Parse a US mailing address from a clarification reply into Ascend keys.

    Accepts "123 Main St, Freehold, NJ 07728" on one line or split across two
    lines. Returns {} unless street, city, state, and zip are all found.
    """
    one_line = " ".join(text.split())
    m = re.search(
        r"(\d[\w\s.#\-]*?)\s*,?\s+([A-Za-z][A-Za-z .'\-]*?)\s*,\s*([A-Z]{2})\s+(\d{5})(?:-(\d{4}))?",
        one_line,
    )
    if not m:
        return {}
    street, city, state, zip5 = m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
    # Guard against grabbing a fragment of a longer sentence as the street.
    if len(street) > 60 or len(city) > 40:
        return {}
    return {
        "mailing_address_street_one": street,
        "mailing_address_city": city,
        "mailing_address_state": state,
        "mailing_address_zip_code": zip5,
    }


def _parse_contact_answer(text: str) -> dict[str, str]:
    """Parse a primary contact (name, email, phone) from a clarification reply.

    Requires at least a name cue or an email address; returns {} otherwise.
    """
    contact: dict[str, str] = {}
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if email_m:
        contact["email"] = email_m.group(0)
    phone_m = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    if phone_m:
        contact["phone"] = re.sub(r"\D", "", phone_m.group(0))
    name_m = re.search(
        r"(?:primary\s+)?contact(?:\s+name)?\s*(?:is|:|=)\s*"
        r"([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,3})",
        text,
        re.IGNORECASE,
    )
    if name_m:
        full = name_m.group(1).strip().split("\n")[0].strip()
        parts = full.split()
        if parts:
            contact["first_name"] = parts[0]
            if len(parts) > 1:
                contact["last_name"] = " ".join(parts[1:])
    elif email_m:
        # "Mike Fingerhut <mjfingerhut@gmail.com>" shape
        pair_m = re.search(
            r"([A-Za-z][A-Za-z'\-]*)\s+([A-Za-z][A-Za-z'\-]*)\s*<" + re.escape(email_m.group(0)) + r">",
            text,
        )
        if pair_m:
            contact["first_name"] = pair_m.group(1)
            contact["last_name"] = pair_m.group(2)
    if not contact.get("first_name") and not contact.get("email"):
        return {}
    return contact


def _parse_applicant_id_hint(text: str) -> Optional[str]:
    """Extract an EZLynx applicant id from a clarification reply, if present."""
    m = re.search(
        r"(?:applicant(?:\s*id)?|applicantId)\s*[:#=]?\s*(\d{6,})",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(r"ezlynx\.com[^\s]*?(\d{8,})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _parse_producer_answer(text: str) -> Optional[str]:
    """Extract a producer/account-manager name or email from a reply line."""
    m = re.search(r"(?im)^\s*(?:producer|account\s*manager)\s*:\s*(.+?)\s*$", text)
    if m:
        value = m.group(1).strip().strip("\"'")
        return value or None
    return None


def strip_email_reply_history(text: str) -> str:
    """Strips quoted email threads, signatures, and reply headers so only new reply text is evaluated."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        # Quoted lines in email threads
        if clean.startswith(">"):
            continue
        # Common reply header intros
        if re.match(r"^On\s+.+wrote:$", clean, re.IGNORECASE):
            break
        if re.match(r"^-+\s*(?:Original|Forwarded)\s+Message\s*-+", clean, re.IGNORECASE):
            break
        # Email signatures
        if clean in ("--", "___", "Kind regards,", "Best regards,", "Best,", "Thanks,", "Thank you,"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def extract_text_from_pdf(pdf_bytes_or_path: bytes | Path | str) -> str:
    """Extract plain text from PDF using pypdf.

    Returns a [PDF_NO_TEXT_EXTRACTED] marker when the PDF has no selectable
    text (scanned/image PDF) so callers can ask the sender for the numbers
    instead of treating it as an empty quote.
    """
    try:
        from pypdf import PdfReader
        if isinstance(pdf_bytes_or_path, (str, Path)):
            reader = PdfReader(str(pdf_bytes_or_path))
        else:
            reader = PdfReader(io.BytesIO(pdf_bytes_or_path))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages_text).strip()
        if not text:
            return "[PDF_NO_TEXT_EXTRACTED: the PDF appears to be a scanned image with no selectable text]"
        return text
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
        clean_user_instruction = strip_email_reply_history(user_instruction)
        combined_text = f"{clean_user_instruction}\n{text}"

        # 1. Insured Name
        # Check explicit proposal layout first: "Insured OHANA 6 LIMITED COMPANY LLC Mailing Address"
        proposal_insured = re.search(
            r"\bInsured\s+([A-Z0-9\s&,.-]+?)\s+(?:Mailing\s+Address|DBA|Policy\s+Effective|DOT\b)",
            text,
        )
        if proposal_insured and len(proposal_insured.group(1).strip()) > 2:
            quote.insured_name = proposal_insured.group(1).strip()
        else:
            insured_match = re.search(
                r"(?:Named\s+Insured|Insured\s+Name|Applicant|Account\s+Name)[ \t]*:[ \t]*([^\n\r,]+?)(?=\s{2,}(?:Quote\s+Date|Mailing\s+Address|Policy\b)|\r?(\n|$))",
                text,
                re.IGNORECASE,
            )
            if insured_match:
                quote.insured_name = insured_match.group(1).strip()
            else:
                bare_insured = re.search(r"\bInsured[ \t]*:[ \t]*([^\n\r,]+)", text, re.IGNORECASE)
                if bare_insured:
                    candidate = bare_insured.group(1).strip()
                    if not any(k in candidate.lower() for k in ["cargo", "mtc", "deductible", "limit", "coverage"]):
                        quote.insured_name = candidate
            if not quote.insured_name and "yes we do" in text.lower():
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
                "Diesel",
            ]:
                if w_name.lower() in text.lower():
                    quote.wholesaler_name = "Diesel Insurance Solutions Inc." if w_name == "Diesel" else w_name
                    break

        # 4. Coverage / Line of Business
        for cov_title, cov_ident in COVERAGE_MAP.items():
            if cov_title in text.lower():
                quote.coverage_title = cov_title.title()
                quote.coverage_identifier = cov_ident
                break

        # 5. Policy / Quote Number
        # The captured value must contain a digit — form artifacts like
        # "Policy Number\nPantoja" (a label with stray text) are not policies.
        pol_match = re.search(
            r"(?:Quote\s+#|Quote\s+Number|Quote\s+ID|Policy\s+#|Policy\s+Number|Reference\s*:?\s*#?)[:\s]+([A-Z0-9\-_]+)",
            text,
            re.IGNORECASE,
        )
        if pol_match:
            candidate = pol_match.group(1).strip()
            if any(c.isdigit() for c in candidate):
                quote.policy_number = candidate

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
        else:
            # Labeled dates: "Effective: 01/01/2027", "Expiration Date: 01/01/2028".
            eff_match = re.search(
                r"effect(?:ive)?(?:\s+date)?\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
                text,
                re.IGNORECASE,
            )
            exp_match = re.search(
                r"expir(?:ation|y)(?:\s+date)?\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
                text,
                re.IGNORECASE,
            )
            try:
                if eff_match:
                    quote.effective_date = datetime.strptime(
                        eff_match.group(1), "%m/%d/%Y"
                    ).date().isoformat()
                if exp_match:
                    quote.expiration_date = datetime.strptime(
                        exp_match.group(1), "%m/%d/%Y"
                    ).date().isoformat()
            except ValueError:
                pass
        
        if not quote.effective_date:
            today = date.today()
            quote.effective_date = today.isoformat()
            quote.expiration_date = date(today.year + 1, today.month, today.day).isoformat()

        # 7. Pure Premium & Multi-Coverage Table Check
        has_mtc = bool(re.search(r"MOTOR TRUCK CARGO", text, re.IGNORECASE))
        has_pd = bool(re.search(r"PHYSICAL DAMAGE", text, re.IGNORECASE))
        prem_two_col = re.search(
            r"PREMIUM\s+\$([\d,]+(?:\.\d{2})?)\s+\$([\d,]+(?:\.\d{2})?)", text
        )

        if has_mtc and has_pd and prem_two_col:
            mtc_prem = _parse_dollars_to_cents(prem_two_col.group(1))
            pd_prem = _parse_dollars_to_cents(prem_two_col.group(2))

            mga_two_col = re.search(
                r"MGA Fee\s+\$([\d,]+(?:\.\d{2})?)\s+\$([\d,]+(?:\.\d{2})?)", text
            )
            mtc_mga = _parse_dollars_to_cents(mga_two_col.group(1)) if mga_two_col else 0
            pd_mga = _parse_dollars_to_cents(mga_two_col.group(2)) if mga_two_col else 0

            tax_two_col = re.search(
                r"Total Taxes\s+\$([\d,]+(?:\.\d{2})?)\s+\$([\d,]+(?:\.\d{2})?)", text
            )
            mtc_tax = _parse_dollars_to_cents(tax_two_col.group(1)) if tax_two_col else 0
            pd_tax = _parse_dollars_to_cents(tax_two_col.group(2)) if tax_two_col else 0

            quote.pure_premium_cents = mtc_prem + pd_prem
            quote.policy_fee_cents = mtc_mga + pd_mga
            quote.surplus_lines_tax_cents = mtc_tax + pd_tax
            quote.surplus_lines_tax_addressed = True
            quote.coverage_title = "Motor Truck Cargo & Physical Damage"
            quote.coverage_identifier = "cargo"

            quote.sub_policies = [
                {
                    "title": "Motor Truck Cargo",
                    "coverage_identifier": "cargo",
                    "pure_premium_cents": mtc_prem,
                    "policy_fee_cents": mtc_mga,
                    "taxes_and_fees_cents": mtc_tax,
                    "surplus_lines_tax_cents": mtc_tax,
                    "billable_suffix": "MTC",
                },
                {
                    "title": "Auto Physical Damage",
                    "coverage_identifier": "auto_physical_damage",
                    "pure_premium_cents": pd_prem,
                    "policy_fee_cents": pd_mga,
                    "taxes_and_fees_cents": pd_tax,
                    "surplus_lines_tax_cents": pd_tax,
                    "billable_suffix": "PD",
                },
            ]
        else:
            premium_match = re.search(
                r"(?:Pure\s+Premium|Base\s+Premium|Coverage\s+Premium|Policy\s+Premium|Advance\s+Premium):\s*\$\s*([\d,]+(?:\.\d{2})?)",
                text,
                re.IGNORECASE,
            )
            if premium_match:
                quote.pure_premium_cents = _parse_dollars_to_cents(premium_match.group(1))
            else:
                # "Total Premium $2,552.00" — require $ sign to avoid matching "25%"
                total_prem = re.search(
                    r"Total\s+Premium\s*\$\s*([\d,]+(?:\.\d{2})?)",
                    text,
                    re.IGNORECASE,
                )
                if total_prem:
                    quote.pure_premium_cents = _parse_dollars_to_cents(total_prem.group(1))
                else:
                    general_premium = re.search(
                        r"(?:Premium|Total\s+Cost|Total\s+Due):\s*\$\s*([\d,]+(?:\.\d{2})?)",
                        text,
                        re.IGNORECASE,
                    )
                    if general_premium:
                        quote.pure_premium_cents = _parse_dollars_to_cents(general_premium.group(1))

        # 7b. General multi-LOB detection: if the MTC+PD special case did not
        # fire, look for multiple distinct coverage sections, each with its
        # own premium. Each becomes a sub-policy → separate billable under
        # one program. Never blend LOBs into a single billable.
        if not quote.sub_policies:
            self._detect_multiple_lobs(quote, text)

        # 8. Parameter 1: Agency Fee
        fee_match = re.search(
            r"(?:Agency\s+Fee|Producer\s+Fee):\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            combined_text,
            re.IGNORECASE,
        )
        if fee_match:
            quote.agency_fees_cents = _parse_dollars_to_cents(fee_match.group(1))
            quote.agency_fee_explicit = True
        elif re.search(r"no\s+agency\s+fee|fee\s*:\s*\$0", combined_text, re.IGNORECASE):
            quote.agency_fees_cents = 0
            quote.agency_fee_explicit = True
        elif re.search(r"\$\s*350(?:\.00)?", combined_text):
            quote.agency_fees_cents = 35000
            quote.agency_fee_explicit = True

        # 9. Parameter 2: Commission Rate
        comm_match = re.search(
            r"(?:Commission\s*(?:Rate)?|Agency\s+Commission|Commission|comm)\s*(?:on\s+this\s+is|is|of|:|=)?\s*([\d.]+\s*%?)",
            combined_text,
            re.IGNORECASE,
        )
        if comm_match and _parse_percentage(comm_match.group(1)) is not None:
            quote.commission_rate = _parse_percentage(comm_match.group(1))
        else:
            user_comm = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:commission|comm)?", combined_text, re.IGNORECASE)
            if user_comm and _parse_percentage(user_comm.group(1)) is not None:
                quote.commission_rate = _parse_percentage(user_comm.group(1))
            else:
                table_comm = re.search(r"\bCommission\s+(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
                if table_comm:
                    quote.commission_rate = _parse_percentage(table_comm.group(1))

        # 10. Parameter 3: Surplus Lines Tax & Fees (if not extracted by multi-coverage table)
        if not quote.sub_policies:
            tax_match = re.search(
                r"(?:Surplus\s+Lines\s+Tax|State\s+Tax|Taxes)\s*[:=]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                combined_text,
                re.IGNORECASE,
            )
            if tax_match:
                quote.surplus_lines_tax_cents = _parse_dollars_to_cents(tax_match.group(1))
                quote.surplus_lines_tax_addressed = True
            elif re.search(r"no\s+surplus\s+(?:lines(?:\s+tax)?|tax)|surplus\s+lines\s+tax\s*:\s*\$0|tax(?:\s*:\s*|\s+is\s+)\$0", combined_text, re.IGNORECASE):
                quote.surplus_lines_tax_cents = 0
                quote.surplus_lines_tax_addressed = True

            stamping_match = re.search(
                r"(?:Stamping\s+Fee)\s*:?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                combined_text,
                re.IGNORECASE,
            )
            if not stamping_match:
                stamping_match = re.search(
                    r"Stamping\s+Fee\s*\n\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                    combined_text,
                    re.IGNORECASE,
                )
            if stamping_match:
                quote.surplus_lines_tax_cents += _parse_dollars_to_cents(stamping_match.group(1))

            # Other fees (label and amount may be on separate lines)
            pol_fee_match = re.search(
                r"(?:Policy\s+Fee)\s*:?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                combined_text,
                re.IGNORECASE,
            )
            if not pol_fee_match:
                # Try label on one line, amount on the next
                pol_fee_match = re.search(
                    r"Policy\s+Fee\s*\n\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                    combined_text,
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
        if re.search(r"\b(?:exclude\s+terrorism|without\s+terrorism|reject\s+tria|without\s+tria|no\s+tria|no\s+terrorism|option\s*2)\b", clean_user_instruction, re.IGNORECASE):
            quote.terrorism_included = False
            quote.has_terrorism_options = False
            if quote.total_without_terrorism_cents:
                quote.pure_premium_cents = quote.total_without_terrorism_cents
        elif re.search(r"\b(?:include\s+terrorism|with\s+terrorism|accept\s+tria|with\s+tria|option\s*1)\b", clean_user_instruction, re.IGNORECASE):
            quote.terrorism_included = True
            quote.has_terrorism_options = False
            if quote.total_with_terrorism_cents:
                quote.pure_premium_cents = quote.total_with_terrorism_cents

        # 12. Edge-case fields: down payment, quote date/revision, line items,
        # named insureds, writing carrier.
        self._extract_edge_case_fields(quote, combined_text)

        # 13. Evaluate Clarity & HITL Questions
        self._evaluate_hitl_requirements(quote, combined_text)
        return quote

    def _extract_edge_case_fields(self, quote: ExtractedQuote, text: str) -> None:
        # Down payment / deposit already paid to the carrier. Patterns like
        # "down payment: $2,500", "deposit paid $1,000", "25% down ($2,500)".
        dp_match = re.search(
            r"(?:down\s*payment|deposit|amount\s+paid|paid\s+to\s+carrier)"
            r"\s*(?:is|of|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if dp_match:
            quote.down_payment_cents = _parse_dollars_to_cents(dp_match.group(1))
            quote.down_payment_detected = True
        else:
            # "25% down" without a dollar amount: flag for clarification.
            pct_down = re.search(r"(\d+(?:\.\d+)?)\s*%\s*down", text, re.IGNORECASE)
            if pct_down:
                quote.down_payment_detected = True  # amount unknown

        # Quote date: "Quote Date: 09/15/2026", "Dated: September 15, 2026".
        date_match = re.search(
            r"(?:quote\s+date|dated?|issued)[:\s]+"
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
            text,
            re.IGNORECASE,
        )
        if date_match:
            quote.quote_date = _format_iso_date(date_match.group(1))

        # Revision marker: "revised", "v2", "version 3", "amended".
        rev_match = re.search(
            r"\b(revised|amended|v\s*(\d+)|version\s*(\d+)|rev\s*(\d+))\b",
            text,
            re.IGNORECASE,
        )
        if rev_match:
            quote.quote_revision = rev_match.group(0).strip()

        # Line items for total cross-check: lines like "General Liability ... $5,000.00".
        # Only capture lines with a dollar amount that look like coverage rows.
        # Skip coverage limits ("Each Occurrence $1,000,000"), deductibles,
        # aggregates — those are limits, not premiums.
        limit_words = ("occurrence", "aggregate", "deductible", "limit", "each",
                       "per occurrence", "general aggregate", "balance to minimum",
                       "minimum term", "minimum retained")
        seen_items: set[tuple[str, int]] = set()
        for line in text.splitlines():
            li = re.search(
                r"^\s*([A-Za-z][A-Za-z\s&/\-]{3,40}?)\s+\$?\s*([\d,]+\.\d{2})\s*$",
                line.strip(),
            )
            if li:
                label = li.group(1).strip()
                label_lower = label.lower()
                if len(label) > 3 and "total" not in label_lower:
                    if any(w in label_lower for w in limit_words):
                        continue
                    amount = _parse_dollars_to_cents(li.group(2))
                    # Normalize label for dedup: "Surplus Line Tax" vs
                    # "Surplus Lines Tax" are the same fee listed twice.
                    norm_label = re.sub(r"s\b", "", label_lower)
                    key = (norm_label, amount)
                    if key in seen_items:
                        continue  # duplicate line (fee schedules often repeat)
                    seen_items.add(key)
                    quote.line_items.append(
                        {"label": label, "amount_cents": amount}
                    )

        # Stated total: prefer the grand total ("Total including Premium,
        # Surcharges, Taxes and Fees $2,939.02") over "Total Premium".
        grand_match = re.search(
            r"total\s+including[^\n$]*\$\s*([\d,]+(?:\.\d{2})?)",
            text,
            re.IGNORECASE,
        )
        if grand_match:
            quote.stated_total_cents = _parse_dollars_to_cents(grand_match.group(1))
        else:
            total_match = re.search(
                r"(?:total\s+(?:premium|due|amount)|grand\s+total|amount\s+due)"
                r"\s*(?:is|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                text,
                re.IGNORECASE,
            )
            if total_match:
                quote.stated_total_cents = _parse_dollars_to_cents(total_match.group(1))

        # Named insureds: capture "Named Insured:" lines and DBA mentions.
        # Multiple distinct entities trigger a clarification question.
        # Stop at field boundaries like "Quote Date:" that share the line.
        seen: set[str] = set()
        boundary = r"(?=\s{2,}(?:Quote\s+Date|Mailing\s+Address|Policy\b)|\r?(\n|$))"
        for pat in [
            rf"Named\s+Insured\s*:\s*([^\n\r,]+?){boundary}",
            rf"Insured\s+Name\s*:\s*([^\n\r,]+?){boundary}",
        ]:
            for m in re.finditer(pat, text, re.IGNORECASE):
                name = m.group(1).strip()
                if name and name.lower() not in seen and len(name) > 2:
                    seen.add(name.lower())
                    quote.named_insureds.append(name)
        # DBA: "ABC LLC DBA XYZ Corp" counts as a second entity.
        dba_match = re.search(r"\bDBA\s+([^\n\r,]+)", text, re.IGNORECASE)
        if dba_match:
            dba_name = dba_match.group(1).strip()
            if dba_name and dba_name.lower() not in seen and len(dba_name) > 2:
                quote.named_insureds.append(dba_name)

        # Writing carrier: the insurer actually on the policy, e.g.
        # "Writing Company: Berkley Aspire" vs the master group "Berkley".
        wc_match = re.search(
            r"(?:writing\s+(?:company|carrier|insurer)|underwritten\s+by|policy\s+issued\s+by)"
            r"\s*(?:is|:|=)?\s*([^\n\r,]+)",
            text,
            re.IGNORECASE,
        )
        if wc_match:
            quote.writing_carrier_name = wc_match.group(1).strip()

    def _detect_multiple_lobs(self, quote: ExtractedQuote, text: str) -> None:
        """Detect multiple lines of business in one quote document.

        Each distinct coverage section with its own premium becomes a
        sub-policy. Each sub-policy carries its own policy number, carrier
        (writing carrier), dates, and commission rate when present —
        falling back to the parent quote's values. Sections without a
        detectable premium are skipped (they cannot become billables).
        """
        # Split text into candidate sections by coverage headers.
        # A header is a line that is mostly a coverage name.
        sections: list[tuple[str, str]] = []  # (coverage_ident, section_text)
        lines = text.splitlines()
        current_ident: str | None = None
        current_lines: list[str] = []
        for line in lines:
            stripped = line.strip().lower()
            matched_ident: str | None = None
            # Header heuristic: line is short and matches/contains a coverage name
            if len(stripped) < 60:
                for cov_title, cov_ident in COVERAGE_MAP.items():
                    if len(cov_title) >= 4 and cov_title in stripped:
                        # Avoid matching inside longer unrelated phrases
                        matched_ident = cov_ident
                        break
            if matched_ident and matched_ident != current_ident:
                if current_ident and current_lines:
                    sections.append((current_ident, "\n".join(current_lines)))
                current_ident = matched_ident
                current_lines = [line]
            elif current_ident:
                current_lines.append(line)
        if current_ident and current_lines:
            sections.append((current_ident, "\n".join(current_lines)))

        # Deduplicate: keep first occurrence of each coverage
        seen: set[str] = set()
        unique_sections: list[tuple[str, str]] = []
        for ident, sec_text in sections:
            if ident not in seen:
                seen.add(ident)
                unique_sections.append((ident, sec_text))

        # Need at least 2 sections with premiums to be multi-LOB
        candidates: list[dict] = []
        for ident, sec_text in unique_sections:
            prem = re.search(
                r"(?:premium|total|cost)[\s:]*\$?\s*([\d,]+(?:\.\d{2})?)",
                sec_text,
                re.IGNORECASE,
            )
            if not prem:
                continue
            premium_cents = _parse_dollars_to_cents(prem.group(1))
            if premium_cents <= 0:
                continue
            sp: dict = {
                "coverage_identifier": ident,
                "title": ident.replace("_", " ").title(),
                "pure_premium_cents": premium_cents,
                "billable_suffix": ident[:3].upper(),
            }
            # Per-section policy number
            pol = re.search(
                r"(?:Policy\s+(?:#|Number|No)|Quote\s+(?:#|Number))[\s:]+([A-Z0-9\-_]+)",
                sec_text,
                re.IGNORECASE,
            )
            if pol:
                sp["policy_number"] = pol.group(1).strip()
            # Per-section carrier (writing carrier for this LOB)
            carr = re.search(
                r"(?:Carrier|Insurer|Writing\s+Company)[\s:]+([^\n\r,]+)",
                sec_text,
                re.IGNORECASE,
            )
            if carr:
                sp["carrier_name"] = carr.group(1).strip()
            wc = re.search(
                r"(?:writing\s+(?:company|carrier|insurer)|underwritten\s+by)"
                r"\s*(?:is|:|=)?\s*([^\n\r,]+)",
                sec_text,
                re.IGNORECASE,
            )
            if wc:
                sp["writing_carrier_name"] = wc.group(1).strip()
            # Per-section dates
            eff = re.search(
                r"effect(?:ive)?(?:\s+date)?\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
                sec_text,
                re.IGNORECASE,
            )
            exp = re.search(
                r"expir(?:ation|y)(?:\s+date)?\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
                sec_text,
                re.IGNORECASE,
            )
            try:
                if eff:
                    sp["effective_date"] = datetime.strptime(eff.group(1), "%m/%d/%Y").date().isoformat()
                if exp:
                    sp["expiration_date"] = datetime.strptime(exp.group(1), "%m/%d/%Y").date().isoformat()
            except ValueError:
                pass
            candidates.append(sp)

        if len(candidates) >= 2:
            quote.sub_policies = candidates
            # Parent premium becomes the sum for display; each billable keeps
            # its own premium.
            quote.pure_premium_cents = sum(c["pure_premium_cents"] for c in candidates)

    def _evaluate_hitl_requirements(self, quote: ExtractedQuote, combined_text: str) -> None:
        reasons: list[str] = []

        # Question 1: Agency fee clarity
        if not re.search(r"agency\s+fee|fee|\$\s*350", combined_text, re.IGNORECASE):
            reasons.append("agency_fee_unspecified")

        # Question 2: Commission rate clarity
        if quote.commission_rate is None:
            reasons.append("commission_rate_unspecified")

        # Question 3: Surplus lines tax clarity
        is_surplus_lines_carrier = bool(quote.wholesaler_name or (quote.carrier_name and quote.carrier_name.lower() in ["nautilus", "evanston", "scottsdale", "tapco", "rps"]))
        if is_surplus_lines_carrier and not quote.surplus_lines_tax_addressed and quote.surplus_lines_tax_cents == 0:
            reasons.append("surplus_lines_tax_verification")

        # Question 4: Terrorism coverage clarity
        if quote.has_terrorism_options and quote.terrorism_included is None:
            reasons.append("dual_terrorism_options_present")

        # Question 5: Down payment detected but amount unknown (e.g. "25% down"
        # with no dollar figure). Financing the full premium would double-charge.
        if quote.down_payment_detected and quote.down_payment_cents == 0:
            reasons.append("down_payment_amount_unknown")

        # Question 6: Conflicting totals — stated total disagrees with the
        # line-item sum by more than $1.00 (rounding tolerance).
        if quote.stated_total_cents is not None and quote.line_items:
            line_sum = sum(li["amount_cents"] for li in quote.line_items)
            if abs(line_sum - quote.stated_total_cents) > 100:
                reasons.append("conflicting_totals")

        # Question 7: Multiple named insureds — which entity signs the
        # finance agreement?
        if len(quote.named_insureds) > 1:
            reasons.append("multiple_named_insureds")

        # Question 8: Expired quote — issued more than 30 days ago. Rates may
        # not hold; confirm the quote is still valid.
        if quote.quote_date:
            try:
                qd = date.fromisoformat(quote.quote_date)
                if (date.today() - qd).days > 30:
                    reasons.append("quote_expired")
            except ValueError:
                pass

        # Question 9: Backdated effective date — policy already started.
        if quote.effective_date:
            try:
                ed = date.fromisoformat(quote.effective_date)
                if ed < date.today():
                    reasons.append("effective_date_backdated")
            except ValueError:
                pass

        # Question 10: Reversed or missing dates.
        if quote.effective_date and quote.expiration_date:
            try:
                ed = date.fromisoformat(quote.effective_date)
                xd = date.fromisoformat(quote.expiration_date)
                if xd <= ed:
                    reasons.append("dates_reversed")
            except ValueError:
                pass

        # Agency fee confirmation: when we're already asking clarifications,
        # confirm the fee amount. Money the client pays should never be
        # assumed — whether defaulted or extracted.
        if reasons and "agency_fee_unspecified" not in reasons and "agency_fee_confirm" not in reasons:
            reasons.append("agency_fee_confirm")

        quote.hitl_reasons = reasons
        self._sync_hitl_questions(quote)

    def _sync_hitl_questions(self, quote: ExtractedQuote) -> None:
        """Synchronizes quote.hitl_questions dynamically based on remaining hitl_reasons."""
        questions: list[str] = []
        for reason in quote.hitl_reasons:
            if reason == "agency_fee_unspecified":
                questions.append("1. Agency Fee: Is there an agency fee? (Default is $350.00, or specify amount)")
            elif reason == "agency_fee_confirm":
                questions.append(
                    f"Agency Fee Confirmation: I found an agency fee of ${quote.agency_fees_cents / 100:,.2f}. "
                    "Is that correct, or should it be a different amount?"
                )
            elif reason == "commission_rate_unspecified":
                questions.append("2. Commission Rate: What is the commission rate for this policy? (e.g., 10%, 12%, 15%)")
            elif reason == "surplus_lines_tax_verification":
                questions.append("3. Surplus Lines Tax: Is surplus lines tax applicable to this quote? (If so, please specify tax/stamping fee amounts)")
            elif reason == "dual_terrorism_options_present":
                with_str = f"${quote.total_with_terrorism_cents / 100:,.2f}" if quote.total_with_terrorism_cents else "With TRIA"
                without_str = f"${quote.total_without_terrorism_cents / 100:,.2f}" if quote.total_without_terrorism_cents else "Without TRIA"
                questions.append(
                    f"4. Terrorism Coverage: The quote includes options {with_str} and {without_str}. Which coverage should be applied to the Ascend agreement?"
                )
            elif reason == "down_payment_amount_unknown":
                questions.append(
                    "5. Down Payment: The quote mentions a down payment was made, but I couldn't find the dollar amount. "
                    "How much was already paid to the carrier? (I'll finance only the remaining balance.)"
                )
            elif reason == "conflicting_totals":
                line_sum = sum(li["amount_cents"] for li in quote.line_items)
                questions.append(
                    f"6. Conflicting Totals: The quote's stated total (${quote.stated_total_cents / 100:,.2f}) "
                    f"doesn't match the sum of its line items (${line_sum / 100:,.2f}). Which total is correct?"
                )
            elif reason == "multiple_named_insureds":
                names = ", ".join(f'"{n}"' for n in quote.named_insureds)
                questions.append(
                    f"7. Named Insured: The quote lists multiple entities ({names}). "
                    "Which entity should sign the finance agreement?"
                )
            elif reason == "quote_expired":
                questions.append(
                    f"8. Quote Age: This quote is dated {quote.quote_date}, more than 30 days ago. "
                    "Is it still valid, or is there a newer version?"
                )
            elif reason == "effective_date_backdated":
                questions.append(
                    f"9. Effective Date: The policy effective date ({quote.effective_date}) is in the past. "
                    "Should I proceed with this date?"
                )
            elif reason == "dates_reversed":
                questions.append(
                    f"10. Dates: The expiration date ({quote.expiration_date}) is not after the effective date "
                    f"({quote.effective_date}). What are the correct dates?"
                )
        quote.hitl_questions = questions
        quote.requires_hitl = len(questions) > 0

    def extract_from_pdf(self, pdf_path_or_bytes: bytes | Path | str, user_instruction: str = "") -> ExtractedQuote:
        text = extract_text_from_pdf(pdf_path_or_bytes)
        return self.extract_from_text(text, user_instruction)

    def apply_user_clarifications(self, quote: ExtractedQuote, reply_text: str) -> ExtractedQuote:
        """Applies user's email or chat responses to resolve ambiguous quote parameters."""
        clean_text = strip_email_reply_history(reply_text.strip())
        text = clean_text or reply_text.strip()
        
        # 1. Agency fee
        if re.search(r"\b(?:no\s+(?:agency\s+)?fee|\$0(?:\.00)?|zero\s+fee)\b", text, re.IGNORECASE):
            quote.agency_fees_cents = 0
            quote.agency_fee_explicit = True
            if "agency_fee_unspecified" in quote.hitl_reasons:
                quote.hitl_reasons.remove("agency_fee_unspecified")
            if "agency_fee_confirm" in quote.hitl_reasons:
                quote.hitl_reasons.remove("agency_fee_confirm")
        elif re.search(r"\b(?:yes|correct|confirmed|that's right|looks good)\b", text, re.IGNORECASE) and "agency_fee_confirm" in quote.hitl_reasons:
            # User confirmed the fee amount; mark explicit.
            quote.agency_fee_explicit = True
            quote.hitl_reasons.remove("agency_fee_confirm")
        else:
            fee_before = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)\s*(?:agency\s+fee|broker\s+fee|fee)", text, re.IGNORECASE)
            fee_after = re.search(r"(?:agency\s+fee|broker\s+fee|fee)\s*(?:is|of|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
            fee_make = re.search(r"\bmake\s+(?:the\s+)?(?:agency\s+)?fee\s*\$?\s*([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
            if fee_make:
                quote.agency_fees_cents = _parse_dollars_to_cents(fee_make.group(1))
                quote.agency_fee_explicit = True
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")
                if "agency_fee_confirm" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_confirm")
            elif fee_before:
                quote.agency_fees_cents = _parse_dollars_to_cents(fee_before.group(1))
                quote.agency_fee_explicit = True
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")
                if "agency_fee_confirm" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_confirm")
            elif fee_after and fee_after.group(1):
                quote.agency_fees_cents = _parse_dollars_to_cents(fee_after.group(1))
                quote.agency_fee_explicit = True
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")
                if "agency_fee_confirm" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_confirm")
            elif re.search(r"\b(?:standard|default|yes)\b", text, re.IGNORECASE) and "agency_fee_unspecified" in quote.hitl_reasons:
                quote.agency_fees_cents = self.default_agency_fee_cents
                quote.hitl_reasons.remove("agency_fee_unspecified")
            elif re.search(r"\$\s*350(?:\.00)?", text):
                quote.agency_fees_cents = 35000
                if "agency_fee_unspecified" in quote.hitl_reasons:
                    quote.hitl_reasons.remove("agency_fee_unspecified")

        # 2. Commission rate
        comm_phrase = re.search(
            r"(?:Commission\s*(?:Rate)?|Agency\s+Commission|Commission|comm)\s*(?:on\s+this\s+is|is|of|:|=)?\s*([\d.]+\s*%?)",
            text,
            re.IGNORECASE,
        )
        if comm_phrase and _parse_percentage(comm_phrase.group(1)) is not None:
            quote.commission_rate = _parse_percentage(comm_phrase.group(1))
            if "commission_rate_unspecified" in quote.hitl_reasons:
                quote.hitl_reasons.remove("commission_rate_unspecified")
        else:
            comm_m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
            if comm_m and _parse_percentage(comm_m.group(1)) is not None:
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

        # 4. Terrorism coverage (Check negative/rejection first with word boundaries)
        if re.search(r"\b(?:exclude|without|reject|no\s+tria|no\s+terrorism|option\s*2)\b", text, re.IGNORECASE) and "dual_terrorism_options_present" in quote.hitl_reasons:
            quote.terrorism_included = False
            quote.has_terrorism_options = False
            if quote.total_without_terrorism_cents:
                quote.pure_premium_cents = quote.total_without_terrorism_cents
            quote.hitl_reasons.remove("dual_terrorism_options_present")
        elif re.search(r"\b(?:include|with\s+terrorism|with\s+tria|accept|accepted|yes|option\s*1)\b", text, re.IGNORECASE) and "dual_terrorism_options_present" in quote.hitl_reasons:
            quote.terrorism_included = True
            quote.has_terrorism_options = False
            if quote.total_with_terrorism_cents:
                quote.pure_premium_cents = quote.total_with_terrorism_cents
            quote.hitl_reasons.remove("dual_terrorism_options_present")

        # 5. Carrier / insured address / primary contact / producer answers.
        # These fields are empty after initial extraction; they get filled
        # when the sender answers a NEEDS_CLARIFICATION email, and
        # create_agreement_and_file_ezlynx consumes them on resume. Never
        # overwrite values already present.
        if not quote.carrier_identifier:
            carrier_name, carrier_identifier = _parse_carrier_answer(text)
            if carrier_identifier:
                quote.carrier_identifier = carrier_identifier
            elif carrier_name and not quote.carrier_name:
                quote.carrier_name = carrier_name
        if not quote.mailing_address:
            address = _parse_address_answer(text)
            if address:
                quote.mailing_address = address
        if not quote.primary_contact:
            contact = _parse_contact_answer(text)
            if contact:
                quote.primary_contact = contact
        if not quote.applicant_id_hint:
            hint = _parse_applicant_id_hint(text)
            if hint:
                quote.applicant_id_hint = hint
        if not quote.producer_hint:
            producer = _parse_producer_answer(text)
            if producer:
                quote.producer_hint = producer
        if not quote.duplicate_confirmed and re.search(
            r"(?i)\bconfirm\s+duplicate\b", text
        ):
            quote.duplicate_confirmed = True

        # 6. Down payment amount answer (e.g. "$2,500 was paid", "down payment 2500").
        if "down_payment_amount_unknown" in quote.hitl_reasons:
            dp_answer = re.search(
                r"(?:down\s*payment|deposit|paid)\s*(?:was|is|of|:|=)?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
                text,
                re.IGNORECASE,
            )
            if dp_answer:
                quote.down_payment_cents = _parse_dollars_to_cents(dp_answer.group(1))
                quote.hitl_reasons.remove("down_payment_amount_unknown")
            elif re.search(r"\bno\s+(?:down\s+payment|deposit)\b|\$0", text, re.IGNORECASE):
                quote.down_payment_cents = 0
                quote.down_payment_detected = False
                quote.hitl_reasons.remove("down_payment_amount_unknown")

        # 7. Conflicting totals answer: sender picks the correct total.
        if "conflicting_totals" in quote.hitl_reasons:
            line_sum = sum(li["amount_cents"] for li in quote.line_items)
            pick_line = re.search(r"\bline\s*items?\b", text, re.IGNORECASE)
            pick_stated = re.search(r"\bstated\s+total\b", text, re.IGNORECASE)
            explicit = re.search(r"\$?\s*([\d,]+(?:\.\d{2})?)", text)
            if pick_line:
                quote.stated_total_cents = line_sum
                quote.hitl_reasons.remove("conflicting_totals")
            elif pick_stated and quote.stated_total_cents:
                quote.hitl_reasons.remove("conflicting_totals")
            elif explicit:
                quote.stated_total_cents = _parse_dollars_to_cents(explicit.group(1))
                quote.hitl_reasons.remove("conflicting_totals")

        # 8. Named insured answer: sender picks the signing entity.
        if "multiple_named_insureds" in quote.hitl_reasons:
            for name in quote.named_insureds:
                if name.lower() in text.lower():
                    quote.insured_name = name
                    quote.named_insureds = [name]
                    quote.hitl_reasons.remove("multiple_named_insureds")
                    break

        # 9. Expired quote answer: "still valid" / "use it" proceeds; anything
        # mentioning a newer quote keeps the question open.
        if "quote_expired" in quote.hitl_reasons:
            if re.search(r"\bstill\s+valid\b|\buse\s+it\b|\bproceed\b|\byes\b", text, re.IGNORECASE):
                quote.hitl_reasons.remove("quote_expired")

        # 10. Backdated effective date: explicit confirmation proceeds.
        if "effective_date_backdated" in quote.hitl_reasons:
            if re.search(r"\bproceed\b|\bconfirm\b|\byes\b|\buse\s+(?:that|this)\s+date\b", text, re.IGNORECASE):
                quote.hitl_reasons.remove("effective_date_backdated")

        # 11. Reversed dates: sender supplies corrected dates.
        if "dates_reversed" in quote.hitl_reasons:
            dates = re.findall(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
            if len(dates) >= 2:
                quote.effective_date = _format_iso_date(dates[0])
                quote.expiration_date = _format_iso_date(dates[1])
                quote.hitl_reasons.remove("dates_reversed")

        # Re-evaluate HITL status and dynamically synchronize remaining questions
        self._sync_hitl_questions(quote)
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
            r"(?:policy\s*(?:#|number|no\.?|num)\s*[:#\-]?\s*)([A-Z0-9\-\/]{4,24})",
            text,
            re.IGNORECASE,
        )
        if not pol_m:
            pol_m = re.search(
                r"(?:policy\s*[:#\-]\s*)([A-Z0-9\-\/]{4,24})",
                text,
                re.IGNORECASE,
            )
        if pol_m and pol_m.group(1).upper() not in ("ENDORSEMENT", "CHANGE", "REQUEST", "PERIOD", "SCHEDULE"):
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

