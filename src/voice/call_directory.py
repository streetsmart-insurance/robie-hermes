"""Durable server-side carrier/contact phone directory for Robie Call.

Resolution order for dispatcher lookups:
1. Runtime store ``data/voice_call_directory.json`` (gitignored, written on the VM)
2. Committed seed ``data/voice_call_directory.seed.json``
3. Hardcoded ``KNOWN_CARRIER_PHONES`` / ``HARDCODED_CARRIER_PHONES`` fallback

Producer warm-transfer phones live under the ``producers`` array in the same
JSON files. Email alone is not enough to transfer — lookup must return E.164.

Scrapers are pluggable. Built-in sources:
- ``KnownCarrierPhonesSeedSource`` — bootstrap from the hardcoded map / seed file
- ``EzlynxExtractedDirectorySource`` — phones already extracted into repo JSON
- ``StreetSmartDirectoryHook`` — documented hook for the live StreetSmart
  directory. Reads ``STREETSMART_VOICE_DIRECTORY_JSON`` when present; otherwise
  a no-op so hermes-poc-01 does not depend on a laptop Chrome crawl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.config import BASE_DIR

logger = logging.getLogger("voice_call_directory")

RUNTIME_DIRECTORY_PATH = BASE_DIR / "data" / "voice_call_directory.json"
SEED_DIRECTORY_PATH = BASE_DIR / "data" / "voice_call_directory.seed.json"

# Same map historically shipped in ezlynx_label_dispatcher. Kept here so the
# dispatcher can fall back if both JSON files are missing on a fresh VM.
HARDCODED_CARRIER_PHONES: Dict[str, str] = {
    "the hartford": "+18005551234",
    "hartford": "+18005551234",
    "travelers": "+18002386225",
    "coterie": "+18555673421",
    "coterie insurance": "+18555673421",
    "progressive": "+18008765581",
    "tapco": "+18003345579",
    "tapco underwriters": "+18003345579",
    "chubb": "+18002524670",
    "chubb group": "+18002524670",
    "amtrust": "+18775287878",
    "cna": "+18002622000",
    "cna surety": "+18002622000",
    "liberty mutual": "+18003440197",
    "employers": "+18886826671",
    "guard": "+18006732265",
    "berkshire hathaway guard": "+18006732265",
    "bhhc": "+18884958949",
    "berkshire hathaway": "+18884958949",
    "rps": "+18665958405",
    "risk placement services": "+18665958405",
    "amwins": "+18002213824",
    "jimcor": "+18006440333",
    "specialty coverage": "+18002422200",
    "new england excess": "+18005484301",
    "markel": "+18004311270",
    "utica first": "+18005565376",
    "utica first insurance company": "+18005565376",
    "tip national": "+18006888408",
    "tip national llc": "+18006888408",
    "carlo": "+17329953409",
    "carlo ferrara": "+17329953409",
    "buster brown": "+17329953409",
    "jake": "+17326688161",
    "jimmy": "+17329954324",
}

# Backward-compatible alias used by the dispatcher and tests.
KNOWN_CARRIER_PHONES = HARDCODED_CARRIER_PHONES


def normalize_phone_e164(raw_phone: Optional[str]) -> Optional[str]:
    """Normalizes any phone string to E.164 format (+1XXXXXXXXXX)."""
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return None


def _normalize_name(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not load voice directory JSON from %s: %s", path, exc)
        return {}


def _phones_from_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    """Accept ``{"phones": {name: +E164}}`` or a flat name→phone map."""
    raw = payload.get("phones") if isinstance(payload.get("phones"), dict) else payload
    phones: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return phones
    for name, value in raw.items():
        if name in {"version", "source", "updated_at", "sources", "phones", "producers"}:
            continue
        phone = None
        if isinstance(value, str):
            phone = normalize_phone_e164(value)
        elif isinstance(value, dict):
            phone = normalize_phone_e164(value.get("phone") or value.get("phone_number"))
        if phone:
            phones[_normalize_name(str(name))] = phone
    return phones


def _match_phone(phones: Dict[str, str], carrier_name: str) -> Optional[str]:
    clean = _normalize_name(carrier_name)
    if not clean or not phones:
        return None
    if clean in phones:
        return phones[clean]
    for key, phone in phones.items():
        if key in clean or clean in key:
            return phone
    return None


class DirectorySource(ABC):
    """Pluggable scrape source that returns ``{normalized_name: +E164}``."""

    name: str = "base"

    @abstractmethod
    def scrape(self) -> Dict[str, str]:
        raise NotImplementedError


class KnownCarrierPhonesSeedSource(DirectorySource):
    """Bootstrap from the committed seed file, then the hardcoded map."""

    name = "known_carrier_phones_seed"

    def __init__(self, seed_path: Optional[Path] = None):
        self.seed_path = Path(seed_path) if seed_path is not None else SEED_DIRECTORY_PATH

    def scrape(self) -> Dict[str, str]:
        phones = _phones_from_payload(_load_json(self.seed_path))
        if phones:
            return phones
        return dict(HARDCODED_CARRIER_PHONES)


class EzlynxExtractedDirectorySource(DirectorySource):
    """Read phones already extracted from EZLynx onto the server.

    Looks at (in order):
    - ``data/ezlynx_full_extracted_directory.json`` (ContextHydrator format)
    - ``data/ezlynx_directory_mapping.json`` (``scripts/crawl_ezlynx_directory.py``)
    """

    name = "ezlynx_extracted_directory"

    def __init__(self, search_paths: Optional[Iterable[Path]] = None):
        self.search_paths = list(search_paths) if search_paths is not None else [
            BASE_DIR / "data" / "ezlynx_full_extracted_directory.json",
            BASE_DIR / "data" / "ezlynx_directory_mapping.json",
        ]

    def scrape(self) -> Dict[str, str]:
        phones: Dict[str, str] = {}
        for path in self.search_paths:
            payload = _load_json(path)
            if not payload:
                continue
            phones.update(self._extract_phones(payload))
        return phones

    @staticmethod
    def _extract_phones(payload: Dict[str, Any]) -> Dict[str, str]:
        found: Dict[str, str] = {}

        def _walk(name: str, node: Any) -> None:
            if isinstance(node, dict):
                directory = node.get("directory") if isinstance(node.get("directory"), dict) else node
                raw_phone = (
                    directory.get("phone")
                    or directory.get("phone_number")
                    or node.get("phone")
                    or node.get("phone_number")
                )
                phone = normalize_phone_e164(raw_phone if isinstance(raw_phone, str) else None)
                if phone and name:
                    found[_normalize_name(name)] = phone
                notes = node.get("notes")
                if isinstance(notes, str) and name and name not in found:
                    match = re.search(r"(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})", notes)
                    if match:
                        note_phone = normalize_phone_e164(match.group(1))
                        if note_phone:
                            found[_normalize_name(name)] = note_phone
                for key, child in node.items():
                    if key in {"directory", "notes", "phone", "phone_number"}:
                        continue
                    if isinstance(child, (dict, list)):
                        _walk(str(key), child)
            elif isinstance(node, list):
                for item in node:
                    item_name = ""
                    if isinstance(item, dict):
                        item_name = str(
                            item.get("name")
                            or item.get("carrier")
                            or item.get("text")
                            or name
                            or ""
                        )
                    _walk(item_name, item)

        _walk("", payload)
        return found


class StreetSmartDirectoryHook(DirectorySource):
    """Documented hook for the live StreetSmart / EZLynx carrier directory.

    Production on hermes-poc-01 should not depend on a laptop Chrome crawl.
    Operators can drop a JSON export at ``STREETSMART_VOICE_DIRECTORY_JSON``
    (or the default path below). A future in-process portal scrape can replace
    this class without changing the dispatcher lookup API.

    Expected shapes (any one is accepted):
    - ``{"phones": {"Carrier Name": "+18005551212"}}``
    - ``{"Carrier Name": {"directory": {"phone": "800-555-1212"}}}``
    """

    name = "streetsmart_directory_hook"
    DEFAULT_EXPORT_PATH = BASE_DIR / "data" / "streetsmart_voice_directory_export.json"

    def __init__(self, export_path: Optional[Path] = None):
        env_path = os.getenv("STREETSMART_VOICE_DIRECTORY_JSON")
        if export_path is not None:
            self.export_path = Path(export_path)
        elif env_path:
            self.export_path = Path(env_path)
        else:
            self.export_path = self.DEFAULT_EXPORT_PATH

    def scrape(self) -> Dict[str, str]:
        if not self.export_path.exists():
            logger.info(
                "StreetSmart directory hook: no export at %s "
                "(set STREETSMART_VOICE_DIRECTORY_JSON to enable).",
                self.export_path,
            )
            return {}
        payload = _load_json(self.export_path)
        phones = _phones_from_payload(payload)
        if phones:
            return phones
        return EzlynxExtractedDirectorySource._extract_phones(payload)


class VoiceCallDirectory:
    """Load / persist / look up carrier phones from the server store."""

    def __init__(
        self,
        runtime_path: Optional[Path] = None,
        seed_path: Optional[Path] = None,
        hardcoded: Optional[Dict[str, str]] = None,
    ):
        self.runtime_path = Path(runtime_path) if runtime_path is not None else RUNTIME_DIRECTORY_PATH
        self.seed_path = Path(seed_path) if seed_path is not None else SEED_DIRECTORY_PATH
        self.hardcoded = dict(hardcoded) if hardcoded is not None else dict(HARDCODED_CARRIER_PHONES)
        self._runtime_phones: Optional[Dict[str, str]] = None
        self._seed_phones: Optional[Dict[str, str]] = None

    def runtime_phones(self) -> Dict[str, str]:
        if self._runtime_phones is None:
            self._runtime_phones = _phones_from_payload(_load_json(self.runtime_path))
        return self._runtime_phones

    def seed_phones(self) -> Dict[str, str]:
        if self._seed_phones is None:
            loaded = _phones_from_payload(_load_json(self.seed_path))
            self._seed_phones = loaded or dict(self.hardcoded)
        return self._seed_phones

    def lookup(self, carrier_name: Optional[str]) -> Optional[str]:
        """Stored directory first, then seed, then hardcoded map."""
        if not carrier_name:
            return None
        for mapping in (self.runtime_phones(), self.seed_phones(), self.hardcoded):
            hit = _match_phone(mapping, carrier_name)
            if hit:
                return hit
        return None

    def replace_runtime(self, phones: Dict[str, str], sources: Optional[List[str]] = None) -> Path:
        """Atomically write the gitignored runtime store."""
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources or [],
            "phones": {k: v for k, v in sorted(phones.items()) if k and v},
        }
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.runtime_path.with_suffix(self.runtime_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        tmp_path.replace(self.runtime_path)
        self._runtime_phones = dict(payload["phones"])
        logger.info(
            "Wrote %s voice directory entries to %s",
            len(payload["phones"]),
            self.runtime_path,
        )
        return self.runtime_path

    def refresh(self, sources: Optional[Iterable[DirectorySource]] = None) -> Dict[str, Any]:
        """Merge all scrape sources into the runtime store (later sources win)."""
        active_sources = list(sources) if sources is not None else default_directory_sources()
        merged: Dict[str, str] = {}
        source_names: List[str] = []
        counts: Dict[str, int] = {}
        for source in active_sources:
            try:
                scraped = source.scrape() or {}
            except Exception as exc:
                logger.warning("Directory source %s failed: %s", getattr(source, "name", source), exc)
                continue
            source_names.append(source.name)
            counts[source.name] = len(scraped)
            for name, phone in scraped.items():
                merged[_normalize_name(name)] = phone
        path = self.replace_runtime(merged, sources=source_names)
        return {
            "path": str(path),
            "count": len(merged),
            "sources": source_names,
            "source_counts": counts,
        }


def default_directory_sources(
    seed_path: Optional[Path] = None,
    extracted_paths: Optional[Iterable[Path]] = None,
    streetsmart_export: Optional[Path] = None,
) -> List[DirectorySource]:
    return [
        KnownCarrierPhonesSeedSource(seed_path=seed_path),
        EzlynxExtractedDirectorySource(search_paths=extracted_paths),
        StreetSmartDirectoryHook(export_path=streetsmart_export),
    ]


_DEFAULT_DIRECTORY: Optional[VoiceCallDirectory] = None


def get_default_directory() -> VoiceCallDirectory:
    global _DEFAULT_DIRECTORY
    if _DEFAULT_DIRECTORY is None:
        _DEFAULT_DIRECTORY = VoiceCallDirectory()
    return _DEFAULT_DIRECTORY


def lookup_carrier_phone(
    carrier_name: Optional[str],
    directory: Optional[VoiceCallDirectory] = None,
) -> Optional[str]:
    """Public lookup used by the Robie Call dispatcher."""
    store = directory or get_default_directory()
    return store.lookup(carrier_name)


def _producers_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("producers") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def load_voice_call_directory(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load runtime store, then committed seed (phones + producers)."""
    if path is not None:
        loaded = _load_json(path)
        if loaded:
            return loaded
    live = _load_json(RUNTIME_DIRECTORY_PATH)
    if live:
        return live
    return _load_json(SEED_DIRECTORY_PATH)


