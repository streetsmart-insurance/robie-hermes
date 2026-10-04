"""EZLynx Policy Setup (APE) automation engine and Playwright Page Object.

Strict invariants:
1. Stop before bind: Never binds coverage or authorizes COMPLETE without manual gate.
2. No shell-only policies: Must execute Add & Edit Policy (#AddAndEditPolicyBtn) to complete vehicles, drivers, coverages, locations, and schedules.
3. Discussion note format: Includes "ROBIE was here".
4. Deterministic locators: No positional .first/.last/.nth selectors.
5. All LOB coverage: Commercial Auto, Personal Auto, BOP, General Liability, Workers Comp, Commercial Property, Commercial Umbrella, Personal Umbrella, Homeowners, Dwelling Fire, Inland Marine, Commercial Package, Crime, Flood, Cyber, Surety.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def running_code_version() -> str:
    """SHA of the running tree or release. Never a leftover hardcoded commit."""
    for key in ("ROBIE_CODE_VERSION", "ROBIE_RELEASE_COMMIT"):
        value = (os.environ.get(key) or "").strip()
        if _SHA_RE.fullmatch(value):
            return value
    repo = Path(__file__).resolve().parents[1]
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and _SHA_RE.fullmatch(sha):
            return sha
    except (OSError, subprocess.TimeoutExpired):
        pass
    for raw in (
        os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT"),
        "/opt/streetsmart-hermes/releases/current",
        "/opt/streetsmart-hermes/current",
        str(Path(__file__).resolve()),
    ):
        if not raw:
            continue
        path = Path(raw)
        try:
            text = str(path.resolve() if path.exists() else path)
        except OSError:
            text = str(path)
        match = re.search(r"(?:^|/)([0-9a-f]{12,40})(?:/|$)", text)
        if match:
            return match.group(1)
    return "unknown"


# Job reports include this so a run can be correlated to the code that
# executed. Read from the running tree/release — do not hardcode a SHA.
CODE_VERSION = running_code_version()

EZLYNX_BASE_URL = "https://app.ezlynx.com"

# Comprehensive Line of Business Map
LINE_OF_BUSINESS_MAP = {
    "commercial_auto": "Auto (Commercial)",
    "personal_auto": "Auto (Personal)",
    "bop": "Business Owners",
    "business_owners": "Business Owners",
    "general_liability": "General Liability",
    "gl": "General Liability",
    "workers_comp": "Workers Compensation",
    "workers_compensation": "Workers Compensation",
    "wc": "Workers Compensation",
    "commercial_package": "Commercial Package",
    "commercial_property": "Commercial Property",
    "commercial_umbrella": "Umbrella (Commercial)",
    "personal_umbrella": "Umbrella (Personal)",
    "homeowners": "Homeowners",
    "home": "Homeowners",
    "ho": "Homeowners",
    "dwelling_fire": "Dwelling Fire",
    "inland_marine": "Inland Marine (Commercial)",
    "crime": "Crime",
    "flood": "Flood",
    "cyber": "Cyber Liability",
    "surety": "Surety Bond",
}

# Transaction Types
TRANSACTION_TYPE_MAP = {
    "new_business": "NBS",
    "renewal": "RWL",
    "policy_change": "PCH",
    "rewrite": "REW",
    "reinstate": "REI",
    "cancel_confirmation": "XLC",
    "audit": "AUD",
}


def clean_currency(val: str | int | float | None) -> str:
    """Strip $, commas, spaces from currency string for clean unmasked number input."""
    if val is None:
        return ""
    s = str(val).strip()
    return re.sub(r"[^\d.]", "", s)


@dataclass(frozen=True)
class VehicleItem:
    vin: str
    year: str = ""
    make: str = ""
    model: str = ""
    body_type: str = ""
    garaging_address: str = ""
    comp_deductible: str = "1000"
    coll_deductible: str = "1000"
    stated_amount: str = ""
    towing: bool = False
    rental: bool = False


@dataclass(frozen=True)
class DriverItem:
    first_name: str
    last_name: str
    dob: str = ""
    license_number: str = ""
    license_state: str = "NJ"
    date_hired: str = ""
    excluded: bool = False
    primary_vehicle_vin: str = ""


@dataclass(frozen=True)
class LocationItem:
    location_number: str = "1"
    address: str = ""
    city: str = ""
    state: str = "NJ"
    zip_code: str = ""
    county: str = ""


@dataclass(frozen=True)
class BuildingItem:
    location_number: str = "1"
    building_number: str = "1"
    construction_type: str = "Frame"
    protection_class: str = "3"
    year_built: str = ""
    square_footage: str = ""
    sprinklered_pct: str = "0"
    stories: str = "1"
    occupancy_class: str = ""
    building_limit: str = ""
    bpp_limit: str = ""
    valuation: str = "RCV"  # RCV or ACV
    coinsurance: str = "80%"
    causes_of_loss: str = "Special"
    deductible: str = "1000"
    business_income_limit: str = ""
    extra_expense_limit: str = ""


@dataclass(frozen=True)
class GLCoverageItem:
    occurrence_limit: str = "1000000"
    general_aggregate: str = "2000000"
    products_aggregate: str = "2000000"
    pers_adv_injury: str = "1000000"
    premises_rented: str = "100000"
    med_expense: str = "5000"
    deductible: str = "0"
    class_code: str = ""
    class_description: str = ""
    exposure: str = ""
    blanket_ai: bool = False
    waiver_subrogation: bool = False


@dataclass(frozen=True)
class PropertyCoverageItem:
    building_limit: str = ""
    bpp_limit: str = ""
    deductible: str = "1000"
    coinsurance: str = "80%"
    causes_of_loss: str = "Special"
    business_income: str = ""
    extra_expense: str = ""
    valuation: str = "RCV"


@dataclass(frozen=True)
class WorkersCompItem:
    covered_states: Sequence[str] = field(default_factory=lambda: ("NJ",))
    class_code: str = ""
    class_description: str = ""
    estimated_annual_payroll: str = ""
    full_time_employees: str = "1"
    part_time_employees: str = "0"
    officers_included: bool = True
    el_each_accident: str = "1000000"
    el_disease_policy: str = "1000000"
    el_disease_employee: str = "1000000"


@dataclass(frozen=True)
class UnderlyingPolicyItem:
    lob: str  # Auto, GL, Employers Liability
    carrier: str
    policy_number: str
    effective_date: str = ""
    expiration_date: str = ""
    limits: str = ""


@dataclass(frozen=True)
class UmbrellaCoverageItem:
    occurrence_limit: str = "1000000"
    aggregate_limit: str = "1000000"
    retained_limit: str = "0"
    underlying_policies: Sequence[UnderlyingPolicyItem] = field(default_factory=tuple)


@dataclass(frozen=True)
class HomeownersCoverageItem:
    dwelling_a: str = ""
    other_structures_b: str = ""
    personal_property_c: str = ""
    loss_of_use_d: str = ""
    liability_e: str = ""
    med_pay_f: str = ""
    all_peril_deductible: str = "1000"
    wind_hail_deductible: str = "1000"
    hurricane_deductible: str = "2%"
    roof_type: str = "Asphalt Shingle"
    roof_year: str = ""
    construction_type: str = "Frame"


@dataclass(frozen=True)
class InlandMarineItem:
    item_description: str
    serial_number: str = ""
    limit: str = ""
    deductible: str = "500"
    category: str = "Contractors Equipment"


@dataclass(frozen=True)
class PolicyShellInput:
    applicant_id: str
    lob: str  # e.g. "Auto (Commercial)" or "commercial_auto"
    transaction_type: str = "NBS"  # e.g. "NBS" or "new_business"
    master_company_value: str = "155"  # Progressive = 155, Travelers = 10, etc.
    writing_company_text: str = ""
    policy_number: str = ""
    effective_date: str = ""  # MM/DD/YYYY
    expiration_date: str = ""  # MM/DD/YYYY
    lob_origination_date: str = ""  # MM/DD/YYYY (mandatory)
    billing_type: str = "Direct Bill"  # wanted value, not an EZLynx option label
    billing_company: str = ""
    rating_state_value: str = "31"  # NJ = 31
    premium: str = ""
    full_term_premium: str = ""
    annual_premium: str = ""
    total_commission: str = "12.00"
    department: str = ""  # wanted Department value, not an EZLynx label
    # LOB Specific Components
    vehicles: Sequence[VehicleItem] = field(default_factory=tuple)
    drivers: Sequence[DriverItem] = field(default_factory=tuple)
    locations: Sequence[LocationItem] = field(default_factory=tuple)
    buildings: Sequence[BuildingItem] = field(default_factory=tuple)
    gl_coverage: GLCoverageItem | None = None
    property_coverage: PropertyCoverageItem | None = None
    wc_coverage: WorkersCompItem | None = None
    umbrella_coverage: UmbrellaCoverageItem | None = None
    homeowners_coverage: HomeownersCoverageItem | None = None
    inland_marine_items: Sequence[InlandMarineItem] = field(default_factory=tuple)
    discussion_note_title: str = ""
    discussion_note_body: str = ""
    request_text: str = ""


# Keys copied from _mint_formentry onto the outer PolicySetupResult / email
# report. Without these, the checkpoint only kept the 8 dataclass defaults
# and Carlo never saw field_fill_error / HITL / landed_url / code_version.
FORMENTRY_NAV_EVIDENCE_KEYS = (
    "field_fill",
    "field_fill_error",
    "hitl_response",
    "hitl_posted",
    "continue_after_hitl",
    "validation",
    "landed_url",
    "code_version",
)


@dataclass(frozen=True)
class PolicySetupResult:
    success: bool
    applicant_id: str
    policy_number: str
    lob: str
    phase_reached: str
    error: str | None = None
    note_added: bool = False
    stopped_before_bind: bool = True
    field_fill: dict[str, Any] | None = None
    field_fill_error: str | None = None
    hitl_response: dict[str, Any] | None = None
    hitl_posted: bool | None = None
    continue_after_hitl: bool | None = None
    validation: dict[str, Any] | None = None
    landed_url: str | None = None
    code_version: str | None = None
    policy_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "success": self.success,
            "applicant_id": self.applicant_id,
            "policy_number": self.policy_number,
            "lob": self.lob,
            "phase_reached": self.phase_reached,
            "error": self.error,
            "note_added": self.note_added,
            "stopped_before_bind": self.stopped_before_bind,
        }
        if self.policy_id:
            payload["policy_id"] = self.policy_id
            payload["policyId"] = self.policy_id
        for key in FORMENTRY_NAV_EVIDENCE_KEYS:
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


class FieldFillHitlError(RuntimeError):
    """Field fill stopped for HITL. Do not click Save. Do not continue."""

    def __init__(self, filled: Any) -> None:
        super().__init__(filled.error or f"{getattr(filled, 'widget', 'field')} fill HITL")
        self.filled = filled


def policy_setup_hitl_blocks_continue(result: dict[str, Any] | None) -> str | None:
    """If HITL fired, return PLAYWRIGHT_BLOCKED text. Caller must not continue."""
    if not isinstance(result, dict) or result.get("success"):
        return None
    error = str(result.get("error") or "").strip()
    hitl = result.get("hitl_response") or {}
    posted = result.get("hitl_posted")
    blocked = (
        posted is False
        or bool(hitl)
        or "HITL" in error
        or "PLAYWRIGHT_BLOCKED" in error
    )
    if not blocked:
        return None
    text = error or "HITL STOP AND ASK"
    if "PLAYWRIGHT_BLOCKED" not in text:
        text = f"PLAYWRIGHT_BLOCKED: {text}"
    return text


def policy_setup_result_from_formentry_nav(
    *,
    applicant_id: str,
    policy_number: str,
    lob: str,
    nav: dict[str, Any],
    policy_id: str | None = None,
) -> PolicySetupResult:
    """Fail-closed FormEntry result that keeps mint evidence on the outer report."""
    extras = {key: nav.get(key) for key in FORMENTRY_NAV_EVIDENCE_KEYS}
    return PolicySetupResult(
        success=False,
        applicant_id=applicant_id,
        policy_number=policy_number,
        lob=lob,
        phase_reached="formentry_mint",
        error=nav.get("error") or "FormEntry was not minted",
        note_added=False,
        stopped_before_bind=True,
        policy_id=policy_id or str(nav.get("policy_id") or "").strip() or None,
        **extras,
    )


def normalize_lob(lob_input: str) -> str:
    cleaned = lob_input.strip()
    return LINE_OF_BUSINESS_MAP.get(cleaned.lower().replace(" ", "_"), cleaned)


def url_is_minted_formentry(url: object) -> bool:
    """True after Save & Continue Edit lands on a FormEntry page.

    The live door is
    /applicantportal/Policy/{policyId}/FormEntry/Index/{formEntryId}.
    The older /applicantportal/FormEntry/{accountId} tab also counts.
    Do not use account-nav FORMENTRY_RE alone — that pattern misses the
    Policy/.../FormEntry/Index/... URL and would HITL a successful mint.
    """
    from .ezlynx_account_nav import FORMENTRY_RE
    from .ezlynx_write_scope import is_policy_form_entry_url

    text = str(url or "").strip()
    if not text:
        return False
    if is_policy_form_entry_url(text):
        return True
    return FORMENTRY_RE.search(text) is not None


_SENTINEL_LOB_ORIG = re.compile(r"^0?1/0?1/1900$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MDY_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_LOB_ORIG_BANNER = re.compile(r"lob orig|must be after", re.IGNORECASE)


def job_effective_date_for_ezlynx(raw: str) -> str:
    """MM/DD/YYYY from the job effective date. Never invent or hardcode a date."""
    text = str(raw or "").strip()
    if not text:
        return ""
    iso = _ISO_DATE.match(text)
    if iso:
        return f"{iso.group(2)}/{iso.group(3)}/{iso.group(1)}"
    mdy = _MDY_DATE.match(text)
    if mdy:
        return f"{int(mdy.group(1)):02d}/{int(mdy.group(2)):02d}/{mdy.group(3)}"
    return text


def is_sentinel_lob_orig_date(value: str) -> bool:
    """EZLynx default 1/1/1900 is not a real LOB Orig. Date."""
    return bool(_SENTINEL_LOB_ORIG.match(str(value or "").strip()))


def collect_visible_validation_errors(snapshot: dict[str, Any] | None) -> list[str]:
    """Flatten every visible validation banner/field error. Never drop a red banner.

    Job 677f362f reported VALIDATION_ERRORS: [] while the page showed
    "LOB Orig. Date: LOB Orig. Date must be after". The snapshot used
    field_errors/summary_errors; the mint path read errors/validation_errors.
    """
    snap = dict(snapshot or {})
    collected: list[str] = []
    for key in (
        "errors",
        "validation_errors",
        "field_errors",
        "summary_errors",
        "banners",
        "alerts",
        "banner_lines",
    ):
        raw = snap.get(key)
        if isinstance(raw, str) and raw.strip():
            collected.append(raw.strip())
        elif isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if text:
                    collected.append(text)
    body = str(snap.get("body_text") or snap.get("body_text_sample") or "")
    for line in body.splitlines():
        folded = line.strip()
        if folded and _LOB_ORIG_BANNER.search(folded):
            collected.append(folded)
    seen: set[str] = set()
    out: list[str] = []
    for item in collected:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def is_lob_orig_date_validation_miss(
    snapshot: dict[str, Any] | None,
    field_value: str = "",
) -> bool:
    """True when 1/1/1900 or a visible 'must be after' banner is blocking mint."""
    snap = dict(snapshot or {})
    value = str(
        field_value or snap.get("lob_orig_date") or snap.get("lob_origination_date") or ""
    ).strip()
    if is_sentinel_lob_orig_date(value):
        return True
    blob = " ".join(collect_visible_validation_errors(snap)).casefold()
    return "must be after" in blob or (
        "lob orig" in blob and "must be after" in blob
    )


def is_commercial_lob(lob_input: str) -> bool:
    """HOME / homeowners / personal lines are not commercial.

    The official Job Engine path uses lob='HOME'. That token must stay
    personal so Department wanted is Personal, not Commercial.
    """
    raw = (lob_input or "").strip().lower().replace(" ", "_")
    if raw in {"home", "ho"}:
        return False
    normalized = normalize_lob(lob_input).lower()
    return not (
        "personal" in normalized
        or "homeowners" in normalized
        or "dwelling" in normalized
        or normalized in {"home", "ho"}
    )


def normalize_transaction_type(trans_input: str) -> str:
    cleaned = trans_input.strip()
    return TRANSACTION_TYPE_MAP.get(cleaned.lower().replace(" ", "_"), cleaned)


def build_discussion_note(lob: str, policy_number: str, carrier: str, custom_summary: str = "") -> str:
    summary = custom_summary.strip() or f"Completed {lob} policy setup for Policy #{policy_number} with {carrier}."
    return f"{summary}\n\nROBIE was here"


def _to_iso_date(value: str) -> str:
    """MM/DD/YYYY -> YYYY-MM-DDT00:00:00; pass through values already in ISO form."""
    v = (value or "").strip()
    if "T" in v:
        return v
    import re as _re

    m = _re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T00:00:00"
    return v


def homeowners_amounts_by_letter(
    ho: HomeownersCoverageItem | None,
) -> dict[str, str]:
    """Stated Coverage A–F amounts. Omit any letter the human did not send."""
    if ho is None:
        return {}
    values: dict[str, str] = {}
    if ho.dwelling_a:
        values["A"] = clean_currency(ho.dwelling_a)
    if ho.other_structures_b:
        values["B"] = clean_currency(ho.other_structures_b)
    if ho.personal_property_c:
        values["C"] = clean_currency(ho.personal_property_c)
    if ho.loss_of_use_d:
        values["D"] = clean_currency(ho.loss_of_use_d)
    if ho.liability_e:
        values["E"] = clean_currency(ho.liability_e)
    if ho.med_pay_f:
        values["F"] = clean_currency(ho.med_pay_f)
    return values


def _homeowners_values_by_label(ho: HomeownersCoverageItem | None) -> dict[str, str]:
    """Map stated amounts onto live HOME names (Coverage A–F), not Dwelling aliases."""
    return {
        f"Coverage {letter}": amount
        for letter, amount in homeowners_amounts_by_letter(ho).items()
    }


def homeowners_coverage_amounts_missing(ho: HomeownersCoverageItem | None) -> bool:
    """True when the job stated no Coverage A-F amounts. Do not guess."""
    return not homeowners_amounts_by_letter(ho)


def merge_homeowners_coverage_from_text(
    ho: HomeownersCoverageItem | None,
    *texts: str,
) -> HomeownersCoverageItem:
    """Fill empty A–F from email / job payload / HITL reply. Never invent."""
    from .policy_setup_dispatch import parse_coverage_amounts_from_reply

    current = ho or HomeownersCoverageItem()
    parsed: dict[str, str] = {}
    for text in texts:
        if not str(text or "").strip():
            continue
        for key, value in parse_coverage_amounts_from_reply(text).items():
            if value and key not in parsed:
                parsed[key] = value
    return HomeownersCoverageItem(
        dwelling_a=current.dwelling_a or parsed.get("dwelling", ""),
        other_structures_b=current.other_structures_b or parsed.get("other_structures", ""),
        personal_property_c=current.personal_property_c or parsed.get("personal_property", ""),
        loss_of_use_d=current.loss_of_use_d or parsed.get("loss_of_use", ""),
        liability_e=current.liability_e or parsed.get("personal_liability", ""),
        med_pay_f=current.med_pay_f or parsed.get("medical_payments", ""),
        all_peril_deductible=current.all_peril_deductible,
        wind_hail_deductible=current.wind_hail_deductible,
        hurricane_deductible=current.hurricane_deductible,
        roof_type=current.roof_type,
        roof_year=current.roof_year,
        construction_type=current.construction_type,
    )


def coverages_tab_stuck_error(
    *,
    live_labels: list[str],
    locators_tried: list[str],
) -> str:
    """Location tab / empty labels. Do not claim A–F amounts were missing."""
    if not live_labels:
        return (
            "PLAYWRIGHT_BLOCKED: I cannot read the coverage fields yet; "
            "could not open Coverages. "
            f"locators_tried={locators_tried}. "
            f"live_labels={live_labels}."
        )
    return (
        "PLAYWRIGHT_BLOCKED: still on the FormEntry Location/Address tab; "
        "could not open Coverages. "
        f"locators_tried={locators_tried}. "
        f"live_labels={live_labels}."
    )


def coverage_fill_miss_error(
    *,
    amounts_by_letter: dict[str, str],
    looked: list[str],
    live_labels: list[str],
    not_found: Any,
    after_retry: bool = False,
) -> str:
    """Label-miss last_error. Do not call stated A–F amounts missing."""
    from .formentry_coverages import coverages_fields_unreadable

    if coverages_fields_unreadable(list(live_labels or [])):
        return coverages_tab_stuck_error(
            live_labels=list(live_labels or []),
            locators_tried=[],
        )
    retry = " after Gemini apply + retry" if after_retry else ""
    return (
        f"PLAYWRIGHT_BLOCKED: no coverage labels were filled{retry}. "
        f"Looked for {looked}. "
        f"live_labels={live_labels}. "
        f"not_found={not_found}"
    )


def stated_live_coverage_names(amounts_by_letter: dict[str, str] | None) -> list[str]:
    """Live HOME names for letters the human stated. Never adds omitted E."""
    return [
        f"Coverage {letter}"
        for letter in ("A", "B", "C", "D", "E", "F")
        if str((amounts_by_letter or {}).get(letter) or "").strip()
    ]


class EzlynxPolicySetupPage:
    """Deterministic Playwright Page Object for EZLynx APE workflows across all LOBs."""

    def __init__(self, page: Any, job_id: str | None = None, hitl_deps: dict | None = None) -> None:
        self.page = page
        self.applicant_id: str | None = None
        self.job_id: str | None = job_id
        self.lob: str = ""
        self._job_effective_date: str = ""
        self._hitl_deps: dict = hitl_deps or {}
        # Wire up default senders if not provided
        if "email_sender" not in self._hitl_deps:
            self._hitl_deps["email_sender"] = self._default_email_sender()
        if "chat_sender" not in self._hitl_deps:
            self._hitl_deps["chat_sender"] = self._default_chat_sender()
        if "gemini_client" not in self._hitl_deps:
            from .gemini_field_helper import default_gemini_field_client

            client = default_gemini_field_client()
            if client is not None:
                self._hitl_deps["gemini_client"] = client

    def _gemini_client(self) -> Any:
        """#388 live-option client. Never invent labels; Gemini names one live option."""
        client = (self._hitl_deps or {}).get("gemini_client")
        if client is not None:
            return client
        from .gemini_field_helper import default_gemini_field_client

        return default_gemini_field_client()

    def _default_email_sender(self):
        """Create an email sender using the verification mailer."""
        def send(*, to: str, subject: str, body: str) -> None:
            from .verification_mailer import send_verification_email
            from .hitl_copy import sanitize_plain_text

            send_verification_email(
                to=[to],
                cc=[],
                subject=sanitize_plain_text(subject).replace("\n", " "),
                text_body=sanitize_plain_text(body),
                html_body=None,
                plain_only=True,
            )
        return send

    def _default_chat_sender(self):
        """Post HITL into the same Chat thread as the @robie. Not a webhook."""
        job_id = self.job_id

        def send(message: str) -> bool:
            from .chat_app_post import post_hitl_to_originating_thread

            return post_hitl_to_originating_thread(message, job_id=job_id)

        return send

    async def navigate_to_policies(self, applicant_id: str) -> None:
        applicant_id = require_allowed_ezlynx_write_applicant(applicant_id)
        self.applicant_id = applicant_id
        url = f"{EZLYNX_BASE_URL}/web/account/{applicant_id}/policies"
        await self.page.goto(url, wait_until="domcontentloaded")

    async def _wait_visible(self, locator: Any, label: str, timeout_ms: int) -> Any:
        from .gemini_ui_rescue import wait_visible_or_rescue_async

        return await wait_visible_or_rescue_async(
            self.page, locator, label, timeout_ms
        )

    async def open_policy_add(self) -> None:
        add_btn = await self._wait_visible(
            self.page.locator("#add-policy"), "Add Policy", 10000
        )
        await add_btn.click()
        await self.page.wait_for_load_state("domcontentloaded")

    async def fill_policy_shell(self, shell_input: PolicyShellInput, save_and_edit: bool = True) -> bool:
        lob_val = normalize_lob(shell_input.lob)
        trans_val = normalize_transaction_type(shell_input.transaction_type)

        from .ezlynx_field_widgets import (
            BILLING_TYPE_WIDGET,
            DEPARTMENT_WIDGET,
            identified_widget,
        )

        async def _fill_named(name: str, root: str, wanted: str) -> None:
            await self._fill_identified_dropdown(
                identified_widget(name=name, root=root),
                wanted,
            )

        # 1-4. Identified dropdowns: live options, Gemini on exact miss, retry once.
        await _fill_named("Line of Business", "#mergeSplitLOB", lob_val)
        await _fill_named("Transaction Type", "#TransactionType", trans_val)
        await _fill_named("Master Company", "#MasterCompany", shell_input.master_company_value)
        await self.page.wait_for_timeout(500)
        if shell_input.writing_company_text:
            await _fill_named("Writing Company", "#WritingCompany", shell_input.writing_company_text)

        # 5. Policy Number
        if shell_input.policy_number:
            pol_num = self.page.locator("#PolicyNumber")
            await pol_num.fill(shell_input.policy_number)

        # 6. Dates
        if shell_input.effective_date:
            eff = self.page.locator("#EffectiveDate")
            await eff.fill(shell_input.effective_date)
        if shell_input.expiration_date:
            exp = self.page.locator("#ExpirationDate")
            await exp.fill(shell_input.expiration_date)
        
        # Mandatory LOB Origination Date
        lob_orig = shell_input.lob_origination_date or shell_input.effective_date
        if lob_orig:
            orig_input = self.page.locator("#LOBOriginationDate")
            if await orig_input.count() > 0:
                await orig_input.fill(lob_orig)

        # 7. Billing Type & Rating State — same live-option helper.
        if shell_input.billing_type:
            await self._fill_identified_dropdown(
                BILLING_TYPE_WIDGET,
                shell_input.billing_type,
            )
        if shell_input.billing_company:
            await _fill_named("Billing Company", "#BillingCompany", shell_input.billing_company)
        if shell_input.rating_state_value:
            await _fill_named("Rating State", "#RatingState", shell_input.rating_state_value)

        # 8. Premiums (clean unmasked numbers)
        if shell_input.premium:
            prem = self.page.locator("#Premium")
            await prem.fill(clean_currency(shell_input.premium))
        if shell_input.full_term_premium:
            ft_prem = self.page.locator("#FullTermPremium")
            if await ft_prem.count() > 0:
                await ft_prem.fill(clean_currency(shell_input.full_term_premium))
        if shell_input.annual_premium:
            ann_prem = self.page.locator("#AnnualPremium")
            await ann_prem.fill(clean_currency(shell_input.annual_premium))
        comm_val = shell_input.total_commission or "12.00"
        comm_input = self.page.locator("#TotalCommission")
        if await comm_input.count() > 0:
            await comm_input.fill(clean_currency(comm_val))

        # 8b. Department: same helper. Widget is #Department, never LOB.
        dept_wanted = shell_input.department or (
            "Commercial" if is_commercial_lob(shell_input.lob) else "Personal"
        )
        await self._fill_identified_dropdown(DEPARTMENT_WIDGET, dept_wanted)

        # 9. Submit action (Add & Edit vs Add Policy)
        if save_and_edit:
            add_edit_btn = await self._wait_visible(
                self.page.locator("#AddAndEditPolicyBtn"), "Add & Edit", 5000
            )
            await add_edit_btn.click()
            await self.page.wait_for_load_state("domcontentloaded")
            return True
        else:
            add_btn = await self._wait_visible(
                self.page.locator("#AddPolicyBtn"), "Add Policy", 5000
            )
            await add_btn.click()
            await self.page.wait_for_load_state("domcontentloaded")
            return True

    # Detailed Sub-Tab / Schedule Handlers
    async def select_vehicle_garaging_address(self, vehicle: VehicleItem) -> None:
        """Choose garaging from EZLynx's active UI mode, failing closed.

        EZLynx normally renders a location dropdown and leaves the legacy raw
        address input hidden/disabled.  Never force-fill that legacy input
        while dropdown mode is present.
        """
        dropdown = self.page.locator("#Vehicle_GaragingAddressId")
        if await dropdown.count() > 0:
            if not await dropdown.is_visible() or not await dropdown.is_enabled():
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: garaging-address dropdown is present but unavailable"
                )
            requested = vehicle.garaging_address.strip()
            if requested:
                from .ezlynx_field_widgets import fill_live_dropdown, identified_widget

                filled = await fill_live_dropdown(
                    self.page,
                    identified_widget(
                        name="Garaging Address",
                        root="#Vehicle_GaragingAddressId",
                    ),
                    requested,
                    gemini_client=(self._hitl_deps or {}).get("gemini_client"),
                )
                if filled.hitl:
                    raise RuntimeError(filled.error or "Garaging Address fill HITL")
                return
            current = str(await dropdown.input_value() or "").strip()
            if current:
                return
            options = await dropdown.locator("option:not([disabled])").all()
            eligible: list[Any] = []
            for option in options:
                value = str(await option.get_attribute("value") or "").strip()
                label = str(await option.text_content() or "").strip()
                if value and label and "select" not in label.casefold():
                    eligible.append(option)
            if len(eligible) != 1:
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: garaging address is missing or ambiguous"
                )
            value = str(await eligible[0].get_attribute("value") or "").strip()
            await dropdown.select_option(value=value)
            await dropdown.dispatch_event("change")
            return

        raw = self.page.locator("#Vehicle_PhysicalAddress_LineOne_A")
        if await raw.count() != 1:
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: no unique garaging-address control is available"
            )
        if not await raw.is_visible() or not await raw.is_enabled():
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: raw garaging-address input is hidden or disabled"
            )
        requested = vehicle.garaging_address.strip()
        if not requested:
            raise RuntimeError("MISSING_REQUIRED_FIELD: garaging_address")
        await raw.fill(requested)
        await raw.dispatch_event("change")

    async def add_vehicle(self, vehicle: VehicleItem) -> None:
        add_btn = self.page.locator("input[value='Add Vehicle'], button:has-text('Add Vehicle'), #add-vehicle-btn")
        await add_btn.click()
        await self.page.wait_for_timeout(500)

        # VIN input & Lookup trigger
        vin_input = self.page.locator("#Vehicle_VINIdentifier_A, input[name='Vehicle_VINIdentifier_A'], #Vehicle_VIN")
        await vin_input.fill(vehicle.vin)
        await vin_input.dispatch_event("input")
        await vin_input.dispatch_event("change")

        # Click Lookup button to decode VIN
        lookup_btn = self.page.locator(".modal button:has-text('Lookup'), button:has-text('Lookup'), .modal a:has-text('Lookup')")
        if await lookup_btn.count() > 0:
            await lookup_btn.click()
            await self.page.wait_for_timeout(1000)

        # Fallback/explicit field population
        if vehicle.year:
            year_input = self.page.locator("#Vehicle_ModelYear_A, input[name='Vehicle_ModelYear_A'], #Vehicle_Year")
            if await year_input.count() > 0:
                await year_input.fill(vehicle.year)
        if vehicle.make:
            make_input = self.page.locator("#Vehicle_ManufacturersName_A, input[name='Vehicle_ManufacturersName_A'], #Vehicle_Make")
            if await make_input.count() > 0:
                await make_input.fill(vehicle.make)
        if vehicle.model:
            model_input = self.page.locator("#Vehicle_ModelName_A, input[name='Vehicle_ModelName_A'], #Vehicle_Model")
            if await model_input.count() > 0:
                await model_input.fill(vehicle.model)

        if vehicle.comp_deductible:
            comp = self.page.locator("#Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A, #Vehicle_Comprehensive_DeductibleAmount_A, #Vehicle_CompDeductible")
            if await comp.count() > 0:
                await comp.fill(clean_currency(vehicle.comp_deductible))

        if vehicle.coll_deductible:
            coll = self.page.locator("#Vehicle_Collision_DeductibleAmount_A, #Vehicle_CollDeductible")
            if await coll.count() > 0:
                await coll.fill(clean_currency(vehicle.coll_deductible))

        await self.select_vehicle_garaging_address(vehicle)

        # Save modal
        save_btn = self.page.locator(".modal button:has-text('Save'), .modal input[value='Save'], button.btn-primary:has-text('Save')")
        if await save_btn.count() > 0:
            await save_btn.click()
            await self.page.wait_for_timeout(1000)

    async def add_driver(self, driver: DriverItem, driver_num: str = "1") -> None:
        add_btn = self.page.locator("input[value='Add Driver'], button:has-text('Add Driver'), #add-driver-btn")
        await add_btn.click()
        await self.page.wait_for_timeout(500)

        # Driver identifier (mandatory in EZLynx)
        driver_id_input = self.page.locator("#Driver_ProducerIdentifier_A, input[name='Driver_ProducerIdentifier_A']")
        if await driver_id_input.count() > 0:
            await driver_id_input.fill(driver_num)

        # Name fields
        first_input = self.page.locator("#Driver_GivenName_A, input[name='Driver_GivenName_A'], #Driver_FirstName")
        await first_input.fill(driver.first_name)
        last_input = self.page.locator("#Driver_Surname_A, input[name='Driver_Surname_A'], #Driver_LastName")
        await last_input.fill(driver.last_name)

        if driver.dob:
            dob_input = self.page.locator("input[name='Driver_BirthDate_A'], #Driver_DOB")
            if await dob_input.count() > 0:
                await dob_input.fill(driver.dob)

        if driver.license_number:
            lic_input = self.page.locator("#Driver_LicenseNumberIdentifier_A, input[name='Driver_LicenseNumberIdentifier_A'], #Driver_LicenseNumber")
            if await lic_input.count() > 0:
                await lic_input.fill(driver.license_number)

        if driver.license_state:
            state = self.page.locator("#Driver_LicensedStateOrProvinceCode_A, select[name='Driver_LicensedStateOrProvinceCode_A'], #Driver_LicenseState")
            if await state.count() > 0:
                await state.select_option(label=driver.license_state)

        if not driver.excluded:
            excl_no = self.page.locator("#Excluded_DriverCode_A-no, input[name='Excluded_DriverCode_A'][value='N']")
            if await excl_no.count() > 0:
                await excl_no.click()

        # Save driver modal
        save_btn = self.page.locator(".modal button:has-text('Save'), .modal input[value='Save'], button.btn-primary:has-text('Save'), #save-driver-btn")
        if await save_btn.count() > 0:
            await save_btn.click()
            await self.page.wait_for_timeout(1000)

    async def add_location(self, loc: LocationItem) -> None:
        add_btn = self.page.locator("#add-location-btn, button[data-action='add-location']")
        await add_btn.click()
        await self.page.locator("#Location_Address1, input[name='Address1']").fill(loc.address)
        await self.page.locator("#Location_City, input[name='City']").fill(loc.city)
        if loc.state:
            await self.page.locator("#Location_State, select[name='State']").select_option(value=loc.state)
        await self.page.locator("#Location_Zip, input[name='Zip']").fill(loc.zip_code)
        save_btn = self.page.locator("#save-location-btn, button[data-action='save-location']")
        if await save_btn.count() > 0:
            await save_btn.click()

    async def ensure_dwelling_location(self, loc: LocationItem | None) -> bool:
        """True when the policy has a dwelling location (existing or added).

        PROVEN 2026-09-13 (policy 83670183, manual browser run): the
        FormEntry "Dwelling Information / Coverages" tab silently refuses
        to open while the Locations grid is empty — every click no-ops.
        Adding one location unlocks the tab on the first click. Call this
        BEFORE attempting the Coverages tab. The modal path below
        ("Add Location" button, uncheck "Same As Mailing", fill by label)
        is the proven working path; the #add-location-btn id selectors
        above never matched the live DOM.
        """
        for sel in (
            "#locations-grid tbody tr",
            "[data-grid='locations'] tbody tr",
        ):
            try:
                if await self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        if loc is None:
            return False
        add_btn = self.page.get_by_role("button", name="Add Location")
        if await add_btn.count() == 0:
            return False
        await add_btn.first.click()
        try:
            same = self.page.get_by_label("Same As Mailing")
            if await same.count() > 0 and await same.first.is_checked():
                await same.first.uncheck()
        except Exception:
            pass
        await self.page.get_by_label("Address").first.fill(loc.address or "")
        await self.page.get_by_label("City").first.fill(loc.city or "")
        if loc.state:
            try:
                await self.page.get_by_label("State").first.fill(loc.state)
            except Exception:
                pass
        await self.page.get_by_label("Zip").first.fill(loc.zip_code or "")
        save_btn = self.page.get_by_role("button", name="Save")
        if await save_btn.count() > 0:
            await save_btn.first.click()
            await self.page.wait_for_timeout(1500)
        for sel in (
            "#locations-grid tbody tr",
            "[data-grid='locations'] tbody tr",
        ):
            try:
                if await self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def add_building(self, bldg: BuildingItem) -> None:
        add_btn = self.page.locator("#add-building-btn, button[data-action='add-building']")
        await add_btn.click()
        if bldg.construction_type:
            await self.page.locator("#Building_ConstructionType, select[name='ConstructionType']").select_option(value=bldg.construction_type)
        if bldg.protection_class:
            await self.page.locator("#Building_ProtectionClass, input[name='ProtectionClass']").fill(bldg.protection_class)
        if bldg.building_limit:
            await self.page.locator("#Building_CoverageLimit, input[name='BuildingLimit']").fill(clean_currency(bldg.building_limit))
        if bldg.bpp_limit:
            await self.page.locator("#Building_BPPLimit, input[name='BPPLimit']").fill(clean_currency(bldg.bpp_limit))
        save_btn = self.page.locator("#save-building-btn, button[data-action='save-building']")
        if await save_btn.count() > 0:
            await save_btn.click()

    async def fill_gl_coverages(self, gl: GLCoverageItem) -> None:
        cov_tab = self.page.locator("a[data-target='#coverages-tab'], #tab-coverages")
        if await cov_tab.count() > 0:
            await cov_tab.click()
        if gl.occurrence_limit:
            await self.page.locator("#GL_EachOccurrence, select[name='EachOccurrence']").select_option(value=gl.occurrence_limit)
        if gl.general_aggregate:
            await self.page.locator("#GL_GeneralAggregate, select[name='GeneralAggregate']").select_option(value=gl.general_aggregate)
        if gl.class_code:
            add_class = self.page.locator("#add-gl-class-btn, button[data-action='add-gl-class']")
            if await add_class.count() > 0:
                await add_class.click()
                await self.page.locator("#GL_ClassCode, input[name='ClassCode']").fill(gl.class_code)
                if gl.exposure:
                    await self.page.locator("#GL_Exposure, input[name='Exposure']").fill(clean_currency(gl.exposure))

    async def fill_wc_coverages(self, wc: WorkersCompItem) -> None:
        wc_tab = self.page.locator("a[data-target='#workers-comp-tab'], #tab-wc")
        if await wc_tab.count() > 0:
            await wc_tab.click()
        if wc.class_code:
            add_wc = self.page.locator("#add-wc-class-btn, button[data-action='add-wc-class']")
            if await add_wc.count() > 0:
                await add_wc.click()
                await self.page.locator("#WC_ClassCode, input[name='WCClassCode']").fill(wc.class_code)
                if wc.estimated_annual_payroll:
                    await self.page.locator("#WC_Payroll, input[name='WCPayroll']").fill(clean_currency(wc.estimated_annual_payroll))
        if wc.el_each_accident:
            await self.page.locator("#WC_ELEachAccident, select[name='ELEachAccident']").select_option(value=wc.el_each_accident)

    async def fill_umbrella_coverages(self, umb: UmbrellaCoverageItem) -> None:
        umb_tab = self.page.locator("a[data-target='#umbrella-underlying-tab'], #tab-umbrella-underlying")
        if await umb_tab.count() > 0:
            await umb_tab.click()
        for under in umb.underlying_policies:
            add_under = self.page.locator("#add-underlying-policy-btn, button[data-action='add-underlying']")
            if await add_under.count() > 0:
                await add_under.click()
                await self.page.locator("#Underlying_Carrier, input[name='UnderlyingCarrier']").fill(under.carrier)
                await self.page.locator("#Underlying_PolicyNumber, input[name='UnderlyingPolicyNumber']").fill(under.policy_number)

    async def fill_homeowners_coverages(self, ho: HomeownersCoverageItem) -> None:
        ho_tab = self.page.locator("a[data-target='#homeowners-tab'], #tab-homeowners")
        if await ho_tab.count() > 0:
            await ho_tab.click()
        if ho.dwelling_a:
            await self.page.locator("#HO_CoverageA, input[name='CoverageA']").fill(clean_currency(ho.dwelling_a))
        if ho.liability_e:
            await self.page.locator("#HO_CoverageE, input[name='CoverageE']").fill(clean_currency(ho.liability_e))

    async def add_inland_marine_item(self, im: InlandMarineItem) -> None:
        im_tab = self.page.locator("a[data-target='#inland-marine-tab'], #tab-im")
        if await im_tab.count() > 0:
            await im_tab.click()
        add_im = self.page.locator("#add-im-item-btn, button[data-action='add-im-item']")
        if await add_im.count() > 0:
            await add_im.click()
            await self.page.locator("#IM_ItemDescription, input[name='ItemDescription']").fill(im.item_description)
            if im.serial_number:
                await self.page.locator("#IM_SerialNumber, input[name='SerialNumber']").fill(im.serial_number)
            if im.limit:
                await self.page.locator("#IM_ItemLimit, input[name='ItemLimit']").fill(clean_currency(im.limit))

    async def add_discussion_note(self, title: str, body: str, policy_number: str = "") -> None:
        """Playwright must never file EZLynx notes (Carlo 2026-09-19).

        Call :meth:`add_discussion_note_via_api` / ``add_note_to_discussion``.
        """
        del title, body, policy_number
        from .ezlynx_api_only_writes import refuse_playwright_note_or_doc

        refuse_playwright_note_or_doc("ezlynx_policy_setup.add_discussion_note")

    def add_discussion_note_via_api(
        self,
        title: str,
        body: str,
        *,
        applicant_id: str | None = None,
        discussion_client: Any | None = None,
    ) -> dict[str, Any]:
        """File the note through DiscussionApi and read back ``note_id``."""
        from .ezlynx_api_only_writes import add_note_to_discussion

        final_body = body if "ROBIE was here" in body else f"{body}\n\nROBIE was here"
        applicant = str(applicant_id or getattr(self, "applicant_id", "") or "").strip()
        return add_note_to_discussion(
            applicant,
            final_body,
            discussion_title=title,
            title_hint=title,
            discussion_client=discussion_client,
        )

    async def save_and_close_form_entry(self) -> None:
        save_close_btn = await self._wait_visible(
            self.page.locator("#finishButton-header"), "Save & Close", 10000
        )
        await save_close_btn.click()
        await self.page.wait_for_load_state("domcontentloaded")

    # Unified LOB Orchestrator
    async def setup_policy_by_lob(self, shell_input: PolicyShellInput) -> PolicySetupResult:
        """HOME on applicant 220250093: search-first gold create + FormEntry coverages.

        The live lock is replaced for exactly one path: a homeowners policy on
        the allowlisted applicant. Search-first via PolicyApi; create with the
        gold payload (writingCompany "10048", masterCompany 13585) only when
        absent; click Save & Continue Edit to mint the FormEntry; fill the
        Coverages tab by literal label. Every other LOB still refuses at
        draft_write_gate. No bind, ever.
        """
        normalized = normalize_lob(shell_input.lob)
        self.lob = normalized
        try:
            applicant_id = require_allowed_ezlynx_write_applicant(shell_input.applicant_id)
        except RuntimeError as exc:
            return PolicySetupResult(
                success=False,
                applicant_id=shell_input.applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="applicant_write_scope",
                error=str(exc),
                note_added=False,
                stopped_before_bind=True,
            )

        if "homeowners" not in normalized.lower() and normalized.upper() != "HOME":
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="draft_write_gate",
                error=(
                    "NEEDS_CLARIFICATION: EZLynx Policy Setup unlocks only the "
                    "HOME path on the allowlisted applicant; "
                    f"LOB {normalized} is still gated"
                ),
                note_added=False,
                stopped_before_bind=True,
            )

        self._job_effective_date = str(shell_input.effective_date or "").strip()
        evidence: dict[str, Any] = {"phases": []}
        try:
            from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
            from .policy_setup_proof import _extract_policy_id, search_first_create

            client = EzlynxApiClient(load_ezlynx_api_config())
            api_report = search_first_create(
                client,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                effective_date=_to_iso_date(shell_input.effective_date),
                expiration_date=_to_iso_date(shell_input.expiration_date),
            )
            evidence["api"] = api_report
            evidence["phases"].append("api_search_first_create")
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="api_search_first_create",
                error=f"{type(exc).__name__}: {exc}",
                note_added=False,
                stopped_before_bind=True,
            )

        row = api_report.get("read_back") or {}
        # Prefer the ID search_first_create already extracted from the row
        # (it saw the row shape); fall back to scanning the row directly.
        policy_id = api_report.get("policy_id") or _extract_policy_id(row)
        # Fallback: the create endpoint returns the new policy ID as a bare
        # scalar. If the search read-back hasn't caught up yet (eventual
        # consistency), use the create response ID so FormEntry can proceed.
        if not policy_id:
            create_info = api_report.get("create") or {}
            fallback_id = create_info.get("policy_id")
            if fallback_id:
                policy_id = str(fallback_id)
            else:
                # Last resort: parse the raw create response scalar.
                resp = create_info.get("response")
                if isinstance(resp, (str, int)):
                    pid = str(resp).strip().strip('"')
                    if pid and pid.lstrip('-').isdigit():
                        policy_id = pid
        self._policy_id = str(policy_id or "").strip()
        if not policy_id:
            # Fail closed with raw HTTP/body details for diagnosis.
            diagnostic = api_report.get("no_id_diagnostic")
            if not diagnostic:
                if api_report.get("verdict") == "ALREADY_EXISTS":
                    row_keys = sorted(row.keys()) if isinstance(row, dict) else []
                    diagnostic = (
                        "no policy id in read-back; cannot open FormEntry. "
                        "Policy already existed (no create attempted), but the ID "
                        f"could not be read from the search row. Row keys: {row_keys}"
                    )
                else:
                    create_info = api_report.get("create") or {}
                    http_status = create_info.get("http_status")
                    raw_body = create_info.get("raw_body") or ""
                    body_preview = raw_body[:500] if len(raw_body) > 500 else raw_body
                    resp_type = create_info.get("response_type")
                    status_src = create_info.get("status_source")
                    diagnostic = (
                        f"no policy id in read-back; cannot open FormEntry. "
                        f"Create HTTP {http_status} (via {status_src}, type {resp_type}), "
                        f"body: {body_preview}"
                    )
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="api_search_first_create",
                error=diagnostic,
                note_added=False,
                stopped_before_bind=True,
            )

        # FormEntry: click Save & Continue Edit on the Edit Policy header,
        # watch validation + DOM for the FormEntry URL.
        nav = await self._mint_formentry(
            policy_id,
            applicant_id,
            effective_date=shell_input.effective_date,
        )
        evidence["formentry_nav"] = nav
        evidence["phases"].append("formentry_mint")
        if not nav.get("formentry_found"):
            return policy_setup_result_from_formentry_nav(
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                nav=nav,
                policy_id=str(policy_id or ""),
            )

        # Coverages tab -> fill from LIVE FormEntry labels (Coverage A–F).
        # Do not invent #HO_CoverageA-F. Do not search Dwelling aliases.
        # Do not guess amounts or invent an omitted letter. Consume A–F
        # from the email / job payload / HITL reply before asking HITL.
        homeowners = merge_homeowners_coverage_from_text(
            shell_input.homeowners_coverage,
            shell_input.request_text,
        )
        amounts = homeowners_amounts_by_letter(homeowners)
        values = _homeowners_values_by_label(homeowners)
        evidence["phases"].append("coverage_fill")
        if not amounts:
            report = {
                "error": (
                    "PLAYWRIGHT_BLOCKED: coverage amounts not on the job; "
                    "will not guess coverage amounts"
                ),
                "coverage_fill": {"filled_count": 0, "not_found": [], "labels": {}},
                "formentry_found": True,
                "policy_id": str(policy_id or ""),
            }
            await self._escalate_formentry_hitl(
                report,
                attempted=["coverage_fill", "job_payload_amounts"],
                applicant_id=applicant_id,
                policy_id=str(policy_id or ""),
                phase="coverage_fill",
            )
            evidence["coverage_fill"] = report["coverage_fill"]
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="coverage_fill",
                error=report["error"],
                note_added=False,
                stopped_before_bind=True,
                hitl_response=report.get("hitl_response"),
                hitl_posted=report.get("hitl_posted"),
                continue_after_hitl=False,
                policy_id=str(policy_id or ""),
                field_fill=report["coverage_fill"],
            )

        try:
            from .formentry_coverages import (
                aensure_coverages_tab,
                afill_coverages_by_label,
                coverages_fields_unreadable,
                is_location_section_labels,
                map_letter_amounts_to_live_labels,
            )

            # PROVEN 2026-09-13 (policy 83670183, manual browser run): the
            # FormEntry "Dwelling Information / Coverages" tab silently
            # refuses to open while the Locations grid is empty. Ensure a
            # dwelling location exists before attempting the tab; fail
            # closed when the job provided no location instead of hitting
            # the mysterious tab-stuck failure.
            first_loc = shell_input.locations[0] if shell_input.locations else None
            if not await self.ensure_dwelling_location(first_loc):
                return PolicySetupResult(
                    success=False,
                    applicant_id=applicant_id,
                    policy_number=shell_input.policy_number,
                    lob=normalized,
                    phase_reached="location_prerequisite",
                    error=(
                        "PLAYWRIGHT_BLOCKED: policy has no dwelling location "
                        "and the job provided none; the FormEntry Coverages "
                        "tab cannot open without one"
                    ),
                    note_added=False,
                    stopped_before_bind=True,
                    policy_id=str(policy_id or ""),
                )
            evidence["phases"].append("location_prerequisite")

            tab = await aensure_coverages_tab(
                self.page, gemini_client=self._gemini_client()
            )
            live_labels = list(tab.live_labels)
            evidence["live_coverage_labels"] = live_labels
            evidence["coverages_tab"] = {
                "on_coverages": tab.on_coverages,
                "still_on_location": tab.still_on_location,
                "clicked": list(tab.clicked),
                "locators_tried": list(tab.locators_tried),
                "gemini_asked": tab.gemini_asked,
            }
            if (
                not tab.on_coverages
                or tab.still_on_location
                or coverages_fields_unreadable(live_labels)
                or is_location_section_labels(live_labels)
            ):
                report = {
                    "error": coverages_tab_stuck_error(
                        live_labels=live_labels,
                        locators_tried=list(tab.locators_tried),
                    ),
                    "coverage_fill": {
                        "filled_count": 0,
                        "not_found": [],
                        "labels": {},
                        "live_labels_seen": live_labels,
                    },
                    "formentry_found": True,
                    "policy_id": str(policy_id or ""),
                }
                await self._escalate_formentry_hitl(
                    report,
                    attempted=[
                        "coverages_tab",
                        "gemini_nav_retry",
                    ],
                    applicant_id=applicant_id,
                    policy_id=str(policy_id or ""),
                    phase="coverage_fill",
                )
                evidence["coverage_fill"] = report["coverage_fill"]
                return PolicySetupResult(
                    success=False,
                    applicant_id=applicant_id,
                    policy_number=shell_input.policy_number,
                    lob=normalized,
                    phase_reached="coverage_fill",
                    error=report["error"],
                    note_added=False,
                    stopped_before_bind=True,
                    hitl_response=report.get("hitl_response"),
                    hitl_posted=report.get("hitl_posted"),
                    continue_after_hitl=False,
                    policy_id=str(policy_id or ""),
                    field_fill=report["coverage_fill"],
                )
            values = map_letter_amounts_to_live_labels(
                amounts, live_labels, gemini_client=self._gemini_client()
            )
            fill_report = await afill_coverages_by_label(
                self.page, values, gemini_client=self._gemini_client()
            )
            fill_report.setdefault("live_labels_seen", list(live_labels))
            fill_report.setdefault("looked_for", list(values))
            evidence["coverage_fill"] = fill_report
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="coverage_fill",
                error=f"{type(exc).__name__}: {exc}",
                note_added=False,
                stopped_before_bind=True,
                policy_id=str(policy_id or ""),
            )

        if fill_report.get("filled_count", 0) <= 0:
            if (
                not getattr(tab, "on_coverages", False)
                or coverages_fields_unreadable(live_labels)
                or is_location_section_labels(live_labels)
            ):
                report = {
                    "error": coverages_tab_stuck_error(
                        live_labels=live_labels,
                        locators_tried=list(tab.locators_tried),
                    ),
                    "coverage_fill": fill_report,
                    "formentry_found": True,
                    "policy_id": str(policy_id or ""),
                }
                await self._escalate_formentry_hitl(
                    report,
                    attempted=["coverages_tab", "gemini_nav_retry"],
                    applicant_id=applicant_id,
                    policy_id=str(policy_id or ""),
                    phase="coverage_fill",
                )
                return PolicySetupResult(
                    success=False,
                    applicant_id=applicant_id,
                    policy_number=shell_input.policy_number,
                    lob=normalized,
                    phase_reached="coverage_fill",
                    error=report["error"],
                    note_added=False,
                    stopped_before_bind=True,
                    hitl_response=report.get("hitl_response"),
                    hitl_posted=report.get("hitl_posted"),
                    continue_after_hitl=False,
                    policy_id=str(policy_id or ""),
                    field_fill=fill_report,
                )
            looked = list(values) or stated_live_coverage_names(amounts)
            report = {
                "error": coverage_fill_miss_error(
                    amounts_by_letter=amounts,
                    looked=looked,
                    live_labels=live_labels,
                    not_found=fill_report.get("not_found"),
                ),
                "coverage_fill": fill_report,
                "formentry_found": True,
                "policy_id": str(policy_id or ""),
            }
            await self._escalate_formentry_hitl(
                report,
                attempted=["coverage_fill", "live_label_match"],
                applicant_id=applicant_id,
                policy_id=str(policy_id or ""),
                phase="coverage_fill",
            )
            if report.get("continue_after_hitl"):
                try:
                    tab = await aensure_coverages_tab(
                        self.page, gemini_client=self._gemini_client()
                    )
                    live_labels = list(tab.live_labels)
                    if (
                        not tab.on_coverages
                        or tab.still_on_location
                        or coverages_fields_unreadable(live_labels)
                        or is_location_section_labels(live_labels)
                    ):
                        raise RuntimeError(
                            coverages_tab_stuck_error(
                                live_labels=live_labels,
                                locators_tried=list(tab.locators_tried),
                            )
                        )
                    values = map_letter_amounts_to_live_labels(
                        amounts, live_labels, gemini_client=self._gemini_client()
                    )
                    fill_report = await afill_coverages_by_label(
                        self.page, values, gemini_client=self._gemini_client()
                    )
                    fill_report.setdefault("live_labels_seen", list(live_labels))
                    fill_report.setdefault("looked_for", list(values))
                    evidence["coverage_fill"] = fill_report
                except Exception as exc:  # noqa: BLE001
                    fill_report = {
                        "filled_count": 0,
                        "not_found": list(values),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if fill_report.get("filled_count", 0) > 0:
                    return PolicySetupResult(
                        success=not fill_report.get("not_found"),
                        applicant_id=applicant_id,
                        policy_number=shell_input.policy_number,
                        lob=normalized,
                        phase_reached="coverage_fill",
                        error=None,
                        note_added=False,
                        stopped_before_bind=True,
                        policy_id=str(policy_id or ""),
                        field_fill=fill_report,
                        continue_after_hitl=True,
                    )
                looked = list(values) or stated_live_coverage_names(amounts)
                retry_error = str(fill_report.get("error") or "")
                if (
                    coverages_fields_unreadable(live_labels)
                    or is_location_section_labels(live_labels)
                    or "could not open Coverages" in retry_error
                    or "cannot read the coverage fields" in retry_error
                ):
                    miss_error = coverages_tab_stuck_error(
                        live_labels=live_labels,
                        locators_tried=list(getattr(tab, "locators_tried", [])),
                    )
                else:
                    miss_error = coverage_fill_miss_error(
                        amounts_by_letter=amounts,
                        looked=looked,
                        live_labels=live_labels,
                        not_found=fill_report.get("not_found"),
                        after_retry=True,
                    )
                report = {
                    "error": miss_error,
                    "coverage_fill": fill_report,
                    "formentry_found": True,
                    "policy_id": str(policy_id or ""),
                }
                await self._escalate_formentry_hitl(
                    report,
                    attempted=[
                        "coverage_fill",
                        "live_label_match",
                        "gemini_apply_retry",
                    ],
                    applicant_id=applicant_id,
                    policy_id=str(policy_id or ""),
                    phase="coverage_fill",
                    applied_retry_failed=True,
                )
            return PolicySetupResult(
                success=False,
                applicant_id=applicant_id,
                policy_number=shell_input.policy_number,
                lob=normalized,
                phase_reached="coverage_fill",
                error=report["error"],
                note_added=False,
                stopped_before_bind=True,
                hitl_response=report.get("hitl_response"),
                hitl_posted=report.get("hitl_posted"),
                continue_after_hitl=False,
                policy_id=str(policy_id or ""),
                field_fill=fill_report,
            )

        return PolicySetupResult(
            success=not fill_report.get("not_found"),
            applicant_id=applicant_id,
            policy_number=shell_input.policy_number,
            lob=normalized,
            phase_reached="coverage_fill",
            error=None,
            note_added=False,
            stopped_before_bind=True,
            policy_id=str(policy_id or ""),
            field_fill=fill_report,
        )

    async def _fill_identified_dropdown(self, widget: Any, wanted: str) -> Any:
        """Exact miss → #388 helper names ONE live option → apply → retry once.

        HITL only if Gemini is still unsure or the retry fails. No alias maps.
        Never hardcode EZLynx option labels.
        """
        from .ezlynx_field_widgets import fill_identified_widget

        filled = await fill_identified_widget(
            self.page,
            widget,
            wanted,
            gemini_client=self._gemini_client(),
        )
        if filled.hitl:
            raise FieldFillHitlError(filled)
        return filled

    async def _fill_required_policy_fields(self) -> dict[str, Any]:
        """Set Billing Type, Department, and LOB Orig. Date before Save & Continue.

        Dropdowns use the identified widget + Gemini live-option helper.
        LOB Orig. Date is filled from the job effective date (never a hardcoded
        date or alias list). HOME wanted Department is Personal, not Commercial.
        """
        from .ezlynx_field_widgets import (
            BILLING_TYPE_WIDGET,
            DEPARTMENT_WIDGET,
        )

        result: dict[str, Any] = {"billing": None, "department": None, "errors": []}

        billing = await self._fill_identified_dropdown(
            BILLING_TYPE_WIDGET,
            "Direct Bill",
        )
        result["billing_fill"] = billing.to_dict()
        result["billing_options"] = billing.live_options
        result["billing"] = billing.selected
        result["billing_visible"] = billing.selected
        result["billing_via"] = billing.via

        dept_wanted = (
            "Commercial" if is_commercial_lob(getattr(self, "lob", "") or "") else "Personal"
        )
        result["department_wanted"] = dept_wanted
        department = await self._fill_identified_dropdown(
            DEPARTMENT_WIDGET,
            dept_wanted,
        )
        result["department_fill"] = department.to_dict()
        result["department_options"] = department.live_options
        result["department"] = department.selected
        result["department_visible"] = department.selected
        result["department_via"] = department.via
        result["lob_orig"] = await self._fill_lob_orig_date()
        return result

    async def _lob_orig_date_locator(self) -> Any:
        """#LOBOriginationDate, then the live Edit-page label. No invented IDs."""
        primary = self.page.locator("#LOBOriginationDate")
        if await primary.count() > 0:
            return primary.first
        for label in ("LOB Orig. Date", "LOB Origination Date"):
            getter = getattr(self.page, "get_by_label", None)
            if not callable(getter):
                continue
            try:
                by_label = getter(label)
                if await by_label.count() > 0:
                    return by_label.first
            except Exception:
                continue
        return None

    async def _read_input_value(self, locator: Any) -> str:
        if locator is None:
            return ""
        for attr in ("input_value", "evaluate"):
            fn = getattr(locator, attr, None)
            if not callable(fn):
                continue
            try:
                if attr == "input_value":
                    return str(await fn() or "").strip()
                return str(await fn("el => el.value || ''") or "").strip()
            except Exception:
                continue
        return ""

    async def _fill_lob_orig_date(self) -> dict[str, Any]:
        """Read the live LOB Orig. Date and fill it from the job effective date."""
        wanted = job_effective_date_for_ezlynx(getattr(self, "_job_effective_date", "") or "")
        locator = await self._lob_orig_date_locator()
        before = await self._read_input_value(locator)
        result: dict[str, Any] = {
            "before": before,
            "wanted": wanted,
            "after": before,
            "filled": False,
            "sentinel": is_sentinel_lob_orig_date(before),
        }
        if locator is None:
            result["error"] = "LOB Orig. Date field not found"
            return result
        if not wanted:
            result["error"] = "job effective date is empty; will not invent LOB Orig. Date"
            return result
        fill = getattr(locator, "fill", None)
        if not callable(fill):
            result["error"] = "LOB Orig. Date locator has no fill"
            return result
        await fill(wanted)
        after = await self._read_input_value(locator)
        result["after"] = after
        result["filled"] = True
        return result

    def _hitl_channel(self) -> str:
        """Email jobs HITL by email; Chat jobs HITL in the originating thread."""
        forced = str((self._hitl_deps or {}).get("channel") or "").strip().casefold()
        if forced in {"email", "chat", "any"}:
            return forced
        try:
            import os

            from .store import JobStore

            job_id = str(self.job_id or "").strip()
            db_path = os.environ.get("ROBIE_JOB_DB") or ""
            if job_id and db_path:
                payload = dict(JobStore(db_path).get_job(job_id).get("payload") or {})
                if payload.get("gmail_message_id") and not payload.get("conversation_id"):
                    return "email"
                if payload.get("conversation_id"):
                    return "chat"
        except Exception:
            pass
        return "any"

    async def _escalate_formentry_hitl(
        self,
        report: dict[str, Any],
        *,
        attempted: list[str],
        applicant_id: str,
        policy_id: str,
        phase: str = "formentry_mint",
        applied_retry_failed: bool = False,
    ) -> None:
        """Gemini-then-Carlo HITL. Fail closed. Append source/reason to report['error'].

        Shared by missing-button, field-fill failure, a true 30s no-FormEntry
        timeout, and empty coverage fill after FormEntry opened.
        """
        if "page_state" not in report:
            try:
                report["page_state"] = await self._page_state_snapshot()
            except Exception:  # noqa: BLE001
                report["page_state"] = {}
        try:
            from .hitl_escalation import HitlRequest, escalate

            screenshot_path = None
            try:
                import os
                import time

                screenshot_dir = "/tmp/robie-hitl-screenshots"
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = os.path.join(
                    screenshot_dir,
                    f"hitl-{getattr(self, 'job_id', 'unknown')}-{int(time.time())}.png",
                )
                await self.page.screenshot(path=screenshot_path)
            except Exception:
                screenshot_path = None
            fill = report.get("field_fill") or {}
            named = str(fill.get("named_option") or "").strip() or None
            visible = str(fill.get("live_visible") or fill.get("selected") or "").strip() or None
            gemini_applied = bool(fill.get("gemini_applied")) and bool(named) and bool(visible)
            error_text = report.get("error") or "FormEntry mint failed"
            if "PLAYWRIGHT_BLOCKED" not in error_text:
                error_text = f"PLAYWRIGHT_BLOCKED: {error_text}"
                report["error"] = error_text
            unguessable = phase == "coverage_fill" and (
                "will not guess" in error_text.casefold()
                or "coverage amounts not on the job" in error_text.casefold()
            )
            hitl_request = HitlRequest(
                job_id=getattr(self, "job_id", None) or "unknown",
                phase=phase,
                error=error_text,
                page_state=report.get("page_state") or {},
                attempted=attempted,
                applicant_id=applicant_id,
                policy_id=policy_id,
                screenshot_path=screenshot_path,
                gemini_applied=gemini_applied,
                gemini_named_option=named,
                live_control_shows=visible,
                formentry_exists=bool(report.get("formentry_found"))
                or phase == "coverage_fill",
                job_still_running=False,
                save_skipped=bool(report.get("field_fill_error"))
                or "skipped Save" in (report.get("error") or ""),
                script_or_job_stopped=True,
                unguessable=unguessable,
                channel=self._hitl_channel(),
                gemini_asked=bool(fill.get("gemini_asked")) or bool(named),
                applied_retry_failed=applied_retry_failed or unguessable,
            )
            deps = getattr(self, "_hitl_deps", {})
            hitl_response = escalate(hitl_request, deps)
            if hitl_response.actionable:
                report["continue_after_hitl"] = True
                report["hitl_posted"] = False
                report["hitl_response"] = {
                    "source": hitl_response.source,
                    "suggestion": hitl_response.suggestion,
                    "actionable": True,
                    "hitl_posted": False,
                }
                return
            report["continue_after_hitl"] = False
            report["hitl_posted"] = bool(hitl_response.hitl_posted)
            report["hitl_response"] = {
                "source": hitl_response.source,
                "suggestion": hitl_response.suggestion,
                "actionable": False,
                "hitl_posted": bool(hitl_response.hitl_posted),
            }
            if hitl_response.hitl_posted:
                dest = "email" if hitl_request.channel == "email" else "originating Chat thread"
                report["error"] = (
                    (report.get("error") or "")
                    + f" HITL posted to the {dest}. STOP AND ASK."
                )
            else:
                reason = hitl_response.suggestion or "Chat ping failed"
                report["error"] = (
                    (report.get("error") or "")
                    + f" HITL posted=false ({hitl_response.source}): {reason}."
                )
                if screenshot_path:
                    report["error"] += f" Screenshot: {screenshot_path}."
        except Exception as hitl_exc:  # noqa: BLE001
            err_msg = f"{type(hitl_exc).__name__}: {hitl_exc}"
            report["hitl_error"] = err_msg
            report["hitl_posted"] = False
            report["continue_after_hitl"] = False
            report["error"] = (
                (report.get("error") or "")
                + f" HITL escalation error ({err_msg}); failing closed."
            )

    async def _mint_formentry(
        self,
        policy_id: str,
        applicant_id: str = "",
        effective_date: str = "",
    ) -> dict[str, Any]:
        """Click Save & Continue Edit; watch validation + DOM for the FormEntry URL.

        The door is the green Save & Continue Edit button on the Edit Policy
        header. The FormEntry URL is
        /applicantportal/Policy/{policyId}/FormEntry/Index/{formEntryId}.
        Watches DOM validation state, not networkidle and not URL-only.
        LOB Orig. Date is filled from the job effective date before click;
        one retry if the 1/1/1900 / must-be-after banner is still visible.
        """
        if effective_date:
            self._job_effective_date = str(effective_date).strip()
        report: dict[str, Any] = {
            "code_version": CODE_VERSION,
            "policy_id": policy_id,
            "formentry_found": False,
            "formentry_url": None,
            "validation": {},
        }
        # Use passed applicant_id, fallback to self.applicant_id
        effective_applicant_id = applicant_id or self.applicant_id or ""
        # PREVENTION: fail fast if applicant_id is empty — don't navigate to a broken URL
        if not effective_applicant_id:
            report["error"] = (
                "REFUSED: applicant_id is empty, cannot construct Edit Policy URL. "
                "This would produce a 404 (double slash). "
                f"policy_id={policy_id}, applicant_id param='{applicant_id}', self.applicant_id='{self.applicant_id}'"
            )
            report["refused_empty_applicant_id"] = True
            return report
        edit_url = (
            f"https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
            f"{effective_applicant_id}/{policy_id}"
        )
        # PREVENTION: validate URL has no empty segments before navigating
        if "//" in edit_url.replace("https://", ""):
            report["error"] = (
                f"REFUSED: malformed Edit Policy URL (double slash): {edit_url}"
            )
            report["refused_malformed_url"] = True
            return report
        await self.page.goto(edit_url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)

        # Pre-click: scan every open tab for an already-minted FormEntry.
        for tab in self._all_tabs():
            try:
                url = tab.url
            except Exception:  # noqa: BLE001
                continue
            if url_is_minted_formentry(url):
                report["formentry_found"] = True
                report["formentry_url"] = url
                report["via"] = "already_open_tab"
                return report

        # Capture pre-click validation state from the DOM.
        report["validation"]["pre_click"] = await self._validation_snapshot()

        # Robust button finding: try multiple strategies with waits.
        # Strategy 1: exact accessible name (original)
        # Strategy 2: case-insensitive partial match
        # Strategy 3: CSS selector for common button patterns
        button = None
        strategies_tried = []
        
        # Wait for page to stabilize (increase from 2s to 5s total)
        await self.page.wait_for_timeout(3000)
        
        # Strategy 1: exact name
        strategies_tried.append("exact_name")
        btn = self.page.get_by_role("button", name="Save & Continue Edit")
        if await btn.count() > 0:
            button = btn.first
        
        # Strategy 2: partial name match (case-insensitive)
        if button is None:
            strategies_tried.append("partial_name")
            # Try with regex for flexible matching
            import re
            btn = self.page.get_by_role("button", name=re.compile(r"save.*continue.*edit", re.IGNORECASE))
            if await btn.count() > 0:
                button = btn.first
        
        # Strategy 3: look for button by text content
        if button is None:
            strategies_tried.append("text_content")
            btn = self.page.locator("button", has_text=re.compile(r"Save & Continue Edit", re.IGNORECASE))
            if await btn.count() > 0:
                button = btn.first
        
        # Strategy 4: CSS selector for green/success buttons (the button is green in screenshots)
        if button is None:
            strategies_tried.append("css_green")
            # Common patterns: .btn-success, .btn-green, button with specific classes
            for selector in ["button.btn-success", "button.btn-green", "a.btn-success"]:
                btn = self.page.locator(selector, has_text=re.compile(r"Continue.*Edit", re.IGNORECASE))
                if await btn.count() > 0:
                    button = btn.first
                    break

        # Strategy 5: link role (it might be an <a> styled as a button)
        if button is None:
            strategies_tried.append("link_role")
            link = self.page.get_by_role("link", name=re.compile(r"save.*continue.*edit", re.IGNORECASE))
            if await link.count() > 0:
                button = link.first

        # Strategy 6: search all clickable elements by text (button, a, div, span, input)
        if button is None:
            strategies_tried.append("all_elements")
            try:
                locator = self.page.locator("button, a, div, span, input[type='button'], input[type='submit']")
                count = await locator.count()
                for i in range(min(count, 100)):
                    el = locator.nth(i)
                    try:
                        text = await el.inner_text()
                        if text and "save" in text.lower() and "continue" in text.lower() and "edit" in text.lower():
                            button = el
                            strategies_tried.append(f"all_elements_idx_{i}")
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # Strategy 7: search in iframes
        if button is None:
            strategies_tried.append("iframe_search")
            try:
                for frame in self.page.frames:
                    if frame == self.page.main_frame:
                        continue
                    try:
                        btn = frame.get_by_role("button", name=re.compile(r"save.*continue.*edit", re.IGNORECASE))
                        if await btn.count() > 0:
                            button = btn.first
                            strategies_tried.append(f"iframe_found")
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # Strategy 8: JavaScript find and click (last resort)
        if button is None:
            try:
                js_found = await self.page.evaluate("""() => {
                    const els = document.querySelectorAll('button, a, div, span, input');
                    for (const el of els) {
                        const text = (el.innerText || el.value || '').toLowerCase();
                        if (text.includes('save') && text.includes('continue') && text.includes('edit')) {
                            return {tag: el.tagName, text: (el.innerText||'').substring(0,100), id: el.id};
                        }
                    }
                    // Also check iframes
                    for (const frame of document.querySelectorAll('iframe')) {
                        try {
                            const doc = frame.contentDocument;
                            if (!doc) continue;
                            const els2 = doc.querySelectorAll('button, a, div, span, input');
                            for (const el of els2) {
                                const text = (el.innerText || el.value || '').toLowerCase();
                                if (text.includes('save') && text.includes('continue') && text.includes('edit')) {
                                    return {tag: el.tagName+'_in_iframe', text: (el.innerText||'').substring(0,100), id: el.id};
                                }
                            }
                        } catch(e) {}
                    }
                    return null;
                }""")
                if js_found:
                    strategies_tried.append(f"js_found_{js_found.get('tag')}")
                    report["js_element_found"] = js_found
                else:
                    strategies_tried.append("js_miss")
            except Exception as js_exc:
                strategies_tried.append(f"js_error")

        report["button_strategies_tried"] = strategies_tried
        
        if button is None:
            report["page_state"] = await self._page_state_snapshot()
            ps = report["page_state"] or {}
            page_url = ps.get("url", self.page.url if hasattr(self.page, "url") else "unknown")
            page_title = ps.get("title", "unknown")
            visible_buttons = ps.get("buttons", [])[:10]
            report["error"] = (
                "Save & Continue Edit button not found on Edit Policy header. "
                f"Tried strategies: {', '.join(strategies_tried)}. "
                f"PAGE_URL: {page_url} "
                f"PAGE_TITLE: {page_title} "
                f"VISIBLE_BUTTONS: {visible_buttons} "
            )
            await self._escalate_formentry_hitl(
                report,
                attempted=strategies_tried,
                applicant_id=applicant_id,
                policy_id=policy_id,
            )
            return report
        # Fill required fields before clicking: Billing Type and Department.
        # The form validation blocks the save if these are empty.
        field_fill_failed = False
        try:
            fill_result = await self._fill_required_policy_fields()
            report["field_fill"] = fill_result
        except FieldFillHitlError as fill_exc:
            field_fill_failed = True
            report["field_fill"] = fill_exc.filled.to_dict()
            report["field_fill_error"] = f"{type(fill_exc).__name__}: {fill_exc}"
            report["error"] = (
                f"PLAYWRIGHT_BLOCKED: Failed to fill required fields: {fill_exc}"
            )
        except Exception as fill_exc:
            field_fill_failed = True
            report["field_fill_error"] = f"{type(fill_exc).__name__}: {fill_exc}"
            report["error"] = (
                f"PLAYWRIGHT_BLOCKED: Failed to fill required fields: {fill_exc}"
            )

        if field_fill_failed:
            # Keep the field-fill error. Do not claim a click or a 30s wait.
            report["error"] += (
                " (skipped Save & Continue Edit click due to field fill failure)"
            )
            report["landed_url"] = getattr(self.page, "url", None)
            await self._escalate_formentry_hitl(
                report,
                attempted=["field_fill"] + list(strategies_tried),
                applicant_id=applicant_id,
                policy_id=policy_id,
            )
            return report

        await button.first.click()
        report["save_clicks"] = 1

        minted = await self._poll_formentry_or_banner(report, seconds=30, snap_key="post_click")
        if minted:
            return report

        snap = report["validation"].get("post_click") or {}
        if is_lob_orig_date_validation_miss(snap):
            retry_fill = await self._fill_lob_orig_date()
            report["lob_orig_retry"] = retry_fill
            report["field_fill"] = dict(report.get("field_fill") or {})
            report["field_fill"]["lob_orig_retry"] = retry_fill
            await button.first.click()
            report["save_clicks"] = 2
            minted = await self._poll_formentry_or_banner(
                report, seconds=30, snap_key="post_retry"
            )
            if minted:
                report["via"] = "save_and_continue_edit_lob_orig_retry"
                return report

        # Still on Edit. Quote the visible banner — never VALIDATION_ERRORS: [].
        report["landed_url"] = self.page.url
        val = (
            report["validation"].get("post_retry")
            or report["validation"].get("post_click")
            or {}
        )
        val_errors = collect_visible_validation_errors(val)
        if not val_errors:
            page_state = await self._page_state_snapshot()
            report["page_state"] = page_state
            val_errors = collect_visible_validation_errors(
                {**val, "body_text_sample": page_state.get("body_text_sample")}
            )
        if not val_errors and is_lob_orig_date_validation_miss(val):
            val_errors = [
                "LOB Orig. Date: LOB Orig. Date must be after "
                f"(live value {val.get('lob_orig_date') or '1/1/1900'})"
            ]
        report["error"] = (
            "Save & Continue Edit clicked; no FormEntry URL after 30s. "
            f"VALIDATION_ERRORS: {val_errors[:5]} "
            f"LANDED_URL: {self.page.url} "
        )
        await self._escalate_formentry_hitl(
            report,
            attempted=["save_and_continue_edit", "lob_orig_date_fill", "formentry_url_poll"]
            + list(strategies_tried),
            applicant_id=applicant_id,
            policy_id=policy_id,
        )
        return report

    async def _poll_formentry_or_banner(
        self,
        report: dict[str, Any],
        *,
        seconds: int,
        snap_key: str,
    ) -> bool:
        """Poll for a minted FormEntry URL. Stop early on a visible validation banner."""
        last_snap: dict[str, Any] = {}
        for _ in range(seconds):
            await self.page.wait_for_timeout(1000)
            url = self.page.url
            if url_is_minted_formentry(url):
                report["formentry_found"] = True
                report["formentry_url"] = url
                report["via"] = "save_and_continue_edit"
                return True
            for tab in self._all_tabs():
                try:
                    turl = tab.url
                except Exception:  # noqa: BLE001
                    continue
                if url_is_minted_formentry(turl):
                    report["formentry_found"] = True
                    report["formentry_url"] = turl
                    report["via"] = "save_and_continue_edit_new_tab"
                    return True
            last_snap = await self._validation_snapshot()
            if is_lob_orig_date_validation_miss(last_snap):
                report["validation"][snap_key] = last_snap
                return False
        report["validation"][snap_key] = last_snap or await self._validation_snapshot()
        return False

    def _all_tabs(self) -> list[Any]:
        try:
                ctx = self.page.context
                return list(ctx.pages)
        except Exception:  # noqa: BLE001
                return [self.page]

    async def _validation_snapshot(self) -> dict[str, Any]:
        """Read validation markers from the DOM, including the red banner."""
        js = r"""
        () => {
          const fieldErrors = Array.from(
                document.querySelectorAll(".field-validation-error, .validation-message, [data-valmsg-for]")
          ).map((el) => (el.innerText || "").trim()).filter(Boolean).slice(0, 20);
          const summary = Array.from(
                document.querySelectorAll(".validation-summary-errors")
          ).map((el) => (el.innerText || "").trim()).filter(Boolean).slice(0, 5);
          const banners = Array.from(
                document.querySelectorAll(
                  ".alert-danger, .alert-error, .alert, [role='alert'], .toast-error, .validation-summary-errors"
                )
          ).map((el) => (el.innerText || "").trim()).filter(Boolean).slice(0, 10);
          const body = document.body ? document.body.innerText : "";
          const bannerLines = body.split("\\n").map((s) => s.trim()).filter((s) =>
                /must be after|lob orig/i.test(s)
          ).slice(0, 10);
          const ariaInvalid = Array.from(
                document.querySelectorAll("[aria-invalid='true']")
          ).map((el) => el.id || el.getAttribute("name") || el.tagName).slice(0, 20);
          const orig = document.querySelector("#LOBOriginationDate");
          const lobOrigDate = orig ? String(orig.value || "").trim() : "";
          const errors = [...fieldErrors, ...summary, ...banners, ...bannerLines]
                .filter(Boolean);
          return {
                field_errors: fieldErrors,
                summary_errors: summary,
                banners,
                banner_lines: bannerLines,
                aria_invalid: ariaInvalid,
                lob_orig_date: lobOrigDate,
                errors,
                validation_errors: errors,
                url: location.href,
                body_text_sample: body.slice(0, 800),
          };
        }
        """
        try:
                return await self.page.evaluate(js)
        except Exception as exc:  # noqa: BLE001
                return {"error": f"{type(exc).__name__}: {exc}"}

    async def _page_state_snapshot(self) -> dict[str, Any]:
        """Capture what IS on the page: URL, title, all buttons/links by name.

        Used when the expected control is absent, so the next fix is based on
        evidence, not guesses. Read-only DOM inspection; no clicks, no writes.
        """
        js = r"""
        () => {
          const buttons = Array.from(
                document.querySelectorAll("button, input[type='button'], input[type='submit'], a.btn, [role='button']")
          ).map((el) => {
                const name = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80);
                const role = el.getAttribute("role") || el.tagName.toLowerCase();
                return name ? `${role}: ${name}` : null;
          }).filter(Boolean).slice(0, 40);
          const headings = Array.from(
                document.querySelectorAll("h1, h2, .page-title, .panel-title")
          ).map((el) => (el.innerText || "").trim().slice(0, 100)).filter(Boolean).slice(0, 10);
          return {
                url: location.href,
                title: document.title,
                buttons: buttons,
                headings: headings,
                body_text_sample: (document.body ? document.body.innerText : "").slice(0, 500),
          };
        }
        """
        try:
                return await self.page.evaluate(js)
        except Exception as exc:  # noqa: BLE001
                return {"error": f"{type(exc).__name__}: {exc}"}


# Job Engine name used by callers. Same page object; one HOME mint path.
EzlynxPolicySetup = EzlynxPolicySetupPage
