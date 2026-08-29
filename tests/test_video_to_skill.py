from __future__ import annotations

import json
from pathlib import Path

from robie_job_engine.video_to_skill import (
    VideoToSkillCompiler,
    compile_recording_to_skill,
    ElementDescriptor,
    UiAction,
)


def test_video_to_skill_compiler_end_to_end():
    sample_trace = [
        {
            "action_type": "navigate",
            "value": "https://portal.travelers.com/login",
            "timestamp_ms": 1000,
        },
        {
            "action_type": "fill",
            "target": {"name": "Username", "name_attr": "username", "role": "textbox"},
            "value": "agent_user_1",
            "timestamp_ms": 2500,
        },
        {
            "action_type": "fill",
            "target": {"name": "Password", "name_attr": "password", "role": "textbox"},
            "value": "secret_pass",
            "timestamp_ms": 3200,
        },
        {
            "action_type": "click",
            "target": {"role": "button", "name": "Sign In"},
            "timestamp_ms": 4000,
        },
        {
            "action_type": "fill",
            "target": {
                "tag": "input",
                "test_id": "applicant-name-input",
                "name": "Applicant Name",
                "frame_src": "/Applicant/12345/QuoteFrame",
            },
            "value": "Acme Trucking LLC",
            "timestamp_ms": 6000,
            "frame_path": ["/Applicant/12345/QuoteFrame"],
        },
        {
            "action_type": "select",
            "target": {"name": "Coverage Type", "element_id": "cov_type_select"},
            "value": "Commercial Auto",
            "timestamp_ms": 7000,
        },
        {
            "action_type": "click",
            "target": {"role": "button", "name": "Generate Quote"},
            "timestamp_ms": 8000,
        },
        {
            "action_type": "assert_text",
            "target": {"test_id": "quote-status-badge"},
            "value": "Quote Generated",
            "timestamp_ms": 10000,
        },
    ]

    compiler = VideoToSkillCompiler(default_timeout_ms=20000, headless=True)
    skill = compiler.compile_trace(
        sample_trace,
        skill_name="travelers_quote_generation",
        description="Automated quote generation on Travelers portal",
        parameterize=True,
    )

    assert skill.skill_name == "travelers_quote_generation"
    assert "Automated quote generation" in skill.description
    assert len(skill.actions) == 8

    # Verify extracted parameters
    param_names = [p["name"] for p in skill.parameters]
    assert "username" in param_names
    assert "password" in param_names
    assert "applicant_name" in param_names or "applicant_name_input" in param_names

    # Verify generated Python code contains expected Playwright idioms
    code = skill.python_code
    assert "def run_travelers_quote_generation(page: Page, params: dict[str, Any]) -> dict[str, Any]:" in code
    assert 'page.goto("https://portal.travelers.com/login"' in code
    assert 'page.get_by_role("button", name="Sign In").click()' in code
    assert 'frame = page.frame_locator(\'iframe[src*="/Applicant/12345/QuoteFrame"]\')' in code
    assert 'expect(page.locator("[data-testid=\'quote-status-badge\']")).to_contain_text("Quote Generated")' in code
    assert "def execute_standalone(params: dict[str, Any] | None = None) -> dict[str, Any]:" in code

    # Verify Skill Markdown
    md = skill.skill_markdown
    assert "---" in md
    assert "name: travelers_quote_generation" in md
    assert "## Parameters" in md
    assert "## Executable Automation Script" in md


def test_action_optimization_and_deduplication():
    events = [
        # Rapid typing simulation
        {
            "action_type": "fill",
            "target": {"element_id": "search_box"},
            "value": "A",
            "timestamp_ms": 100,
        },
        {
            "action_type": "fill",
            "target": {"element_id": "search_box"},
            "value": "Acme",
            "timestamp_ms": 150,
        },
        {
            "action_type": "fill",
            "target": {"element_id": "search_box"},
            "value": "Acme Logistics",
            "timestamp_ms": 200,
        },
        # Duplicate accidental double-click within 50ms
        {
            "action_type": "click",
            "target": {"role": "button", "name": "Search"},
            "timestamp_ms": 300,
        },
        {
            "action_type": "click",
            "target": {"role": "button", "name": "Search"},
            "timestamp_ms": 330,
        },
    ]

    compiler = VideoToSkillCompiler()
    skill = compiler.compile_trace(events, skill_name="dedup_test")

    # Typing should coalesce to 1 fill action with full text, click deduplicated to 1
    assert len(skill.actions) == 2
    assert skill.actions[0].value == "Acme Logistics"
    assert skill.actions[1].action_type == "click"


def test_convenience_compile_function():
    events = [
        {"action_type": "navigate", "value": "https://example.com"},
        {"action_type": "click", "target": {"test_id": "start-btn"}},
    ]
    compiled = compile_recording_to_skill(events, skill_name="quick_demo")
    assert compiled.skill_name == "quick_demo"
    assert 'page.goto("https://example.com"' in compiled.python_code
    assert 'page.locator("[data-testid=\'start-btn\']").click()' in compiled.python_code


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite()
