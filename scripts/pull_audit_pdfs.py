#!/usr/bin/env python3
"""Pull selected EZLynx Document Library PDFs via Classic API into handoffs/audit_pdfs."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import requests

from src.ezlynx.api_client import EZLynxApiClient

OUT = Path("/opt/renewal-automation-system/data/handoffs/audit_pdfs")

# From compact dump — attachable candidates only
PULL = {
    "30438515_Diamond": [
        ("E9PR7t3G8oZzInvoUKLsUx5doZRmGOa1LzhkNp9E6F_ws7Ontfi_CfUZkw-vKyi50", "Final_Audit-133728633.pdf"),
        ("CBPD2W6fOf0TaWRyPHUdO3m8qgskyt9KigQGf0AEwozws7Ontfi_CfUZkw-vKyi50", "Workers_Compensation_Insurance_audit.pdf"),
        ("ASEMOytZF-XeV1YogJcNdfxHTrUAUhCGGgPZbOKR4dvws7Ontfi_CfUZkw-vKyi50", "FINAL_AUDIT_COMPLETED.pdf"),
    ],
    "38245246_Realty": [
        ("24SG1lljHyTZO4oq6iWt_S9Bz6SSMBw-_9RYQ7sdMPHws7Ontfi_CfUZkw-vKyi50", "Audit_Dispute_Results.pdf"),
        ("tahH6Z0hGAWlxa8Twfxbfl-oI6eu54TGlz9v4JZJA3vws7Ontfi_CfUZkw-vKyi50", "Audit_Worksheet.pdf"),
        ("poRQx2zqviUowvDgdmDJOJi3IjTcl0_P5BUyiH_I4Svws7Ontfi_CfUZkw-vKyi50", "Travelers_Final_Audit-Policy_2E388509_07-23-2024_TO_07-23-2025.pdf"),
        # skip BOR/quote/app *_audit.pdf unless needed — not audit letters
    ],
}


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


def extract_bytes(payload) -> bytes:
    """Classic documentlibrary/{id} often returns JSON with base64 Content/FileContents."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        # maybe raw base64
        try:
            return base64.b64decode(payload)
        except Exception:
            return payload.encode("utf-8", errors="replace")
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected payload type {type(payload)}")

    # common keys
    for key in (
        "Content",
        "content",
        "FileContents",
        "fileContents",
        "Data",
        "data",
        "Base64",
        "base64",
        "DocumentContent",
        "FileData",
        "Bytes",
    ):
        if key in payload and payload[key]:
            val = payload[key]
            if isinstance(val, str):
                # strip data URI
                if val.startswith("data:") and "," in val:
                    val = val.split(",", 1)[1]
                return base64.b64decode(val)
            if isinstance(val, list):
                return bytes(val)
    # nested
    for nest in ("Document", "document", "File", "file", "Result", "result"):
        if nest in payload and isinstance(payload[nest], dict):
            try:
                return extract_bytes(payload[nest])
            except Exception:
                pass
    raise ValueError("no base64 body; keys=" + ",".join(payload.keys()))


def main() -> None:
    api = EZLynxApiClient()
    assert api.authenticate_classic()
    headers = api._get_classic_headers()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for folder, docs in PULL.items():
        dest = OUT / folder
        dest.mkdir(parents=True, exist_ok=True)
        for doc_id, filename in docs:
            url = f"{api.services_url}/documentlibrary/{doc_id}"
            r = requests.get(url, headers=headers, timeout=60)
            entry = {
                "folder": folder,
                "doc_id": doc_id,
                "filename": filename,
                "http": r.status_code,
                "content_type": r.headers.get("content-type"),
            }
            if r.status_code != 200:
                entry["error"] = r.text[:300]
                manifest.append(entry)
                print("FAIL", folder, filename, r.status_code)
                continue
            ct = (r.headers.get("content-type") or "").lower()
            raw = r.content
            out_path = dest / safe_name(filename)
            try:
                if "application/pdf" in ct or raw[:4] == b"%PDF":
                    data = raw
                else:
                    payload = r.json()
                    entry["json_keys"] = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
                    # print small peek
                    if isinstance(payload, dict):
                        print("KEYS", folder, filename, list(payload.keys())[:20])
                        # Headers may indicate content type; body elsewhere
                        for k, v in list(payload.items())[:8]:
                            if isinstance(v, str) and len(v) > 80:
                                print(" ", k, "len", len(v), "head", v[:40])
                            else:
                                print(" ", k, v if not isinstance(v, (dict, list)) else type(v))
                    data = extract_bytes(payload)
                out_path.write_bytes(data)
                entry["path"] = str(out_path)
                entry["bytes"] = len(data)
                entry["pdf_magic"] = data[:4] == b"%PDF"
                print("OK", out_path, len(data), "pdf" if data[:4] == b"%PDF" else "not-pdf")
            except Exception as e:
                # save raw for debug
                raw_path = dest / (safe_name(filename) + ".raw.json")
                raw_path.write_bytes(raw)
                entry["error"] = str(e)
                entry["raw_saved"] = str(raw_path)
                print("ERR", folder, filename, e)
            manifest.append(entry)
    man_path = OUT / "manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2))
    print("MANIFEST", man_path)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
