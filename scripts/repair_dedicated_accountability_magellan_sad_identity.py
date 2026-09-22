#!/usr/bin/env python3
"""Idempotently repair Magellan SAD client phone and name on the dedicated app.

The department Google Doc section ``Customer sentiment (SAD) — Magellan`` was
listing Magellan's account/agency DID in the Phone column and dumping that same
number into ``Account / caller``. Carlo needs:

1. Magellan **client** phone (ANI), never the agency/account DID
2. Real client name: Magellan caller name when present, else EZLynx phone match
3. No agency-name fallback when Magellan and EZLynx both lack a name

The dedicated tree is not in this monorepo. This repair rewrites the Magellan
SAD row builder in ``production_main.py`` and teaches the Magellan Playwright
extractor to capture ``caller_name`` plus tel: href phones.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


# ---------------------------------------------------------------------------
# production_main.py — SAD row identity
# ---------------------------------------------------------------------------

OLD_SAD_LOOP = '''    sad = []
    for row in magellan["calls"]:
        if str(row.get("sentiment", "")).casefold() not in {"sad", "negative", "at risk", "at-risk"} and not any("risk" in str(t).casefold() for t in row.get("tags", [])):
            continue
        matches = sales_by_phone.get(_phone(row.get("from_phone")), [])
        active_matches = [item for item in matches if str(item.get("Opportunity Status") or "").casefold() in active_sales_statuses]
        candidates = active_matches or matches
        owned = [item for item in candidates if _record_department(item, roster, "CSR", "Assigned Producer") != "Executive & Unverified"]
        ownership = max(owned, key=lambda item: str(item.get("Opportunity Created Date") or ""), default={})
        owner = ownership.get("CSR") or ownership.get("Assigned Producer") or row.get("to_phone")
        department = _record_department(ownership, roster, "CSR", "Assigned Producer") if ownership else "Executive & Unverified"
        normalized_phone = _phone(row.get("from_phone"))
        account_or_caller = MAGELLAN_ACCOUNT_OVERRIDES.get(normalized_phone, row.get("from_phone"))
        sad.append({"Date & Time": row.get("date_time"), "Account / caller": account_or_caller,
                    "From": row.get("from_phone"), "Phone": row.get("from_phone"),
                    "Sentiment": row.get("sentiment"), "Topics": ", ".join(row.get("tags", [])), "Owner": owner,
                    "Department": department,
                    "Handled State": "Department matched through EZLynx phone ownership; callback status requires RingCentral evidence" if ownership else "UNVERIFIED — no unique EZLynx phone ownership match"})
        sad[-1]["Duration"] = row.get("duration", "")
        sad[-1]["Business Hours"] = "Yes"
'''

NEW_SAD_LOOP = '''    sad = []
    for row in magellan["calls"]:
        if str(row.get("sentiment", "")).casefold() not in {"sad", "negative", "at risk", "at-risk"} and not any("risk" in str(t).casefold() for t in row.get("tags", [])):
            continue
        client_phone = _magellan_client_phone(row)
        normalized_phone = _phone(client_phone)
        matches = sales_by_phone.get(normalized_phone, []) if normalized_phone else []
        active_matches = [item for item in matches if str(item.get("Opportunity Status") or "").casefold() in active_sales_statuses]
        candidates = active_matches or matches
        owned = [item for item in candidates if _record_department(item, roster, "CSR", "Assigned Producer") != "Executive & Unverified"]
        ownership = max(owned, key=lambda item: str(item.get("Opportunity Created Date") or ""), default={})
        owner = ownership.get("CSR") or ownership.get("Assigned Producer") or ""
        department = _record_department(ownership, roster, "CSR", "Assigned Producer") if ownership else "Executive & Unverified"
        account_or_caller = _magellan_client_name(row, ownership, normalized_phone)
        sad.append({"Date & Time": row.get("date_time"), "Account / caller": account_or_caller,
                    "From": client_phone, "Phone": client_phone,
                    "Sentiment": row.get("sentiment"), "Topics": ", ".join(row.get("tags", [])), "Owner": owner,
                    "Department": department,
                    "Handled State": "Department matched through EZLynx phone ownership; callback status requires RingCentral evidence" if ownership else "UNVERIFIED — no unique EZLynx phone ownership match"})
        sad[-1]["Duration"] = row.get("duration", "")
        sad[-1]["Business Hours"] = "Yes"
'''

HELPERS_MARKER = "def _magellan_client_phone("

HELPERS_BLOCK = '''
# Magellan account / agency DIDs. Client ANI must win over these numbers.
MAGELLAN_AGENCY_DIDS = {"7324628343"}
MAGELLAN_AGENCY_NAMES = {
    "street smart",
    "streetsmart",
    "street smart insurance",
    "streetsmart insurance",
    "street smart insurance agency",
    "streetsmart insurance agency",
    "agency",
    "main",
    "main line",
    "ai receptionist",
    "magellan",
}


def _magellan_party_name_and_phone(value: Any) -> tuple[str, str]:
    """Split a Magellan From/To cell into (name, phone-text)."""
    raw = str(value or "").replace("\\xa0", " ")
    if not raw.strip():
        return "", ""
    phone_matches = list(re.finditer(r"(?:\\+?1[-.\\s]*)?(?:\\(?\\d{3}\\)?[-.\\s]*)?\\d{3}[-.\\s]*\\d{4}", raw))
    phone_text = phone_matches[-1].group(0) if phone_matches else ""
    name = raw
    for match in phone_matches:
        name = name.replace(match.group(0), " ")
    name = " ".join(name.split()).strip(" -\\t|/")
    folded = name.casefold()
    if not name or folded in MAGELLAN_AGENCY_NAMES or folded in {"unknown", "unknown client", "unverified", "n/a", "na", "none", "-", "--"}:
        name = ""
    if not phone_text:
        digits = re.sub(r"\\D", "", raw)
        if len(digits) >= 10 and not re.search(r"[A-Za-z]", raw):
            phone_text = digits[-10:] if len(digits) >= 10 else digits
            name = ""
    return name, phone_text


def _is_magellan_agency_did(value: Any) -> bool:
    digits = re.sub(r"\\D", "", str(value or ""))
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    digits = digits[-10:] if len(digits) >= 10 else digits
    return bool(digits) and digits in MAGELLAN_AGENCY_DIDS


def _magellan_client_phone(row: Dict[str, Any]) -> str:
    """Prefer Magellan client/ANI; never the agency/account DID."""
    for key in ("client_phone", "caller_phone"):
        _, phone_text = _magellan_party_name_and_phone(row.get(key))
        phone_text = phone_text or str(row.get(key) or "").strip()
        if phone_text and not _is_magellan_agency_did(phone_text):
            return phone_text
    _, from_text = _magellan_party_name_and_phone(row.get("from_phone"))
    _, to_text = _magellan_party_name_and_phone(row.get("to_phone"))
    from_text = from_text or str(row.get("from_phone") or "").strip()
    to_text = to_text or str(row.get("to_phone") or "").strip()
    if from_text and not _is_magellan_agency_did(from_text):
        return from_text
    if to_text and not _is_magellan_agency_did(to_text):
        return to_text
    return ""


def _magellan_client_name(row: Dict[str, Any], ownership: Dict[str, Any], normalized_phone: str) -> str:
    """Magellan caller name, else EZLynx / verified override; never agency brand."""
    magellan_name = str(row.get("caller_name") or row.get("client_name") or row.get("from_name") or "").strip()
    if not magellan_name:
        magellan_name, _ = _magellan_party_name_and_phone(row.get("from_phone"))
    if magellan_name and magellan_name.casefold() not in MAGELLAN_AGENCY_NAMES:
        folded = magellan_name.casefold()
        if folded not in {"unknown", "unknown client", "unverified", "n/a", "na", "none", "-", "--"} and re.search(r"[A-Za-z]", magellan_name):
            return " ".join(magellan_name.split())
    override = ""
    if normalized_phone:
        override = str(MAGELLAN_ACCOUNT_OVERRIDES.get(normalized_phone) or "").strip()
    ezlynx = ""
    if ownership:
        for key in ("Account Name", "Applicant Name", "Customer Name", "Insured"):
            value = str(ownership.get(key) or "").strip()
            if value and value.casefold() not in MAGELLAN_AGENCY_NAMES and re.search(r"[A-Za-z]", value):
                ezlynx = " ".join(value.split())
                break
    for candidate in (override, ezlynx):
        if candidate and candidate.casefold() not in MAGELLAN_AGENCY_NAMES:
            return candidate
    return "Unknown"

'''


# ---------------------------------------------------------------------------
# magellan_playwright.py — extract caller_name + tel: phones
# ---------------------------------------------------------------------------

OLD_EXTRACT_PUSH = r'''                    res.push({
                        date_time: dateEl.innerText.replace(/\\n/g, ' ').trim(),
                        from_phone: fromEl.innerText.replace(/\\n/g, ' ').trim(),
                        to_phone: toEl ? toEl.innerText.replace(/\\n/g, ' ').trim() : '',
                        duration: durEl ? durEl.innerText.trim() : '',
                        sentiment: sentiment,
                        tags: uniqueTags
                    });'''

NEW_EXTRACT_PUSH = r'''                    const fromTel = fromEl.querySelector('a[href^="tel:"]');
                    const toTel = toEl ? toEl.querySelector('a[href^="tel:"]') : null;
                    const fromText = fromEl.innerText.replace(/\\n/g, ' ').trim();
                    const toText = toEl ? toEl.innerText.replace(/\\n/g, ' ').trim() : '';
                    const fromPhone = fromTel ? String(fromTel.getAttribute('href') || '').replace(/^tel:/i, '') : fromText;
                    const toPhone = toTel ? String(toTel.getAttribute('href') || '').replace(/^tel:/i, '') : toText;
                    const callerName = fromText.replace(fromPhone, ' ').replace(/\\+?1?[-.\\s]*\\(?\\d{3}\\)?[-.\\s]*\\d{3}[-.\\s]*\\d{4}/g, ' ').replace(/\\s+/g, ' ').trim();
                    res.push({
                        date_time: dateEl.innerText.replace(/\\n/g, ' ').trim(),
                        from_phone: fromPhone,
                        to_phone: toPhone,
                        caller_name: callerName,
                        duration: durEl ? durEl.innerText.trim() : '',
                        sentiment: sentiment,
                        tags: uniqueTags
                    });'''


def _insert_helpers(source: str) -> str:
    if HELPERS_MARKER in source:
        return source
    anchor = "def _phone(value: Any) -> str:"
    if source.count(anchor) != 1:
        raise RuntimeError("refusing unexpected _phone helper shape for Magellan SAD identity repair")
    phone_start = source.index(anchor)
    # Insert helpers immediately before _phone.
    return source[:phone_start] + HELPERS_BLOCK + source[phone_start:]


def repair_production_main(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    old_count = original.count(OLD_SAD_LOOP)
    new_count = original.count(NEW_SAD_LOOP)
    helpers_present = HELPERS_MARKER in original

    if old_count == 0 and new_count == 1 and helpers_present:
        return "already_repaired"
    if old_count != 1 or new_count != 0:
        raise RuntimeError(
            "refusing unexpected Magellan SAD row builder shape: "
            f"old={old_count}, new={new_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-magellan-sad-identity")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_SAD_LOOP, NEW_SAD_LOOP, 1)
    updated = _insert_helpers(updated)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if (
        verified.count(OLD_SAD_LOOP) != 0
        or verified.count(NEW_SAD_LOOP) != 1
        or HELPERS_MARKER not in verified
    ):
        raise RuntimeError("Magellan SAD identity repair verification failed for production_main.py")
    compile(verified, str(path), "exec")
    return "repaired"


def repair_magellan_extractor(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    old_count = original.count(OLD_EXTRACT_PUSH)
    new_count = original.count(NEW_EXTRACT_PUSH)

    if old_count == 0 and new_count == 1:
        return "already_repaired"
    if old_count != 1 or new_count != 0:
        raise RuntimeError(
            "refusing unexpected Magellan extract push shape: "
            f"old={old_count}, new={new_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-magellan-sad-identity")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_EXTRACT_PUSH, NEW_EXTRACT_PUSH, 1)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if verified.count(OLD_EXTRACT_PUSH) != 0 or verified.count(NEW_EXTRACT_PUSH) != 1:
        raise RuntimeError("Magellan SAD identity repair verification failed for magellan_playwright.py")
    if "caller_name" not in verified:
        raise RuntimeError("Magellan extractor repair did not retain caller_name")
    return "repaired"


def repair(*, production_main: Path, magellan_extractor: Path) -> dict[str, str]:
    return {
        "production_main": repair_production_main(production_main),
        "magellan_extractor": repair_magellan_extractor(magellan_extractor),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production-main", required=True)
    parser.add_argument("--magellan-extractor", required=True)
    args = parser.parse_args()
    result = repair(
        production_main=Path(args.production_main),
        magellan_extractor=Path(args.magellan_extractor),
    )
    print(f"production_main={result['production_main']}")
    print(f"magellan_extractor={result['magellan_extractor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
