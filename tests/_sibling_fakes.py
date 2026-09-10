"""Shared sys.modules fake isolation for the verification worker unit tests.

Background: pytest imports every test module before running any test. A
fake installed with a bare ``sys.modules[name] = fake`` therefore leaks
into the imports of test modules collected later. Two rules make the fakes
behave:

1. ``from package import submodule`` resolves through the PARENT package's
   attribute first, not ``sys.modules``. Installing a fake must also set
   the parent attribute, otherwise a worker that does
   ``from . import verification_mailer as mailer`` binds the real module
   (or an earlier test file's fake) instead of this file's fake.
2. Teardown must restore the parent attribute alongside ``sys.modules``,
   and only when this file's fake is still the current entry -- a test
   module imported later may have installed its own fake on top, which must
   not be clobbered.

Usage: keep a per-module ``_INSTALLED`` dict, call
``install_fake(_INSTALLED, name, fake)`` for each faked sibling BEFORE
importing the worker under test, and call ``teardown_fakes(_INSTALLED)``
from the module-level ``teardown_module``.
"""

from __future__ import annotations

import sys
from typing import Any


def _set_parent_attribute(name: str, target: Any) -> None:
    """Point the parent package's ``child`` attribute at ``target``."""
    parent_name, _, child = name.rpartition(".")
    if not parent_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is None:
        return
    setattr(parent, child, target)


def _clear_parent_attribute(name: str, fake: Any) -> None:
    """Remove the parent attribute, but only when it is still our fake."""
    parent_name, _, child = name.rpartition(".")
    if not parent_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is None:
        return
    try:
        if getattr(parent, child, None) is fake:
            delattr(parent, child)
    except AttributeError:
        pass


def install_fake(registry: dict, name: str, fake: Any) -> None:
    """Install ``fake`` as ``sys.modules[name]`` plus the parent attribute."""
    if name not in registry:
        registry[name] = (sys.modules.get(name), fake)
    sys.modules[name] = fake
    _set_parent_attribute(name, fake)


def teardown_fakes(registry: dict) -> None:
    """Undo :func:`install_fake`; safe to call from ``teardown_module``."""
    for name, (original, fake) in registry.items():
        if sys.modules.get(name) is fake:
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
            if original is None:
                _clear_parent_attribute(name, fake)
            else:
                _set_parent_attribute(name, original)


def ensure_real_module(name: str) -> Any:
    """Evict any import-time fake for ``name`` and return the real module.

    Sibling worker test modules install fakes in ``sys.modules`` (plus the
    parent package attribute) at THEIR import time, and pytest/unittest
    import every test module before running any test. A test of the real
    implementation that binds ``from package import submodule`` at ITS
    import time would otherwise capture their fake. Call this before
    binding: when the current entry is a fake (a bare ``ModuleType`` with
    no ``__file__``), it is evicted from ``sys.modules`` and from the
    parent attribute, then the real module is imported fresh.

    This does not disturb the faking modules: they bound their fakes into
    their worker namespaces at their own import time, and their
    ``teardown_fakes`` becomes a safe no-op for the evicted entry (the
    current entry is no longer their fake).
    """
    import importlib

    module = sys.modules.get(name)
    if module is None or getattr(module, "__file__", None) is None:
        sys.modules.pop(name, None)
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            try:
                if getattr(parent, child, None) is module:
                    delattr(parent, child)
            except AttributeError:
                pass
        module = importlib.import_module(name)
    return module
