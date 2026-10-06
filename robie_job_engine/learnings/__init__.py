"""Per-worker learnings store.

Each of the four verification workers (policy changes, mortgagee, manual
renewals, audits) keeps its accumulated learnings in a YAML file next to
this module. New learnings are appended to the worker's file; this loader
validates the schema and exposes them to the workers.

Schema per entry: id, rule, learned (YYYY-MM-DD), source.
"""

import datetime
import os

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

WORKERS = ("policy_changes", "mortgagee", "renewals", "audits")

_ID_PREFIX = {
    "policy_changes": "pc-",
    "mortgagee": "mg-",
    "renewals": "rn-",
    "audits": "au-",
}

_REQUIRED_FIELDS = ("id", "rule", "learned", "source")

_LEARNINGS_DIR = os.path.dirname(os.path.abspath(__file__))


def _path(worker):
    return os.path.join(_LEARNINGS_DIR, f"{worker}.yaml")


def _validate(worker, learnings):
    prefix = _ID_PREFIX[worker]
    seen = set()
    for i, entry in enumerate(learnings):
        if not isinstance(entry, dict):
            raise ValueError(f"{worker}[{i}]: entry is not a mapping")
        for field in _REQUIRED_FIELDS:
            if not entry.get(field):
                raise ValueError(f"{worker}[{i}]: missing required field '{field}'")
        entry_id = entry["id"]
        if not entry_id.startswith(prefix):
            raise ValueError(
                f"{worker}[{i}]: id '{entry_id}' must start with '{prefix}'"
            )
        if entry_id in seen:
            raise ValueError(f"{worker}[{i}]: duplicate id '{entry_id}'")
        seen.add(entry_id)
        try:
            datetime.date.fromisoformat(str(entry["learned"]))
        except ValueError:
            raise ValueError(
                f"{worker}[{i}]: learned '{entry['learned']}' is not YYYY-MM-DD"
            )
    return learnings


def get_learnings(worker):
    """Return the validated list of learnings for one worker."""
    if worker not in WORKERS:
        raise ValueError(f"unknown worker '{worker}'; expected one of {WORKERS}")
    if yaml is None:
        raise RuntimeError("PyYAML is required to load worker learnings")
    path = _path(worker)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _validate(worker, data.get("learnings", []))


def all_learnings():
    """Return {worker: [learnings]} for all four workers."""
    return {worker: get_learnings(worker) for worker in WORKERS}
