"""Keep the guarded browser schema direct on the installed Hermes interface."""
from __future__ import annotations


def expose_guarded_browser(toolsets=None):
    if toolsets is None:
        import toolsets
    core = getattr(toolsets, '_HERMES_CORE_TOOLS', None)
    # This installed interface was measured on Production. Do not silently
    # disable the repair if an upstream update changes its contract.
    if not isinstance(core, list) or not all(isinstance(name, str) for name in core):
        raise RuntimeError('Unsupported Hermes direct-tool visibility interface')
    if 'playwright_exec' not in core:
        core.append('playwright_exec')

