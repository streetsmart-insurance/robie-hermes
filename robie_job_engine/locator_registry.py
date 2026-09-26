"""Declarative locator registry for carrier and portal automation.

Maps field/action names to primary and fallback Playwright locators.
Enforces strict uniqueness: positional locators (.first, .nth, .last) are prohibited.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .playwright_write_guard import locator_is_positional_guess


@dataclass(frozen=True)
class FieldLocator:
    name: str
    primary_strategy: str  # role, label, text, test_id, css
    primary_selector: str
    fallback_strategy: str | None = None
    fallback_selector: str | None = None
    exact: bool = True
    description: str = ""
    last_updated: str = ""
    name_pattern: str = ""
    accessible_name: str = ""

    def validate(self) -> None:
        if locator_is_positional_guess(self.primary_selector):
            raise ValueError(f"Primary selector contains forbidden positional guess: {self.primary_selector}")
        if self.fallback_selector and locator_is_positional_guess(self.fallback_selector):
            raise ValueError(f"Fallback selector contains forbidden positional guess: {self.fallback_selector}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageLocators:
    portal: str
    page_name: str
    version: str = "1.0"
    fields: dict[str, FieldLocator] = field(default_factory=dict)

    def add_field(self, field_locator: FieldLocator) -> None:
        field_locator.validate()
        self.fields[field_locator.name] = field_locator

    def get_field(self, field_name: str) -> FieldLocator | None:
        return self.fields.get(field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "portal": self.portal,
            "page_name": self.page_name,
            "version": self.version,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
        }


class LocatorRegistry:
    """Registry loading declarative locator definitions from registry directories."""

    def __init__(self, registry_dir: str | Path | None = None) -> None:
        if registry_dir:
            self.registry_dir = Path(registry_dir)
        else:
            self.registry_dir = Path(__file__).resolve().parent / "locators"
        self._pages: dict[str, PageLocators] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.registry_dir.exists():
            return
        for file in self.registry_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text())
                portal = data.get("portal")
                page_name = data.get("page_name")
                if portal and page_name:
                    key = f"{portal}:{page_name}"
                    page_loc = PageLocators(portal=portal, page_name=page_name, version=data.get("version", "1.0"))
                    for fname, fval in data.get("fields", {}).items():
                        floc = FieldLocator(
                            name=fname,
                            primary_strategy=fval.get("primary_strategy", "label"),
                            primary_selector=fval.get("primary_selector", ""),
                            fallback_strategy=fval.get("fallback_strategy"),
                            fallback_selector=fval.get("fallback_selector"),
                            exact=bool(fval.get("exact", True)),
                            description=fval.get("description", ""),
                            last_updated=fval.get("last_updated", ""),
                            name_pattern=str(fval.get("name_pattern") or ""),
                            accessible_name=str(fval.get("accessible_name") or ""),
                        )
                        page_loc.add_field(floc)
                    self._pages[key] = page_loc
            except Exception:
                pass

    def register_page(self, page_locators: PageLocators) -> None:
        key = f"{page_locators.portal}:{page_locators.page_name}"
        self._pages[key] = page_locators

    def get_page(self, portal: str, page_name: str) -> PageLocators | None:
        key = f"{portal}:{page_name}"
        return self._pages.get(key)

    def get_locator(self, portal: str, page_name: str, field_name: str) -> FieldLocator | None:
        key = f"{portal}:{page_name}"
        page = self._pages.get(key)
        if not page:
            return None
        return page.get_field(field_name)

    def resolve_element(self, page_obj: Any, portal: str, page_name: str, field_name: str) -> Any:
        """Resolve a Playwright Locator using primary with fallback. Must resolve to exactly 1 element."""
        loc = self.get_locator(portal, page_name, field_name)
        if not loc:
            raise KeyError(f"No locator registered for {portal}:{page_name}.{field_name}")

        target = self._build_locator(page_obj, loc.primary_strategy, loc.primary_selector, loc.exact)
        count_fn = getattr(target, "count", None)
        count = count_fn() if callable(count_fn) else 1

        if count == 1:
            return target

        if count == 0 and loc.fallback_selector and loc.fallback_strategy:
            fb_target = self._build_locator(page_obj, loc.fallback_strategy, loc.fallback_selector, loc.exact)
            fb_count_fn = getattr(fb_target, "count", None)
            fb_count = fb_count_fn() if callable(fb_count_fn) else 1
            if fb_count == 1:
                return fb_target

        raise RuntimeError(
            f"PLAYWRIGHT_BLOCKED: Locator for {portal}:{page_name}.{field_name} "
            f"resolved to {count} elements (expected 1)"
        )

    def _build_locator(self, page_obj: Any, strategy: str, selector: str, exact: bool) -> Any:
        if strategy == "role":
            # e.g. "button:New program"
            parts = selector.split(":", 1)
            role = parts[0]
            name = parts[1] if len(parts) > 1 else None
            if name:
                return page_obj.get_by_role(role, name=name, exact=exact)
            return page_obj.get_by_role(role)
        if strategy == "label":
            return page_obj.get_by_label(selector, exact=exact)
        if strategy == "text":
            return page_obj.get_by_text(selector, exact=exact)
        if strategy == "test_id":
            return page_obj.get_by_test_id(selector)
        return page_obj.locator(selector)
