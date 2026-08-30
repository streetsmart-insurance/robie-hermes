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
    department: str = ""  # "Commercial Lines (CL)" or "Personal Lines (P/L)"
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
        save_close_btn = self.page.locator("#finishButton-header")
        await save_close_btn.wait_for(state="visible", timeout=10000)
        await save_close_btn.click()
        await self.page.wait_for_load_state("domcontentloaded")

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
