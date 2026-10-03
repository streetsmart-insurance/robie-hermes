#!/usr/bin/env python3
"""Fail closed when the dedicated accountability Magellan extract is an unverified zero.

``src/production_main.py`` on streetsmart-accountability-prod is not in this
repo. ``fetch_live_magellan_data`` can return ``calls: []`` after an empty
table race, and ``run(--publish --deliver)`` then emails that as success.

This repair inserts the rule from ``scripts/magellan_empty_gate.py`` and calls
it before a snapshot of that extract is saved and before publish or deliver.
Collect-only stays soft. ``MAGELLAN_ALLOW_EMPTY=1`` overrides publish/deliver.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


OLD_SIGNATURE = (
    "def build_department_data(target, sources: Dict[str, Path]) -> Dict[str, Dict[str, Any]]:"
)
NEW_SIGNATURE = (
    "def build_department_data(target, sources: Dict[str, Path], *, publish: bool = False, "
    "deliver: bool = False) -> Dict[str, Dict[str, Any]]:"
)
OLD_FETCH = "    magellan = fetch_live_magellan_data(target.isoformat())\n"
NEW_FETCH = (
    "    magellan = fetch_live_magellan_data(target.isoformat())\n"
    "    refuse_unverified_empty_magellan(magellan, publish=publish, deliver=deliver)\n"
)
OLD_RETURN = (
    '            "Source / verification": row.get("Handled State"),\n'
    "        })\n"
    "    return departments\n"
)
NEW_RETURN = (
    '            "Source / verification": row.get("Handled State"),\n'
    "        })\n"
    "    magellan_call_count_for_stamp = magellan_call_count(magellan)\n"
    "    magellan_verified_empty_for_stamp = magellan_verified_genuine_zero(magellan)\n"
    "    for _magellan_department in departments.values():\n"
    '        _magellan_department["magellan_calls_on_target_date"] = magellan_call_count_for_stamp\n'
    '        _magellan_department["magellan_extract_verified_empty"] = magellan_verified_empty_for_stamp\n'
    "    return departments\n"
)
OLD_CALL = "        departments = build_department_data(target, sources)\n"
NEW_CALL = (
    "        departments = build_department_data("
    "target, sources, publish=publish, deliver=deliver)\n"
)
OLD_REUSE = (
    "            departments = _load_prepared_snapshot(target)\n"
    "            reused = True\n"
)
NEW_REUSE = (
    "            departments = _load_prepared_snapshot(target)\n"
    "            if (publish or deliver) and snapshot_magellan_blocks_delivery(departments):\n"
    "                departments = None\n"
    "            else:\n"
    "                reused = True\n"
)
OLD_PUBLISH = "    url = None\n    if publish:\n"
NEW_PUBLISH = (
    "    if (publish or deliver) and snapshot_magellan_blocks_delivery(departments):\n"
    "        raise MagellanEmptyExtractError(EMPTY_MAGELLAN_REASON)\n"
    "    url = None\n"
    "    if publish:\n"
)
OLD_EXCEPT = (
    "    except SourceGateError as exc:\n"
    '        print(json.dumps({"status": "blocked", "reason": str(exc)}, indent=2))\n'
    "        raise SystemExit(2)\n"
)
NEW_EXCEPT = (
    "    except SourceGateError as exc:\n"
    '        print(json.dumps({"status": "blocked", "reason": str(exc)}, indent=2))\n'
    "        raise SystemExit(2)\n"
    "    except MagellanEmptyExtractError as exc:\n"
    '        print(json.dumps({"status": "blocked", "reason": str(exc)}, indent=2))\n'
    "        raise SystemExit(3)\n"
)

MARKERS = (
    "def refuse_unverified_empty_magellan(",
    "refuse_unverified_empty_magellan(magellan, publish=publish, deliver=deliver)",
    "build_department_data(target, sources, publish=publish, deliver=deliver)",
    "snapshot_magellan_blocks_delivery(departments)",
    "except MagellanEmptyExtractError as exc:",
    '"magellan_calls_on_target_date"',
)


def _load_gate():
    path = Path(__file__).resolve().parent / "magellan_empty_gate.py"
    spec = importlib.util.spec_from_file_location("magellan_empty_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Magellan empty-extract gate is missing: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def helper_source() -> str:
    module = _load_gate()
    parts = [
        f"EMPTY_MAGELLAN_REASON = {module.EMPTY_MAGELLAN_REASON!r}\n",
        inspect.getsource(module.MagellanEmptyExtractError),
        inspect.getsource(module.magellan_allow_empty),
        inspect.getsource(module.magellan_call_count),
        inspect.getsource(module.magellan_verified_genuine_zero),
        inspect.getsource(module.empty_magellan_reason),
        inspect.getsource(module.refuse_unverified_empty_magellan),
        inspect.getsource(module.snapshot_magellan_blocks_delivery),
    ]
    return "\n".join(parts)


def _anchors_ok(source: str) -> None:
    anchors = {
        "signature": OLD_SIGNATURE,
        "fetch": OLD_FETCH,
        "return": OLD_RETURN,
        "call": OLD_CALL,
        "reuse": OLD_REUSE,
        "publish": OLD_PUBLISH,
        "except": OLD_EXCEPT,
    }
    problems = [name for name, text in anchors.items() if source.count(text) != 1]
    if problems:
        raise RuntimeError(
            "refusing unexpected Magellan empty-extract shape: " + ", ".join(problems)
        )


def rewrite_source(original: str) -> tuple[str, str]:
    present = [marker in original for marker in MARKERS]
    if all(present):
        return "already_repaired", original
    if any(present):
        raise RuntimeError("refusing partial Magellan empty-extract repair")
    _anchors_ok(original)

    signature_at = original.index(OLD_SIGNATURE)
    updated = original[:signature_at] + helper_source().rstrip() + "\n\n" + original[signature_at:]
    updated = updated.replace(OLD_SIGNATURE, NEW_SIGNATURE, 1)
    updated = updated.replace(OLD_FETCH, NEW_FETCH, 1)
    updated = updated.replace(OLD_RETURN, NEW_RETURN, 1)
    updated = updated.replace(OLD_CALL, NEW_CALL, 1)
    updated = updated.replace(OLD_REUSE, NEW_REUSE, 1)
    updated = updated.replace(OLD_PUBLISH, NEW_PUBLISH, 1)
    updated = updated.replace(OLD_EXCEPT, NEW_EXCEPT, 1)
    compile(updated, "<production_main>", "exec")
    if not all(marker in updated for marker in MARKERS):
        raise RuntimeError("Magellan empty-extract repair did not install the publish gate")
    if updated.count(OLD_FETCH) != 1 or OLD_CALL in updated:
        raise RuntimeError("Magellan empty-extract repair left an unguarded live extract")
    return "repaired", updated


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    status, updated = rewrite_source(original)
    if status == "already_repaired":
        return status

    backup = path.with_suffix(path.suffix + ".pre-magellan-empty-gate")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if verified != updated:
        raise RuntimeError("Magellan empty-extract repair verification failed")
    compile(verified, str(path), "exec")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
