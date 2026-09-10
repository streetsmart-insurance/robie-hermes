import base64
from pathlib import Path
from src.email_outreach.auth_setup import get_robie_gmail_service
from src.ezlynx.api_client import EZLynxApiClient

svc = get_robie_gmail_service()
ez = EZLynxApiClient()
out_dir = Path("data/downloads/loss_runs")
out_dir.mkdir(parents=True, exist_ok=True)

def find_pdf_parts(parts):
    found = []
    for p in parts:
        fn = p.get("filename", "")
        att_id = p.get("body", {}).get("attachmentId")
        if fn.lower().endswith(".pdf") and att_id:
            found.append((fn, att_id))
        if "parts" in p:
            found.extend(find_pdf_parts(p["parts"]))
    return found

targets = [
    ("1a082ef0108c8590", "145217363", "A23B8960-78760-SSRM NTL", "Edwin_Lema"),
    ("1a082e951e7610df", "84705043", "FINFR17078371-94344-SSRM NTL", "Marek_PKS")
]

for mid, app_id, pol, name in targets:
    msg = svc.users().messages().get(userId="me", id=mid).execute()
    payload = msg.get("payload", {})
    pdf_parts = find_pdf_parts(payload.get("parts", []))
    print(f"{name} ({mid}) has {len(pdf_parts)} PDF parts: {[p[0] for p in pdf_parts]}")
    for fn, att_id in pdf_parts:
        att = svc.users().messages().attachments().get(userId="me", messageId=mid, id=att_id).execute()
        data = base64.urlsafe_b64decode(att["data"])
        dest = out_dir / f"{name}_{fn}"
        dest.write_bytes(data)
        print(f"Saved {dest} ({len(data)} bytes)")
        up = ez.upload_document(
            applicant_id=app_id,
            file_path=dest,
            folder_name="Renewal Offers/Declarations",
            description=f"Trinity Underwriters Loss Runs 2025-2026 - {fn}",
            policy_number=pol,
            doc_type="loss_runs",
            label_to_apply="Loss Runs"
        )
        print(f"{name} upload result: {up.get('status')} via {up.get('method')}")
print("ALL LOSS RUNS UPLOADED!")
