"""Comprehensive unit, contract, and Playwright workflow tests for all EZLynx Lines of Business."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from robie_job_engine.ezlynx_policy_setup import (
    BuildingItem,
    CommercialAutoCoverageItem,
    DriverItem,
    EzlynxPolicySetupPage,
    GLCoverageItem,
    HomeownersCoverageItem,
    InlandMarineItem,
    LocationItem,
    PolicySetupResult,
    PolicyShellInput,
    PropertyCoverageItem,
    UnderlyingPolicyItem,
    UmbrellaCoverageItem,
    VehicleItem,
    WorkersCompItem,
    build_discussion_note,
    clean_currency,
    normalize_lob,
    normalize_transaction_type,
    LINE_OF_BUSINESS_MAP,
    TRANSACTION_TYPE_MAP,
)
from robie_job_engine.locator_registry import LocatorRegistry


def test_clean_currency():
    assert clean_currency("$22,080.00") == "22080.00"
    assert clean_currency("$1,000,000") == "1000000"
    assert clean_currency(" 500 ") == "500"
    assert clean_currency(None) == ""
    assert clean_currency(1234.5) == "1234.5"


def test_normalize_lob_all_lines():
    for key, expected in LINE_OF_BUSINESS_MAP.items():
        assert normalize_lob(key) == expected
        assert normalize_lob(expected) == expected


def test_normalize_transaction_types():
    for key, expected in TRANSACTION_TYPE_MAP.items():
        assert normalize_transaction_type(key) == expected
        assert normalize_transaction_type(expected) == expected


def test_build_discussion_note():
    note = build_discussion_note("Commercial Auto", "CA-99281-26", "Progressive")
    assert "ROBIE was here" in note
    assert "Commercial Auto" in note
    assert "CA-99281-26" in note
    assert "Progressive" in note


def test_locator_registry_loads_all_lob_locators():
    registry = LocatorRegistry()
    page_locators = registry.get_page("ezlynx", "policy_setup")
    assert page_locators is not None, "ezlynx:policy_setup must be loaded by LocatorRegistry"

    # Shell Locators
    assert page_locators.get_field("add_policy_button") is not None
    assert page_locators.get_field("lob_select") is not None
    assert page_locators.get_field("lob_origination_date_input") is not None
    assert page_locators.get_field("add_and_edit_policy_button") is not None
    assert page_locators.get_field("form_entry_save_close") is not None

    # Auto Locators
    assert page_locators.get_field("tab_vehicles") is not None
    assert page_locators.get_field("add_vehicle_button") is not None
    assert page_locators.get_field("vehicle_vin_input") is not None
    assert page_locators.get_field("tab_drivers") is not None
    assert page_locators.get_field("add_driver_button") is not None

    # GL & BOP Locators
    assert page_locators.get_field("tab_locations") is not None
    assert page_locators.get_field("add_location_button") is not None
    assert page_locators.get_field("add_building_button") is not None
    assert page_locators.get_field("tab_coverages") is not None
    assert page_locators.get_field("gl_occurrence_limit_select") is not None
    assert page_locators.get_field("gl_class_code_input") is not None

    # Workers Comp Locators
    assert page_locators.get_field("tab_workers_comp") is not None
    assert page_locators.get_field("add_wc_class_code_button") is not None
    assert page_locators.get_field("wc_payroll_input") is not None

    # Umbrella Locators
    assert page_locators.get_field("tab_umbrella_underlying") is not None
    assert page_locators.get_field("add_underlying_policy_button") is not None

    # Homeowners Locators
    assert page_locators.get_field("tab_property_homeowners") is not None
    assert page_locators.get_field("ho_dwelling_limit_a_input") is not None

    # Inland Marine Locators
    assert page_locators.get_field("tab_inland_marine") is not None
    assert page_locators.get_field("add_im_item_button") is not None

    # Discussion Notes Locators
    assert page_locators.get_field("tab_discussion_notes") is not None
    assert page_locators.get_field("add_discussion_note_button") is not None


# LOB Spec Test Cases

def test_commercial_auto_spec():
    vehicle = VehicleItem(
        vin="TEST-VIN-001",
        year="2022",
        make="Ford",
        model="F-250",
        garaging_address="SANITIZED GARAGING ADDRESS",
        comp_deductible="1000",
        coll_deductible="1000",
        towing=True,
    )
    driver = DriverItem(
        first_name="SYNTHETIC-FIRST",
        last_name="SYNTHETIC-LAST",
        dob="SYNTHETIC-DOB",
        license_number="TEST-LICENSE-001",
        license_state="NJ",
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="commercial_auto",
        transaction_type="new_business",
        master_company_value="155",
        writing_company_text="TEST CARRIER",
        policy_number="TEST-POLICY-CA-001",
        effective_date="09/01/2026",
        expiration_date="09/01/2027",
        lob_origination_date="09/01/2026",
        premium="22080.00",
        vehicles=(vehicle,),
        drivers=(driver,),
    )
    assert shell_input.lob == "commercial_auto"
    assert len(shell_input.vehicles) == 1
    assert shell_input.vehicles[0].vin == "TEST-VIN-001"
    assert len(shell_input.drivers) == 1


def test_bop_and_commercial_property_spec():
    loc = LocationItem(
        location_number="1",
        address="SANITIZED PROPERTY ADDRESS",
        city="SANITIZED CITY",
        state="NJ",
        zip_code="00000",
    )
    bldg = BuildingItem(
        location_number="1",
        building_number="1",
        construction_type="Joisted Masonry",
        protection_class="3",
        year_built="2010",
        square_footage="5000",
        building_limit="1500000",
        bpp_limit="250000",
        causes_of_loss="Special",
        deductible="1000",
    )
    gl = GLCoverageItem(
        occurrence_limit="1000000",
        general_aggregate="2000000",
        products_aggregate="2000000",
        class_code="54321",
        exposure="500000",
        blanket_ai=True,
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="bop",
        transaction_type="new_business",
        master_company_value="10",
        policy_number="TEST-POLICY-BOP-001",
        effective_date="10/01/2026",
        expiration_date="10/01/2027",
        premium="4500.00",
        locations=(loc,),
        buildings=(bldg,),
        gl_coverage=gl,
    )
    assert shell_input.lob == "bop"
    assert len(shell_input.locations) == 1
    assert len(shell_input.buildings) == 1
    assert shell_input.buildings[0].building_limit == "1500000"
    assert shell_input.gl_coverage.occurrence_limit == "1000000"


def test_workers_comp_spec():
    wc = WorkersCompItem(
        covered_states=("NJ", "NY"),
        class_code="8810",
        class_description="Clerical Office Employees",
        estimated_annual_payroll="125000",
        full_time_employees="3",
        officers_included=True,
        el_each_accident="1000000",
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="workers_comp",
        transaction_type="new_business",
        master_company_value="55",
        policy_number="TEST-POLICY-WC-001",
        effective_date="10/01/2026",
        expiration_date="10/01/2027",
        premium="1850.00",
        wc_coverage=wc,
    )
    assert shell_input.lob == "workers_comp"
    assert shell_input.wc_coverage.class_code == "8810"
    assert shell_input.wc_coverage.estimated_annual_payroll == "125000"


def test_umbrella_spec():
    auto_under = UnderlyingPolicyItem(
        lob="Auto",
        carrier="Progressive",
        policy_number="TEST-POLICY-CA-001",
        limits="1,000,000 CSL",
    )
    gl_under = UnderlyingPolicyItem(
        lob="General Liability",
        carrier="Travelers",
        policy_number="TEST-POLICY-BOP-001",
        limits="1,000,000 / 2,000,000",
    )
    umb = UmbrellaCoverageItem(
        occurrence_limit="5000000",
        aggregate_limit="5000000",
        retained_limit="10000",
        underlying_policies=(auto_under, gl_under),
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="commercial_umbrella",
        policy_number="TEST-POLICY-UMB-001",
        effective_date="10/01/2026",
        expiration_date="10/01/2027",
        premium="3200.00",
        umbrella_coverage=umb,
    )
    assert shell_input.lob == "commercial_umbrella"
    assert len(shell_input.umbrella_coverage.underlying_policies) == 2


def test_homeowners_spec():
    ho = HomeownersCoverageItem(
        dwelling_a="650000",
        other_structures_b="65000",
        personal_property_c="325000",
        loss_of_use_d="130000",
        liability_e="500000",
        med_pay_f="5000",
        all_peril_deductible="1000",
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="homeowners",
        policy_number="TEST-POLICY-HO-001",
        effective_date="11/01/2026",
        expiration_date="11/01/2027",
        premium="1950.00",
        homeowners_coverage=ho,
    )
    assert shell_input.lob == "homeowners"
    assert shell_input.homeowners_coverage.dwelling_a == "650000"


def test_inland_marine_spec():
    im = InlandMarineItem(
        item_description="2023 Caterpillar 308 CR Mini Excavator",
        serial_number="TEST-SERIAL-001",
        limit="95000",
        deductible="1000",
    )
    shell_input = PolicyShellInput(
        applicant_id="SANITIZED-APPLICANT-001",
        lob="inland_marine",
        policy_number="TEST-POLICY-IM-001",
        effective_date="09/01/2026",
        expiration_date="09/01/2027",
        premium="1200.00",
        inland_marine_items=(im,),
    )
    assert shell_input.lob == "inland_marine"
    assert len(shell_input.inland_marine_items) == 1
    assert shell_input.inland_marine_items[0].serial_number == "TEST-SERIAL-001"


def test_playwright_page_object_mock_orchestrator():
    """The legacy multi-save orchestrator must fail before its first write."""
    async def _run():
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.select_option = AsyncMock()
        mock_locator.dispatch_event = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator.is_enabled = AsyncMock(return_value=True)
        mock_locator.input_value = AsyncMock(return_value="existing-location")

        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.goto = AsyncMock()

        page_obj = EzlynxPolicySetupPage(mock_page)

        # Test Commercial Auto Run
        vehicle = VehicleItem(vin="TEST-VIN-001")
        driver = DriverItem(first_name="SYNTHETIC-FIRST", last_name="SYNTHETIC-LAST")
        shell_input = PolicyShellInput(
            applicant_id="220250093",
            lob="commercial_auto",
            policy_number="TEST-POLICY-CA-001",
            effective_date="09/01/2026",
            expiration_date="09/01/2027",
            premium="22080.00",
            vehicles=(vehicle,),
            drivers=(driver,),
        )
        result = await page_obj.setup_policy_by_lob(shell_input)
        assert result.success is False
        assert result.lob == "Auto (Commercial)"
        assert result.phase_reached == "draft_write_gate"
        assert result.stopped_before_bind is True
        assert result.note_added is False
        assert result.error.startswith("NEEDS_CLARIFICATION:")
        mock_page.locator.assert_not_called()

    asyncio.run(_run())


def test_vehicle_garaging_uses_location_dropdown_not_hidden_raw_input():
    async def _run():
        dropdown = MagicMock()
        dropdown.count = AsyncMock(return_value=1)
        dropdown.is_visible = AsyncMock(return_value=True)
        dropdown.is_enabled = AsyncMock(return_value=True)
        dropdown.select_option = AsyncMock()
        dropdown.dispatch_event = AsyncMock()
        raw = MagicMock()
        raw.fill = AsyncMock()
        page = MagicMock()
        page.locator = MagicMock(
            side_effect=lambda selector: dropdown
            if selector == "#Vehicle_GaragingAddressId"
            else raw
        )

        await EzlynxPolicySetupPage(page).select_vehicle_garaging_address(
            VehicleItem(vin="TESTVIN", garaging_address="123 Industrial Pkwy")
        )

        dropdown.select_option.assert_awaited_once_with(label="123 Industrial Pkwy")
        dropdown.dispatch_event.assert_awaited_once_with("change")
        raw.fill.assert_not_awaited()

    asyncio.run(_run())


def test_vehicle_garaging_fails_closed_when_dropdown_mode_is_disabled():
    async def _run():
        dropdown = MagicMock()
        dropdown.count = AsyncMock(return_value=1)
        dropdown.is_visible = AsyncMock(return_value=False)
        dropdown.is_enabled = AsyncMock(return_value=False)
        page = MagicMock()
        page.locator = MagicMock(return_value=dropdown)

        try:
            await EzlynxPolicySetupPage(page).select_vehicle_garaging_address(
                VehicleItem(vin="TESTVIN", garaging_address="123 Industrial Pkwy")
            )
        except RuntimeError as exc:
            assert str(exc).startswith("PLAYWRIGHT_BLOCKED:")
        else:
            raise AssertionError("disabled dropdown mode must fail closed")

    asyncio.run(_run())


def test_fill_commercial_auto_coverages_ui():
    async def _run():
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.select_option = AsyncMock()
        mock_locator.dispatch_event = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator.is_enabled = AsyncMock(return_value=True)
        mock_locator.first = mock_locator

        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.wait_for_timeout = AsyncMock()

        page_obj = EzlynxPolicySetupPage(mock_page)
        cov = CommercialAutoCoverageItem(
            csl_limit="1000000",
            bi_per_person="1000000",
            bi_per_accident="1000000",
            pd_per_accident="1000000",
            um_uim_limit="1000000",
            med_pay="5000",
            pip="250000",
        )
        await page_obj.fill_commercial_auto_coverages(cov)

        assert mock_page.locator.called
        assert mock_locator.fill.await_count >= 7
        assert mock_locator.dispatch_event.await_count >= 14

    asyncio.run(_run())


def test_add_vehicle_and_driver_ui():
    async def _run():
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.select_option = AsyncMock()
        mock_locator.dispatch_event = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator.is_enabled = AsyncMock(return_value=True)
        mock_locator.evaluate = AsyncMock(return_value="SELECT")
        mock_locator.all = AsyncMock(return_value=[mock_locator, mock_locator])
        mock_locator.first = mock_locator
        mock_locator.locator = MagicMock(return_value=mock_locator)

        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.wait_for_timeout = AsyncMock()

        page_obj = EzlynxPolicySetupPage(mock_page)

        # 1. Add Vehicle
        vehicle = VehicleItem(
            vin="1FTNE2Y84NKA12345",
            year="2022",
            make="Ford",
            model="Transit-250",
            cost_new="45000",
            premium="2450.00",
            comp_deductible="1000",
            coll_deductible="1000",
        )
        await page_obj.add_vehicle(vehicle)
        assert mock_locator.fill.await_count >= 5

        # Reset mocks
        mock_locator.fill.reset_mock()

        # 2. Add Driver
        driver = DriverItem(
            first_name="Carlo",
            last_name="Ferrara",
            dob="01/01/1985",
            license_number="F12345678901234",
            license_state="NJ",
            experience_years="15",
            licensed_year="2005",
        )
        await page_obj.add_driver(driver, driver_num="1")
        assert mock_locator.fill.await_count >= 5

    asyncio.run(_run())


def test_execute_formentry_schedules_test_only():
    async def _run():
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.select_option = AsyncMock()
        mock_locator.dispatch_event = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator.is_enabled = AsyncMock(return_value=True)
        mock_locator.evaluate = AsyncMock(return_value="SELECT")
        mock_locator.all = AsyncMock(return_value=[mock_locator, mock_locator])
        mock_locator.first = mock_locator
        mock_locator.locator = MagicMock(return_value=mock_locator)

        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.wait_for_timeout = AsyncMock()

        page_obj = EzlynxPolicySetupPage(mock_page)

        shell_input = PolicyShellInput(
            applicant_id="220250093",
            lob="commercial_auto",
            policy_number="CA-ROBIE-LIVE-02",
            vehicles=(
                VehicleItem(
                    vin="1FTNE2Y84NKA12345",
                    year="2022",
                    make="Ford",
                    model="Transit-250",
                    cost_new="45000",
                    premium="2450.00",
                    comp_deductible="1000",
                    coll_deductible="1000",
                ),
            ),
            drivers=(
                DriverItem(
                    first_name="Carlo",
                    last_name="Ferrara",
                    dob="01/01/1985",
                    license_number="F12345678901234",
                    license_state="NJ",
                    experience_years="15",
                ),
            ),
            commercial_auto_coverage=CommercialAutoCoverageItem(
                csl_limit="1000000",
                bi_per_person="1000000",
                bi_per_accident="1000000",
                pd_per_accident="1000000",
                um_uim_limit="1000000",
                med_pay="5000",
                pip="250000",
            ),
        )

        res = await page_obj.execute_formentry_schedules_test_only(shell_input)
        assert res["success"] is True
        assert res["applicant_id"] == "220250093"
        assert res["policy_number"] == "CA-ROBIE-LIVE-02"
        assert "vehicle:1FTNE2Y84NKA12345" in res["schedules_applied"]
        assert "driver:Carlo Ferrara" in res["schedules_applied"]
        assert "commercial_auto_coverages" in res["schedules_applied"]

    asyncio.run(_run())
