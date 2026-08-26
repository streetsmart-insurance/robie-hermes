from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


@dataclass
class ElementDescriptor:
    tag: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    element_id: str | None = None
    test_id: str | None = None
    name_attr: str | None = None
    class_names: list[str] = field(default_factory=list)
    css_selector: str | None = None
    xpath: str | None = None
    frame_src: str | None = None
    bounding_box: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UiAction:
    action_type: str  # navigate, click, fill, select, hover, wait, frame_switch, verify, upload, press_key
    target: ElementDescriptor | None = None
    value: str | None = None
    param_key: str | None = None  # Bound variable name if parameterized
    timestamp_ms: int = 0
    frame_path: list[str] = field(default_factory=list)  # Nested frame selectors/src
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompiledSkill:
    skill_name: str
    description: str
    parameters: list[dict[str, Any]]
    python_code: str
    skill_markdown: str
    actions: list[UiAction]
    readback_assertions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "description": self.description,
            "parameters": self.parameters,
            "python_code": self.python_code,
            "skill_markdown": self.skill_markdown,
            "actions": [a.to_dict() for a in self.actions],
            "readback_assertions": self.readback_assertions,
        }


class VideoToSkillCompiler:
    """Visual learning engine: compiles UI workflow recordings / traces into executable Playwright automation skills."""

    def __init__(self, *, default_timeout_ms: int = 15000, headless: bool = True):
        self.default_timeout_ms = default_timeout_ms
        self.headless = headless

    def compile_trace(
        self,
        raw_events: Sequence[dict[str, Any]],
        *,
        skill_name: str = "custom_workflow_skill",
        description: str = "Automated workflow compiled from visual recording",
        parameterize: bool = True,
    ) -> CompiledSkill:
        """Compile a list of raw recorded browser events / frame observations into a Playwright Skill."""
        actions = self._parse_actions(raw_events)
        optimized_actions = self._optimize_actions(actions)
        params = self._extract_parameters(optimized_actions) if parameterize else []
        assertions = self._extract_readback_assertions(optimized_actions)

        python_code = self._generate_playwright_python(
            skill_name=skill_name,
            actions=optimized_actions,
            parameters=params,
            assertions=assertions,
        )
        skill_md = self._generate_skill_markdown(
            skill_name=skill_name,
            description=description,
            parameters=params,
            python_code=python_code,
        )

        return CompiledSkill(
            skill_name=skill_name,
            description=description,
            parameters=params,
            python_code=python_code,
            skill_markdown=skill_md,
            actions=optimized_actions,
            readback_assertions=assertions,
        )

    def _parse_actions(self, raw_events: Sequence[dict[str, Any]]) -> list[UiAction]:
        parsed: list[UiAction] = []
        for ev in raw_events:
            atype = ev.get("action_type") or ev.get("type") or "click"
            target_data = ev.get("target") or {}
            elem = ElementDescriptor(
                tag=target_data.get("tag"),
                role=target_data.get("role"),
                name=target_data.get("name"),
                text=target_data.get("text"),
                element_id=target_data.get("element_id") or target_data.get("id"),
                test_id=target_data.get("test_id") or target_data.get("data-testid"),
                name_attr=target_data.get("name_attr") or target_data.get("name"),
                class_names=target_data.get("class_names") or [],
                css_selector=target_data.get("css_selector") or target_data.get("selector"),
                xpath=target_data.get("xpath"),
                frame_src=target_data.get("frame_src"),
                bounding_box=target_data.get("bounding_box"),
            )
            val = ev.get("value") or ev.get("text") or ev.get("url")
            ts = int(ev.get("timestamp_ms", 0))
            frame_path = ev.get("frame_path") or []
            if elem.frame_src and elem.frame_src not in frame_path:
                frame_path.append(elem.frame_src)

            action = UiAction(
                action_type=atype,
                target=elem,
                value=str(val) if val is not None else None,
                timestamp_ms=ts,
                frame_path=frame_path,
                metadata=ev.get("metadata") or {},
            )
            parsed.append(action)
        return parsed

    def _optimize_actions(self, actions: list[UiAction]) -> list[UiAction]:
        """Deduplicate rapid clicks, coalesce typing keystrokes, and eliminate redundant waits."""
        optimized: list[UiAction] = []
        for action in actions:
            if not optimized:
                optimized.append(action)
                continue
            prev = optimized[-1]
            # Coalesce consecutive fills on the same selector
            if (
                action.action_type in ("fill", "type")
                and prev.action_type in ("fill", "type")
                and action.target
                and prev.target
                and self._selector_for(action.target) == self._selector_for(prev.target)
            ):
                # keep the latest full value
                optimized[-1] = action
                continue

            # Drop redundant duplicate clicks on identical element within 200ms
            if (
                action.action_type == "click"
                and prev.action_type == "click"
                and action.target
                and prev.target
                and self._selector_for(action.target) == self._selector_for(prev.target)
                and 0 <= (action.timestamp_ms - prev.timestamp_ms) < 200
            ):
                continue

            optimized.append(action)
        return optimized

    def _extract_parameters(self, actions: list[UiAction]) -> list[dict[str, Any]]:
        """Identify candidate fields (e.g. Applicant Name, VIN, Policy Number) to parameterize."""
        params: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        for action in actions:
            if action.action_type in ("fill", "type", "select") and action.value:
                label_hint = self._infer_field_name(action)
                key = self._slugify(label_hint)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    action.param_key = key
                    params.append({
                        "name": key,
                        "label": label_hint,
                        "default_value": action.value,
                        "required": True,
                        "type": "string",
                    })
                elif key in seen_keys:
                    action.param_key = key

        return params

    def _infer_field_name(self, action: UiAction) -> str:
        if not action.target:
            return "input_value"
        t = action.target
        if t.name:
            return t.name
        if t.name_attr:
            return t.name_attr
        if t.test_id:
            return t.test_id
        if t.element_id:
            return t.element_id
        if t.text:
            return t.text[:25]
        return "input_field"

    def _slugify(self, text: str) -> str:
        s = re.sub(r"[^\w\s-]", "", text).strip().lower()
        s = re.sub(r"[-\s]+", "_", s)
        return s or "param"

    def _selector_for(self, target: ElementDescriptor | None) -> str:
        if not target:
            return "body"
        if target.test_id:
            return f"[data-testid='{target.test_id}']"
        if target.role and target.name:
            return f"role={target.role}[name='{target.name}']"
        if target.element_id:
            return f"#{target.element_id}"
        if target.name_attr:
            return f"[name='{target.name_attr}']"
        if target.css_selector:
            return target.css_selector
        if target.text and target.tag:
            return f"{target.tag}:has-text('{target.text}')"
        if target.xpath:
            return f"xpath={target.xpath}"
        return target.tag or "body"

    def _extract_readback_assertions(self, actions: list[UiAction]) -> list[dict[str, Any]]:
        assertions: list[dict[str, Any]] = []
        for action in actions:
            if action.action_type in ("verify", "assert_visible", "assert_text"):
                assertions.append({
                    "action_type": action.action_type,
                    "selector": self._selector_for(action.target),
                    "expected_value": action.value,
                    "frame_path": action.frame_path,
                })
        return assertions

    def _generate_playwright_python(
        self,
        *,
        skill_name: str,
        actions: list[UiAction],
        parameters: list[dict[str, Any]],
        assertions: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = [
            "from __future__ import annotations",
            "",
            "from typing import Any",
            "from playwright.sync_api import sync_playwright, Page, expect",
            "",
            f"def run_{skill_name}(page: Page, params: dict[str, Any]) -> dict[str, Any]:",
            '    """Automated workflow execution step compiled from visual recording."""',
            f"    page.set_default_timeout({self.default_timeout_ms})",
            "    results = {}",
        ]

        for i, action in enumerate(actions, 1):
            lines.append(f"    # Step {i}: {action.action_type}")
            scope_var = "page"
            if action.frame_path:
                # Handle iframe encapsulation
                frame_sel = action.frame_path[0]
                if frame_sel.startswith("http") or "/" in frame_sel:
                    lines.append(f"    frame = page.frame_locator('iframe[src*=\"{frame_sel}\"]')")
                else:
                    lines.append(f"    frame = page.frame_locator('{frame_sel}')")
                scope_var = "frame"

            selector = self._selector_for(action.target)

            if action.action_type in ("navigate", "goto"):
                url = action.value or "about:blank"
                lines.append(f"    page.goto({json.dumps(url)}, wait_until='networkidle')")

            elif action.action_type == "click":
                if selector.startswith("role="):
                    m = re.match(r"role=(\w+)\[name='(.*)'\]", selector)
                    if m:
                        role, name = m.group(1), m.group(2)
                        lines.append(f"    {scope_var}.get_by_role({json.dumps(role)}, name={json.dumps(name)}).click()")
                    else:
                        lines.append(f"    {scope_var}.locator({json.dumps(selector)}).click()")
                else:
                    lines.append(f"    {scope_var}.locator({json.dumps(selector)}).click()")

            elif action.action_type in ("fill", "type"):
                val_expr = f"params.get('{action.param_key}', {json.dumps(action.value or '')})" if action.param_key else json.dumps(action.value or "")
                lines.append(f"    target_input = {scope_var}.locator({json.dumps(selector)})")
                lines.append(f"    target_input.wait_for(state='visible')")
                lines.append(f"    target_input.fill({val_expr})")

            elif action.action_type == "select":
                val_expr = f"params.get('{action.param_key}', {json.dumps(action.value or '')})" if action.param_key else json.dumps(action.value or "")
                lines.append(f"    {scope_var}.locator({json.dumps(selector)}).select_option({val_expr})")

            elif action.action_type == "hover":
                lines.append(f"    {scope_var}.locator({json.dumps(selector)}).hover()")

            elif action.action_type == "wait":
                lines.append(f"    {scope_var}.locator({json.dumps(selector)}).wait_for(state='visible')")

            elif action.action_type in ("verify", "assert_visible"):
                verified_key = action.param_key or f"step_{i}_verified"
                lines.append(f"    expect({scope_var}.locator({json.dumps(selector)})).to_be_visible()")
                lines.append(f"    results['{verified_key}'] = True")

            elif action.action_type == "assert_text":
                text_key = action.param_key or f"step_{i}_text_verified"
                lines.append(f"    expect({scope_var}.locator({json.dumps(selector)})).to_contain_text({json.dumps(action.value or '')})")
                lines.append(f"    results['{text_key}'] = True")

            elif action.action_type == "press_key":
                key = action.value or "Enter"
                lines.append(f"    {scope_var}.locator({json.dumps(selector)}).press({json.dumps(key)})")

            elif action.action_type == "upload":
                lines.append(f"    {scope_var}.locator({json.dumps(selector)}).set_input_files(params.get('file_path', {json.dumps(action.value or '')}))")

        lines.extend([
            "    results['status'] = 'SUCCESS'",
            "    return results",
            "",
            f"def execute_standalone(params: dict[str, Any] | None = None) -> dict[str, Any]:",
            "    params = params or {}",
            "    with sync_playwright() as p:",
            f"        browser = p.chromium.launch(headless={self.headless})",
            "        context = browser.new_context()",
            "        page = context.new_page()",
            f"        res = run_{skill_name}(page, params)",
            "        browser.close()",
            "        return res",
        ])
        return "\n".join(lines)

    def _generate_skill_markdown(
        self,
        *,
        skill_name: str,
        description: str,
        parameters: list[dict[str, Any]],
        python_code: str,
    ) -> str:
        param_table = "\n".join(
            f"| `{p['name']}` | {p['type']} | `{p['default_value']}` | {p['label']} |"
            for p in parameters
        )
        if not param_table:
            param_table = "| (none) | - | - | No dynamic parameters |"

        return f"""---
name: {skill_name}
description: {description}
version: 1.0.0
---

# {skill_name}

{description}

## Parameters
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
{param_table}

## Executable Automation Script
```python
{python_code}
```

## Verification & Readback Checkpoint
- Verify authoritative state changes after script completion.
- Fail closed if expected confirmation indicators are absent.
"""


def compile_recording_to_skill(
    trace_events: Sequence[dict[str, Any]],
    skill_name: str = "visual_workflow_skill",
    description: str = "Compiled Playwright automation skill",
) -> CompiledSkill:
    compiler = VideoToSkillCompiler()
    return compiler.compile_trace(trace_events, skill_name=skill_name, description=description)