def list_producers(directory: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if directory is not None:
        found = _producers_from_payload(directory)
        if found:
            return found
    runtime = _producers_from_payload(_load_json(RUNTIME_DIRECTORY_PATH))
    if runtime:
        return runtime
    seeded = _producers_from_payload(_load_json(SEED_DIRECTORY_PATH))
    if seeded:
        return seeded
    # Last resort: StreetSmart producers already in the hardcoded phone map.
    return [
        {"name": "Jake Ferrara", "email": "jake@streetsmart.insurance", "phone": "+17326688161", "aliases": ["Jake", "Ferrara, Jake"]},
        {"name": "Carlo Ferrara", "email": "carlo@streetsmart.insurance", "phone": "+17329953409", "aliases": ["Carlo", "Buster Brown", "Ferrara, Carlo"]},
        {"name": "Jimmy", "phone": "+17329954324", "aliases": ["Jimmy"]},
    ]


def _normalize_person_key(value: str) -> str:
    cleaned = re.sub(r"[.,]", " ", value.lower())
    parts = [p for p in cleaned.split() if p]
    if 1 < len(parts) <= 4:
        return " ".join(sorted(parts))
    return " ".join(parts)


def _producer_keys(entry: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for raw in [entry.get("name"), entry.get("email"), *(entry.get("aliases") or [])]:
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        keys.append(_normalize_person_key(text))
        if "@" in text:
            keys.append(text.lower())
    return keys


def lookup_producer(
    name: Optional[str] = None,
    email: Optional[str] = None,
    directory: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a StreetSmart producer to display name + optional E.164 phone.

    Matching uses the EZLynx Producer field (name) and/or email against the
    voice directory. Returns None when no directory row matches.
    """
    producers = list_producers(directory)
    if not producers:
        return None

    candidates: List[str] = []
    if name and str(name).strip():
        candidates.append(_normalize_person_key(str(name).strip()))
    if email and str(email).strip():
        candidates.append(str(email).strip().lower())
    candidates = [c for c in candidates if c]
    if not candidates:
        return None

    exact_hits: List[Dict[str, Any]] = []
    for entry in producers:
        keys = _producer_keys(entry)
        if any(c in keys for c in candidates):
            exact_hits.append(entry)

    chosen = exact_hits[0] if len(exact_hits) == 1 else None
    if chosen is None and exact_hits:
        return None

    if chosen is None:
        token_hits: List[Dict[str, Any]] = []
        tokens = set()
        for raw in (name, email):
            if raw:
                tokens.update(p for p in re.sub(r"[.,@]", " ", str(raw).lower()).split() if p)
        for entry in producers:
            keys = set(_producer_keys(entry))
            key_tokens = set()
            for key in keys:
                key_tokens.update(key.split())
            distinctive = {t for t in tokens if t not in {"insurance", "streetsmart", "com", "ferrara"}}
            if distinctive and distinctive & key_tokens:
                token_hits.append(entry)
        unique = []
        seen = set()
        for hit in token_hits:
            mark = (hit.get("name") or "").lower()
            if mark in seen:
                continue
            seen.add(mark)
            unique.append(hit)
        if len(unique) == 1:
            chosen = unique[0]

    if chosen is None:
        return None

    phone = normalize_phone_e164(chosen.get("phone"))
    return {
        "name": chosen.get("name") or name,
        "email": chosen.get("email") or email,
        "phone": phone,
        "aliases": list(chosen.get("aliases") or []),
    }


def refresh_voice_call_directory(
    directory: Optional[VoiceCallDirectory] = None,
    sources: Optional[Iterable[DirectorySource]] = None,
) -> Dict[str, Any]:
    store = directory or get_default_directory()
    return store.refresh(sources=sources)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap or refresh the server-side Robie Call phone directory."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Scrape configured sources and write data/voice_call_directory.json.",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Write the runtime store from the committed seed / hardcoded map only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without updating the runtime store.",
    )
    parser.add_argument(
        "--runtime-path",
        default=None,
        help="Override runtime JSON path (tests / alternate data dir).",
    )
    parser.add_argument(
        "--seed-path",
        default=None,
        help="Override seed JSON path.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    store = VoiceCallDirectory(
        runtime_path=Path(args.runtime_path) if args.runtime_path else None,
        seed_path=Path(args.seed_path) if args.seed_path else None,
    )
    if args.bootstrap and not args.refresh:
        sources: List[DirectorySource] = [KnownCarrierPhonesSeedSource(seed_path=store.seed_path)]
    else:
        sources = default_directory_sources(seed_path=store.seed_path)
        if not args.refresh and not args.bootstrap:
            print("Provide --refresh (weekly job) or --bootstrap (one-time seed).")
            return 2

    if args.dry_run:
        merged: Dict[str, str] = {}
        source_names = []
        for source in sources:
            scraped = source.scrape() or {}
            source_names.append(source.name)
            merged.update({_normalize_name(k): v for k, v in scraped.items()})
        print(json.dumps({
            "dry_run": True,
            "would_write": str(store.runtime_path),
            "count": len(merged),
            "sources": source_names,
        }, indent=2))
        return 0

    result = store.refresh(sources=sources)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
