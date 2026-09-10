#!/usr/bin/env python3
"""Verification script for EZLynx Document API integration.

Tests:
1. Modern OAuth2 authentication & DocumentApi scope negotiation.
2. Classic Web Services authentication.
3. Applicant Document Library listing.
4. Document export / downloading with authentic PDF validation.
5. Upload readiness check.
"""

import sys
import json
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("verify_document_api")

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ezlynx.api_client import EZLynxApiClient
from src.config import settings

def main():
    print("=" * 70)
    print("EZLYNX DOCUMENT API INTEGRATION VERIFICATION")
    print(f"Environment: {settings.ezlynx_env}")
    print(f"Connect Token URL: {settings.ezlynx_connect_token_url}")
    print(f"Document API URL: {settings.ezlynx_document_api_url}")
    print("=" * 70)

    client = EZLynxApiClient()

    # Step 1: OAuth2 Authentication & Scope Verification
    print("\n[1/4] Testing Modern OAuth2 Gateway Authentication...")
    oauth_ok = client.authenticate_oauth(force_refresh=True)
    print(f"  -> OAuth Authentication Status: {'SUCCESS' if oauth_ok else 'FAILED'}")
    print(f"  -> Granted OAuth Scopes: {client._oauth_scopes}")
    print(f"  -> DocumentApi Scope Provisioned: {'YES (Direct REST upload enabled)' if client._has_document_scope else 'NO (Pending Applied Systems approval)'}")

    # Step 2: Classic Authentication
    print("\n[2/4] Testing Classic Web Services Authentication...")
    classic_ok = client.authenticate_classic(force_refresh=True)
    print(f"  -> Classic Authentication Status: {'SUCCESS' if classic_ok else 'FAILED'}")
    print(f"  -> Classic Token: {client._classic_token[:15]}..." if client._classic_token else "  -> Classic Token: None")

    # Step 3: Document Library Listing
    test_applicant_id = "21587605"  # StreetSmart internal agency account
    print(f"\n[3/4] Listing Documents for Applicant {test_applicant_id}...")
    listing = client.list_applicant_documents(applicant_id=test_applicant_id, page_index=1, page_size=5)
    if listing.get("status") == "success":
        data = listing.get("data", {})
        total = data.get("TotalRecords", 0)
        docs = data.get("Documents", [])
        print(f"  -> Total Documents on Record: {total}")
        print(f"  -> Retrieved Sample ({len(docs)} documents):")
        for i, d in enumerate(docs, 1):
            doc_id = d.get("Id") or d.get("ID")
            desc = d.get("Description") or "Unknown"
            created = d.get("DateTimeCreated") or "Unknown"
            mime = d.get("MimeType") or "Unknown"
            print(f"     {i}. ID: {doc_id} | Name: {desc} | Created: {created} | Type: {mime}")
    else:
        print(f"  -> Listing Failed: {listing.get('error')}")

    # Step 4: Export / Download Test
    export_dir = Path("/tmp/ezlynx_test_export")
    print(f"\n[4/4] Testing Document Export to {export_dir}...")
    export_res = client.export_applicant_documents(
        applicant_id=test_applicant_id,
        dest_dir=export_dir,
        max_docs=3
    )
    if export_res.get("status") == "success":
        print(f"  -> Export Status: SUCCESS")
        print(f"  -> Exported Manifest: {export_res.get('manifest_path')}")
        print(f"  -> Exported Count: {export_res.get('exported_count')}")
        for item in export_res.get("files", []):
            if item.get("downloaded"):
                print(f"     * Downloaded: {item.get('filename')} ({item.get('size_bytes')} bytes)")
            else:
                print(f"     * Metadata saved: {item.get('description')} (ID: {item.get('id')})")
    else:
        print(f"  -> Export Failed: {export_res.get('error')}")

    print("\n" + "=" * 70)
    print("VERIFICATION COMPLETE")
    print("=" * 70)

if __name__ == "__main__":
    main()
