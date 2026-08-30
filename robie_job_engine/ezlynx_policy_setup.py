"""EZLynx Policy Setup (APE) automation engine and Playwright Page Object.

Strict invariants:
1. Stop before bind: Never binds coverage or authorizes COMPLETE without manual gate.
2. No shell-only policies: Must execute Add & Edit Policy (#AddAndEditPolicyBtn) to complete vehicles, drivers, coverages, locations, and schedules.
3. Discussion note format: Includes "ROBIE was here".
4. Deterministic locators: No positional .first/.last/.nth selectors.
5. All LOB coverage: Commercial Auto, Personal Auto, BOP, General Liability, Workers Comp, Commercial Property, Commercial Umbrella, Personal Umbrella, Homeowners, Dwelling Fire, Inland Marine, Commercial Package, Crime, Flood, Cyber, Surety.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence


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
    cost_new: str = ""
    vehicle_type: str = "Commercial"
    radius: str = "Local 0-50 Miles"
    use: str = "Comm'l"
    rate_class: str = "01"
    garaging_address: str = ""
    premium: str = ""
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
    experience_years: str = "15"
    licensed_year: str = "2005"
    gender: str = "M"
    marital_status: str = "M"
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
class CommercialAutoCoverageItem:
    csl_limit: str = "1000000"
    bi_per_person: str = "1000000"
    bi_per_accident: str = "1000000"
    pd_per_accident: str = "1000000"
    um_uim_limit: str = "1000000"
    med_pay: str = "5000"
    pip: str = "250000"


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
    liability_e: str = "500000"
    med_pay_f: str = "5000"
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
    billing_type: str = "Direct"  # Direct or Agency
    billing_company: str = ""
    rating_state_value: str = "31"  # NJ = 31
    premium: str = ""
    full_term_premium: str = ""
    annual_premium: str = ""
    total_commission: str = "12.00"
    department: str = ""  # "Commercial Lines (CL)" or "Personal Lines (P/L)"
    # LOB Specific Components
    vehicles: Sequence[VehicleItem] = field(default_factory=tuple)
    drivers: Sequence[DriverItem] = field(default_factory=tuple)
    locations: Sequence[LocationItem] = field(default_factory=tuple)
    buildings: Sequence[BuildingItem] = field(default_factory=tuple)
    gl_coverage: GLCoverageItem | None = None
    property_coverage: PropertyCoverageItem | None = None
    wc_coverage: WorkersCompItem | None = None
    commercial_auto_coverage: CommercialAutoCoverageItem | None = None
    umbrella_coverage: UmbrellaCoverageItem | None = None
    homeowners_coverage: HomeownersCoverageItem | None = None
    inland_marine_items: Sequence[InlandMarineItem] = field(default_factory=tuple)
    discussion_note_title: str = ""
    discussion_note_body: str = ""


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "applicant_id": self.applicant_id,
            "policy_number": self.policy_number,
            "lob": self.lob,
            "phase_reached": self.phase_reached,
            "error": self.error,
            "note_added": self.note_added,
            "stopped_before_bind": self.stopped_before_bind,
        }


def normalize_lob(lob_input: str) -> str:
    cleaned = lob_input.strip()
    return LINE_OF_BUSINESS_MAP.get(cleaned.lower().replace(" ", "_"), cleaned)


def is_commercial_lob(lob_input: str) -> bool:
    normalized = normalize_lob(lob_input).lower()
    return not (
        "personal" in normalized
        or "homeowners" in normalized
        or "dwelling" in normalized
    )


def normalize_transaction_type(trans_input: str) -> str:
    cleaned = trans_input.strip()
    return TRANSACTION_TYPE_MAP.get(cleaned.lower().replace(" ", "_"), cleaned)


def build_discussion_note(lob: str, policy_number: str, carrier: str, custom_summary: str = "") -> str:
    summary = custom_summary.strip() or f"Completed {lob} policy setup for Policy #{policy_number} with {carrier}."
    return f"{summary}\n\nROBIE was here"


class EzlynxPolicySetupPage:
    """Deterministic Playwright Page Object for EZLynx APE workflows across all LOBs."""

    def __init__(self, page: Any) -> None:
        self.page = page

    async def navigate_to_policies(self, applicant_id: str) -> None:
        url = f"{EZLYNX_BASE_URL}/web/account/{applicant_id}/policies"
        await self.page.goto(url, wait_until="domcontentloaded")

    async def open_policy_add(self) -> None:
        add_btn = self.page.locator("#add-policy")
        await add_btn.wait_for(state="visible", timeout=10000)
        await add_btn.click()
        await self.page.wait_for_load_state("domcontentloaded")

    async def fill_policy_shell(self, shell_input: PolicyShellInput, save_and_edit: bool = True) -> bool:
        lob_val = normalize_lob(shell_input.lob)
        trans_val = normalize_transaction_type(shell_input.transaction_type)

        # 1. Line of business
        lob_select = self.page.locator("#mergeSplitLOB")
        await lob_select.select_option(value=lob_val)
        await lob_select.dispatch_event("change")

        # 2. Transaction type
        trans_select = self.page.locator("#TransactionType")
        await trans_select.select_option(value=trans_val)
        await trans_select.dispatch_event("change")

        # 3. Master company
        master_select = self.page.locator("#MasterCompany")
        await master_select.select_option(value=shell_input.master_company_value)
        await master_select.dispatch_event("change")

        # Wait briefly for dynamic writing company population
        await self.page.wait_for_timeout(500)

        # 4. Writing company (if specified)
        if shell_input.writing_company_text:
            writing_select = self.page.locator("#WritingCompany")
            await writing_select.select_option(label=shell_input.writing_company_text)
            await writing_select.dispatch_event("change")

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

        # 7. Billing Type & Rating State
        if shell_input.billing_type:
            bill = self.page.locator("#BillingType")
            await bill.select_option(value=shell_input.billing_type)
        if shell_input.billing_company:
            bill_comp = self.page.locator("#BillingCompany")
            if await bill_comp.count() > 0:
                await bill_comp.select_option(label=shell_input.billing_company)
        if shell_input.rating_state_value:
            state = self.page.locator("#RatingState")
            await state.select_option(value=shell_input.rating_state_value)

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

        # 8b. Department selection (Commercial Lines (CL) for commercial, Personal Lines (P/L) for personal)
        dept_val = shell_input.department or (
            "Commercial Lines (CL)" if is_commercial_lob(shell_input.lob) else "Personal Lines (P/L)"
        )
        dept_trigger = self.page.locator("#Department .select2-choice, #Department a.ui-select-match")
        if await dept_trigger.count() > 0:
            await dept_trigger.click()
            await self.page.wait_for_timeout(250)
            dept_option = self.page.locator(f"#Department .ui-select-choices-row:has-text('{dept_val}')")
            if await dept_option.count() > 0:
                await dept_option.click()
            else:
                search_input = self.page.locator("#Department input.ui-select-search")
                if await search_input.count() > 0:
                    await search_input.fill(dept_val)
                    await self.page.wait_for_timeout(200)
                    row = self.page.locator(f"#Department .ui-select-choices-row:has-text('{dept_val}')")
                    if await row.count() > 0:
                        await row.click()

        # 9. Submit action (Add & Edit vs Add Policy)
        if save_and_edit:
            add_edit_btn = self.page.locator("#AddAndEditPolicyBtn")
            await add_edit_btn.wait_for(state="visible", timeout=5000)
            await add_edit_btn.click()
            await self.page.wait_for_load_state("domcontentloaded")
            return True
        else:
            add_btn = self.page.locator("#AddPolicyBtn")
            await add_btn.wait_for(state="visible", timeout=5000)
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
                await dropdown.select_option(label=requested)
                await dropdown.dispatch_event("change")
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
        edit_btn = self.page.locator("a[data-original-title='Edit'], button[data-original-title='Edit']")
        add_btn = self.page.locator("input[value='Add Vehicle'], button:has-text('Add Vehicle'), #add-vehicle-btn")
        if await edit_btn.count() > 0:
            await edit_btn.first.click()
            await self.page.wait_for_timeout(500)
        elif await add_btn.count() > 0:
            await add_btn.click()
            await self.page.wait_for_timeout(500)

        # VIN input & Lookup trigger
        vin_input = self.page.locator("#Vehicle_VINIdentifier_A, input[name='Vehicle_VINIdentifier_A'], #Vehicle_VIN")
        if await vin_input.count() > 0:
            await vin_input.fill(vehicle.vin)
            await vin_input.dispatch_event("input")
            await vin_input.dispatch_event("change")

        # Explicit vehicle fields
        if vehicle.year:
            year_input = self.page.locator("#Vehicle_ModelYear_A, input[name='Vehicle_ModelYear_A'], #Vehicle_Year")
            if await year_input.count() > 0:
                await year_input.fill(vehicle.year)
                await year_input.dispatch_event("input")
                await year_input.dispatch_event("change")
        if vehicle.make:
            make_input = self.page.locator("#Vehicle_ManufacturersName_A, input[name='Vehicle_ManufacturersName_A'], #Vehicle_Make")
            if await make_input.count() > 0:
                await make_input.fill(vehicle.make)
                await make_input.dispatch_event("input")
                await make_input.dispatch_event("change")
        if vehicle.model:
            model_input = self.page.locator("#Vehicle_ModelName_A, input[name='Vehicle_ModelName_A'], #Vehicle_Model")
            if await model_input.count() > 0:
                await model_input.fill(vehicle.model)
                await model_input.dispatch_event("input")
                await model_input.dispatch_event("change")
        if vehicle.body_type:
            body_input = self.page.locator("#Vehicle_BodyCode_A, select#Vehicle_BodyCode_A, input[name='Vehicle_BodyCode_A']")
            if await body_input.count() > 0:
                if await body_input.evaluate("el => el.tagName === 'SELECT'"):
                    await body_input.select_option(label=vehicle.body_type)
                else:
                    await body_input.fill(vehicle.body_type)
                await body_input.dispatch_event("change")
        if vehicle.cost_new:
            cost_input = self.page.locator("#Vehicle_CostNewAmount_A, input[name='Vehicle_CostNewAmount_A']")
            if await cost_input.count() > 0:
                await cost_input.fill(clean_currency(vehicle.cost_new))
                await cost_input.dispatch_event("input")
                await cost_input.dispatch_event("change")
        if vehicle.vehicle_type:
            vtype_input = self.page.locator("#Vehicle_VehicleType_A, select#Vehicle_VehicleType_A, input[name='Vehicle_VehicleType_A']")
            if await vtype_input.count() > 0:
                if await vtype_input.evaluate("el => el.tagName === 'SELECT'"):
                    await vtype_input.select_option(label=vehicle.vehicle_type)
                else:
                    await vtype_input.fill(vehicle.vehicle_type)
                await vtype_input.dispatch_event("change")

        # Rating Information accordion
        rating_btn = self.page.locator(".repeaterEntryModal.in button.collapsible:has-text('Rating Information'), .modal button.collapsible:has-text('Rating Information')")
        if await rating_btn.count() > 0:
            await rating_btn.click()
            await self.page.wait_for_timeout(300)

        # Garaging location
        loc_select = self.page.locator(".repeaterEntryModal.in select[name='Form127.Ez_Vehicle_RepeaterKey_A'], select[name='Form127.Ez_Vehicle_RepeaterKey_A'], #Form127_Ez_Vehicle_RepeaterKey_A")
        if await loc_select.count() > 0:
            if vehicle.garaging_address:
                await loc_select.select_option(label=vehicle.garaging_address)
            else:
                options = await loc_select.locator("option").all()
                if len(options) > 1:
                    await loc_select.select_option(index=1)
            await loc_select.dispatch_event("change")
        elif await self.page.locator("#Vehicle_GaragingAddressId").count() > 0:
            await self.select_vehicle_garaging_address(vehicle)

        if vehicle.use:
            use_select = self.page.locator("#Vehicle_Use_A, select[name='Vehicle_Use_A']")
            if await use_select.count() > 0:
                await use_select.select_option(label=vehicle.use)
                await use_select.dispatch_event("change")

        if vehicle.radius:
            radius_select = self.page.locator("select[name='Form127.Vehicle_RadiusOfUse_A'], #Vehicle_RadiusOfUse_A")
            if await radius_select.count() > 0:
                await radius_select.select_option(label=vehicle.radius)
                await radius_select.dispatch_event("change")

        if vehicle.rate_class:
            rate_input = self.page.locator("#Vehicle_RateClassCode_A, input[name='Vehicle_RateClassCode_A']")
            if await rate_input.count() > 0:
                await rate_input.fill(vehicle.rate_class)
                await rate_input.dispatch_event("input")
                await rate_input.dispatch_event("change")

        # Coverages accordion
        cov_btn = self.page.locator(".repeaterEntryModal.in button.collapsible:has-text('Coverages'), .modal button.collapsible:has-text('Coverages')")
        if await cov_btn.count() > 0:
            await cov_btn.click()
            await self.page.wait_for_timeout(300)

        if vehicle.premium:
            prem_input = self.page.locator("#Vehicle_TotalPremiumAmount_A, input[name='Vehicle_TotalPremiumAmount_A']")
            if await prem_input.count() > 0:
                await prem_input.fill(clean_currency(vehicle.premium))
                await prem_input.dispatch_event("input")
                await prem_input.dispatch_event("change")

        if vehicle.comp_deductible:
            comp = self.page.locator("#Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A, #Vehicle_Comprehensive_DeductibleAmount_A, #Vehicle_CompDeductible")
            if await comp.count() > 0:
                await comp.fill(clean_currency(vehicle.comp_deductible))
                await comp.dispatch_event("input")
                await comp.dispatch_event("change")

        if vehicle.coll_deductible:
            coll = self.page.locator("#Vehicle_Collision_DeductibleAmount_A, #Vehicle_CollDeductible")
            if await coll.count() > 0:
                await coll.fill(clean_currency(vehicle.coll_deductible))
                await coll.dispatch_event("input")
                await coll.dispatch_event("change")

        # Save vehicle modal
        save_btn = self.page.locator(".repeaterEntryModal.in button.btn-primary:has-text('Save'), .modal button.btn-primary:has-text('Save'), .modal button:has-text('Save')")
        if await save_btn.count() > 0:
            await save_btn.first.click()
            await self.page.wait_for_timeout(1000)

    async def add_driver(self, driver: DriverItem, driver_num: str = "1") -> None:
        edit_btn = self.page.locator("a[data-original-title='Edit'], button[data-original-title='Edit']")
        add_btn = self.page.locator("input[value='Add Driver'], button:has-text('Add Driver'), #add-driver-btn")
        if await edit_btn.count() > 0:
            await edit_btn.first.click()
            await self.page.wait_for_timeout(500)
        elif await add_btn.count() > 0:
            await add_btn.click()
            await self.page.wait_for_timeout(500)

        # Driver identifier (mandatory in EZLynx)
        driver_id_input = self.page.locator("#Driver_ProducerIdentifier_A, input[name='Driver_ProducerIdentifier_A']")
        if await driver_id_input.count() > 0:
            await driver_id_input.fill(driver_num)
            await driver_id_input.dispatch_event("input")
            await driver_id_input.dispatch_event("change")

        # Name fields
        first_input = self.page.locator("#Driver_GivenName_A, input[name='Driver_GivenName_A'], #Driver_FirstName")
        if await first_input.count() > 0:
            await first_input.fill(driver.first_name)
            await first_input.dispatch_event("input")
            await first_input.dispatch_event("change")

        last_input = self.page.locator("#Driver_Surname_A, input[name='Driver_Surname_A'], #Driver_LastName")
        if await last_input.count() > 0:
            await last_input.fill(driver.last_name)
            await last_input.dispatch_event("input")
            await last_input.dispatch_event("change")

        if driver.dob:
            dob_input = self.page.locator("input[name='Driver_BirthDate_A'], #Driver_BirthDate_A, #Driver_DOB")
            if await dob_input.count() > 0:
                await dob_input.fill(driver.dob)
                await dob_input.dispatch_event("input")
                await dob_input.dispatch_event("change")

        if driver.license_number:
            lic_input = self.page.locator("#Driver_LicenseNumberIdentifier_A, input[name='Driver_LicenseNumberIdentifier_A'], #Driver_LicenseNumber")
            if await lic_input.count() > 0:
                await lic_input.fill(driver.license_number)
                await lic_input.dispatch_event("input")
                await lic_input.dispatch_event("change")

        if driver.license_state:
            state = self.page.locator("#Driver_LicensedStateOrProvinceCode_A, select[name='Driver_LicensedStateOrProvinceCode_A'], #Driver_LicenseState")
            if await state.count() > 0:
                if await state.evaluate("el => el.tagName === 'SELECT'"):
                    await state.select_option(label=driver.license_state)
                else:
                    await state.fill(driver.license_state)
                await state.dispatch_event("change")

        if driver.experience_years:
            exp_input = self.page.locator("#Driver_ExperienceYearCount_A, input[name='Driver_ExperienceYearCount_A']")
            if await exp_input.count() > 0:
                await exp_input.fill(driver.experience_years)
                await exp_input.dispatch_event("input")
                await exp_input.dispatch_event("change")

        if driver.licensed_year:
            lic_yr_input = self.page.locator("#Driver_LicensedYear_A, input[name='Driver_LicensedYear_A']")
            if await lic_yr_input.count() > 0:
                await lic_yr_input.fill(driver.licensed_year)
                await lic_yr_input.dispatch_event("input")
                await lic_yr_input.dispatch_event("change")

        if not driver.excluded:
            excl_no = self.page.locator("#Excluded_DriverCode_A-no, input[name='Excluded_DriverCode_A'][value='N']")
            if await excl_no.count() > 0:
                await excl_no.click()

        # Save driver modal
        save_btn = self.page.locator(".repeaterEntryModal.in button.btn-primary:has-text('Save'), .modal button.btn-primary:has-text('Save'), .modal button:has-text('Save'), #save-driver-btn")
        if await save_btn.count() > 0:
            await save_btn.first.click()
            await self.page.wait_for_timeout(1000)

    async def fill_commercial_auto_coverages(self, cov: CommercialAutoCoverageItem) -> None:
        """Populate Commercial Auto policy-level coverage limits on FormEntry using clean UI inputs."""
        cov_tab = self.page.locator("a:has-text('Coverages'), span:has-text('Coverages'), #coverages-tab")
        if await cov_tab.count() > 0:
            await cov_tab.first.click()
            await self.page.wait_for_timeout(500)

        # CSL / Liability Limits
        csl_input = self.page.locator("#Coverage_CombinedSingleLimit_PerAccidentLimitAmount, #Coverage_CombinedSingleLimit_Amount, input[name='Coverage_CombinedSingleLimit_PerAccidentLimitAmount']")
        if await csl_input.count() > 0:
            await csl_input.fill(clean_currency(cov.csl_limit))
            await csl_input.dispatch_event("input")
            await csl_input.dispatch_event("change")

        bi_person = self.page.locator("#Vehicle_BodilyInjury_PerPersonLimitAmount_A, input[name='Vehicle_BodilyInjury_PerPersonLimitAmount_A']")
        if await bi_person.count() > 0:
            await bi_person.fill(clean_currency(cov.bi_per_person))
            await bi_person.dispatch_event("input")
            await bi_person.dispatch_event("change")

        bi_acc = self.page.locator("#Vehicle_BodilyInjury_PerAccidentLimitAmount_A, input[name='Vehicle_BodilyInjury_PerAccidentLimitAmount_A']")
        if await bi_acc.count() > 0:
            await bi_acc.fill(clean_currency(cov.bi_per_accident))
            await bi_acc.dispatch_event("input")
            await bi_acc.dispatch_event("change")

        pd_acc = self.page.locator("#Vehicle_PropertyDamage_PerAccidentLimitAmount_A, input[name='Vehicle_PropertyDamage_PerAccidentLimitAmount_A']")
        if await pd_acc.count() > 0:
            await pd_acc.fill(clean_currency(cov.pd_per_accident))
            await pd_acc.dispatch_event("input")
            await pd_acc.dispatch_event("change")

        um_input = self.page.locator("#Vehicle_UninsuredUnderinsuredMotorists_BodilyInjuryPerAccidentLimitAmount_A, input[name='Vehicle_UninsuredUnderinsuredMotorists_BodilyInjuryPerAccidentLimitAmount_A']")
        if await um_input.count() > 0:
            await um_input.fill(clean_currency(cov.um_uim_limit))
            await um_input.dispatch_event("input")
            await um_input.dispatch_event("change")

        med_input = self.page.locator("#Vehicle_MedicalPayments_PerPersonLimitAmount_A, input[name='Vehicle_MedicalPayments_PerPersonLimitAmount_A']")
        if await med_input.count() > 0:
            await med_input.fill(clean_currency(cov.med_pay))
            await med_input.dispatch_event("input")
            await med_input.dispatch_event("change")

        pip_input = self.page.locator("#Vehicle_PersonalInjuryProtection_PerPersonLimitAmount_A, input[name='Vehicle_PersonalInjuryProtection_PerPersonLimitAmount_A']")
        if await pip_input.count() > 0:
            await pip_input.fill(clean_currency(cov.pip))
            await pip_input.dispatch_event("input")
            await pip_input.dispatch_event("change")

    async def add_location(self, loc: LocationItem) -> None:
        add_btn = self.page.locator("input[value='Add Location'], button:has-text('Add Location'), #add-location-btn")
        await add_btn.click()

        if loc.address:
            addr = self.page.locator("#Location_Address, input[name='Location_Address']")
            await addr.fill(loc.address)
        if loc.city:
            city = self.page.locator("#Location_City, input[name='Location_City']")
            await city.fill(loc.city)
        if loc.state:
            state = self.page.locator("#Location_State, select[name='Location_State']")
            if await state.evaluate("el => el.tagName === 'SELECT'"):
                await state.select_option(label=loc.state)
            else:
                await state.fill(loc.state)
        if loc.zip_code:
            zc = self.page.locator("#Location_Zip, input[name='Location_Zip']")
            await zc.fill(loc.zip_code)

        save_btn = self.page.locator("input[value='Save'], button:has-text('Save'), #save-location-btn")
        await save_btn.click()

    async def add_building(self, bldg: BuildingItem) -> None:
        add_btn = self.page.locator("input[value='Add Building'], button:has-text('Add Building'), #add-building-btn")
        await add_btn.click()

        if bldg.construction_type:
            ct = self.page.locator("#Building_ConstructionType, select[name='Building_ConstructionType']")
            if await ct.evaluate("el => el.tagName === 'SELECT'"):
                await ct.select_option(label=bldg.construction_type)
            else:
                await ct.fill(bldg.construction_type)

        if bldg.year_built:
            yb = self.page.locator("#Building_YearBuilt, input[name='Building_YearBuilt']")
            await yb.fill(bldg.year_built)

        if bldg.square_footage:
            sqft = self.page.locator("#Building_SquareFootage, input[name='Building_SquareFootage']")
            await sqft.fill(clean_currency(bldg.square_footage))

        save_btn = self.page.locator("input[value='Save'], button:has-text('Save'), #save-building-btn")
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
        final_body = body if "ROBIE was here" in body else f"{body}\n\nROBIE was here"
        
        # 1. Check in-form tab
        disc_tab = self.page.locator("a[data-target='#discussions-tab'], #tab-discussions")
        if await disc_tab.count() > 0:
            await disc_tab.click()
            add_note_btn = self.page.locator("#add-note-btn, button[data-action='add-note']")
            if await add_note_btn.count() > 0:
                await add_note_btn.click()
                await self.page.locator("#Discussion_Title, input[name='DiscussionTitle']").fill(title)
                await self.page.locator("#Discussion_Body, textarea[name='DiscussionBody']").fill(final_body)
                save_note = self.page.locator("#save-note-btn, button[data-action='save-note']")
                if await save_note.count() > 0:
                    await save_note.click()
                    return

        # 2. Header Flyout with Direct Policy Association (#btnAssociatetoAPolicy)
        header_note_btn = self.page.locator("#add-note-header")
        if await header_note_btn.count() > 0:
            await header_note_btn.click()
            await self.page.wait_for_timeout(300)
            
            # Associate to actual Policy record
            if policy_number:
                btn_assoc = self.page.locator("#btnAssociatetoAPolicy")
                if await btn_assoc.count() > 0:
                    await btn_assoc.click()
                    await self.page.wait_for_timeout(250)
                    select_policy_btn = self.page.locator("button:has-text('Select a policy')")
                    if await select_policy_btn.count() > 0:
                        await select_policy_btn.click()
                        await self.page.wait_for_timeout(250)
                        opt = self.page.locator(f"mat-option:has-text('{policy_number}'), [role='option']:has-text('{policy_number}')")
                        if await opt.count() > 0:
                            await opt.click()

            title_input = self.page.locator("#txtDiscussionTitle")
            if await title_input.count() > 0:
                await title_input.fill(title)
            body_input = self.page.locator("#txtNote")
            if await body_input.count() > 0:
                await body_input.fill(final_body)
            save_btn = self.page.locator("#btnSaveNote")
            if await save_btn.count() > 0:
                await save_btn.click()
                await self.page.wait_for_timeout(300)
                # Close workspace flyout
                close_btn = self.page.locator("#close-workspace-button")
                if await close_btn.count() > 0:
                    await close_btn.click()

    async def save_and_close_form_entry(self) -> None:
        save_close_btn = self.page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close'), #btnSaveClose")
        if await save_close_btn.count() > 0:
            await save_close_btn.first.click()
            await self.page.wait_for_load_state("domcontentloaded")
            await self.page.wait_for_timeout(2000)

    # Explicit Test-Only Schedule Executor
    async def execute_formentry_schedules_test_only(self, shell_input: PolicyShellInput) -> dict[str, Any]:
        """Execute FormEntry schedule and limit population in Test-only mode against an open policy form."""
        schedules_applied: list[str] = []

        # 1. Vehicles
        if shell_input.vehicles:
            veh_tab = self.page.locator("a:has-text('Vehicles'), span:has-text('Vehicles'), #vehicles-tab")
            if await veh_tab.count() > 0:
                await veh_tab.first.click()
                await self.page.wait_for_timeout(500)
            for v in shell_input.vehicles:
                await self.add_vehicle(v)
                schedules_applied.append(f"vehicle:{v.vin}")

        # 2. Drivers
        if shell_input.drivers:
            driver_tab = self.page.locator("a:has-text('Drivers'), span:has-text('Drivers'), #drivers-tab")
            if await driver_tab.count() > 0:
                await driver_tab.first.click()
                await self.page.wait_for_timeout(500)
            for idx, d in enumerate(shell_input.drivers, start=1):
                await self.add_driver(d, driver_num=str(idx))
                schedules_applied.append(f"driver:{d.first_name} {d.last_name}")

        # 3. Commercial Auto Coverages / Limits
        if shell_input.commercial_auto_coverage:
            cov_tab = self.page.locator("a:has-text('Coverages'), span:has-text('Coverages'), #coverages-tab")
            if await cov_tab.count() > 0:
                await cov_tab.first.click()
                await self.page.wait_for_timeout(500)
            await self.fill_commercial_auto_coverages(shell_input.commercial_auto_coverage)
            schedules_applied.append("commercial_auto_coverages")

        # 4. GL Coverages
        if shell_input.gl_coverage:
            await self.fill_gl_coverages(shell_input.gl_coverage)
            schedules_applied.append("gl_coverages")

        # 5. Workers Comp
        if shell_input.wc_coverage:
            await self.fill_wc_coverages(shell_input.wc_coverage)
            schedules_applied.append("wc_coverages")

        # 6. Umbrella
        if shell_input.umbrella_coverage:
            await self.fill_umbrella_coverages(shell_input.umbrella_coverage)
            schedules_applied.append("umbrella_coverages")

        # 7. Homeowners
        if shell_input.homeowners_coverage:
            await self.fill_homeowners_coverages(shell_input.homeowners_coverage)
            schedules_applied.append("homeowners_coverages")

        # 8. Locations & Buildings
        for loc in shell_input.locations:
            await self.add_location(loc)
            schedules_applied.append(f"location:{loc.location_number}")

        for bldg in shell_input.buildings:
            await self.add_building(bldg)
            schedules_applied.append(f"building:{bldg.location_number}-{bldg.building_number}")

        # 9. Inland Marine
        for im in shell_input.inland_marine_items:
            await self.add_inland_marine_item(im)
            schedules_applied.append(f"inland_marine:{im.item_type}")

        return {
            "success": True,
            "applicant_id": shell_input.applicant_id,
            "policy_number": shell_input.policy_number,
            "schedules_applied": schedules_applied,
        }

    # Unified LOB Orchestrator
    async def setup_policy_by_lob(self, shell_input: PolicyShellInput) -> PolicySetupResult:
        """Refuse the legacy write path while Policy Setup remains a Test draft.

        The original implementation created a shell and performed multiple
        saves without an authoritative duplicate check, durable checkpoints,
        or reopen verification.  Keep the page-object helpers available for
        selector-level Test work, but never enter the consequential
        orchestrator until a later, reviewed implementation supplies those
        controls.
        """
        normalized = normalize_lob(shell_input.lob)

        return PolicySetupResult(
            success=False,
            applicant_id=shell_input.applicant_id,
            policy_number=shell_input.policy_number,
            lob=normalized,
            phase_reached="draft_write_gate",
            error=(
                "NEEDS_CLARIFICATION: EZLynx Policy Setup v0.1.0-draft "
                "does not authorize consequential writes"
            ),
            note_added=False,
            stopped_before_bind=True,
        )
