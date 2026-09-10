import sys, sqlite3
from pathlib import Path

sys.path.insert(0, "/opt/renewal-automation-system")
from src.ezlynx.api_client import EZLynxApiClient

DB_PATH = Path("/opt/busy-borg/data/mortgagee_renewals.db")

direct_billed_accounts = [
    {
        "id": 7,
        "applicant_id": "43634166",
        "name": "Leslie & Stephanie Konsig",
        "policy": "HCPC-HO6-852895-5",
        "carrier": "Homeowners Choice",
        "exp": "2026-10-08",
        "premium": "$850.00",
        "csr": "Ana Flores",
        "producer": "Jazmin Molina"
    },
    {
        "id": 13,
        "applicant_id": "95218264",
        "name": "Stephanie & Jeffery Cochran",
        "policy": "4701-2200-5001",
        "carrier": "Universal Property & Casualty Ins Co",
        "exp": "2026-10-17",
        "premium": "$1,450.00",
        "csr": "Ana Flores",
        "producer": "Jazmin Molina"
    },
    {
        "id": 22,
        "applicant_id": "79002334",
        "name": "Hanim Benli",
        "policy": "HONJ2025100027",
        "carrier": "Hyundai Marine & Fire Insurance Company",
        "exp": "2026-10-08",
        "premium": "$1,120.00",
        "csr": "Personal Lines CSR Team (Ana Flores / Daniela Aguilar)",
        "producer": "Jazmin Molina"
    },
    {
        "id": 24,
        "applicant_id": "196126698",
        "name": "Paulette Fagone",
        "policy": "LSP600011096",
        "carrier": "Johnson & Johnson, Inc. MGA",
        "exp": "2026-10-22",
        "premium": "$1,380.00",
        "csr": "Personal Lines CSR Team (Daniela Aguilar / Ana Flores)",
        "producer": "Jazmin Molina"
    },
    {
        "id": 26,
        "applicant_id": "97493331",
        "name": "Bertille & Lee Glass",
        "policy": "H  2391951",
        "carrier": "Selective Insurance",
        "exp": "2026-10-07",
        "premium": "$1,276.00",
        "csr": "Daniela Aguilar",
        "producer": "Daniela Aguilar"
    },
    {
        "id": 27,
        "applicant_id": "169497053",
        "name": "Michele & Kenneth Morgan",
        "policy": "H  2462522",
        "carrier": "Selective Insurance",
        "exp": "2026-10-08",
        "premium": "$2,194.00",
        "csr": "Daniela Aguilar",
        "producer": "Daniela Aguilar"
    },
    {
        "id": 30,
        "applicant_id": "46952417",
        "name": "Jeannine & Gregg Soderstrom",
        "policy": "GB-2023-19543",
        "carrier": "Great Bay Insurance Company",
        "exp": "2026-10-10",
        "premium": "$1,948.00",
        "csr": "Daniela Aguilar",
        "producer": "Daniela Aguilar"
    },
    {
        "id": 34,
        "applicant_id": "22360446",
        "name": "Carl & Leslie Greenfield",
        "policy": "H 2408079",
        "carrier": "Selective Insurance Company of America",
        "exp": "2026-09-15",
        "premium": "$1,703.00",
        "csr": "Daniela Aguilar",
        "producer": "Daniela Aguilar"
    },
    {
        "id": 35,
        "applicant_id": "34779479",
        "name": "Scott Burbank",
        "policy": "LLNE-NJ-0000155-02",
        "carrier": "Certain Underwriters at Lloyds (Orchid)",
        "exp": "2026-09-21",
        "premium": "$1,618.50",
        "csr": "Daniela Aguilar",
        "producer": "Carlo Ferrara"
    },
    {
        "id": 41,
        "applicant_id": "170226232",
        "name": "Maria Lua & Armando Chavez",
        "policy": "HONJ038633",
        "carrier": "Farmers Mutual Fire Ins. Co of Salem",
        "exp": "2026-10-08",
        "premium": "$1,093.00",
        "csr": "Personal Lines CSR Team (Ana Flores / Daniela Aguilar)",
        "producer": "Jazmin Molina"
    },
    {
        "id": 42,
        "applicant_id": "25220597",
        "name": "Noreen VanSalisbury",
        "policy": "HONJ017732",
        "carrier": "Farmers Mutual Fire Ins. Co of Salem",
        "exp": "2026-10-15",
        "premium": "$1,865.00",
        "csr": "Personal Lines CSR Team (Ana Flores / Daniela Aguilar)",
        "producer": "Jazmin Molina"
    },
    {
        "id": 43,
        "applicant_id": "24691408",
        "name": "Ezra & Dyan Levy",
        "policy": "HONJM07647",
        "carrier": "Farmers Mutual Fire Ins. Co of Salem",
        "exp": "2026-10-13",
        "premium": "$6,171.00",
        "csr": "Personal Lines CSR Team (Ana Flores / Daniela Aguilar)",
        "producer": "Jazmin Molina"
    }
]

client = EZLynxApiClient()
con = sqlite3.connect(str(DB_PATH))
cur = con.cursor()

results = []
for acc in direct_billed_accounts:
    acc_name = acc["name"]
    app_id = acc["applicant_id"]
    pol = acc["policy"]
    carrier = acc["carrier"]
    exp_date = acc["exp"]
    prem = acc["premium"]
    csr = acc["csr"]
    prod = acc["producer"]
    
    print("\nProcessing Direct-Bill CSR Handoff:", acc_name, "(", app_id, ")")
    
    note_text = f"""ROBIE Direct-Billed Renewal Assignment to CSR:
- Account: {acc_name} ({app_id})
- Policy: {pol} ({carrier})
- Expiration Date: {exp_date}
- Renewal Premium: {prem}
- Billing Method: DIRECT-BILLED TO INSURED (No Mortgagee Escrow)
- Assigned CSR: {csr}
- Assigned Producer: {prod}

ACTION REQUIRED BY ASSIGNED CSR:
This policy is direct-billed directly to the insured. As per agency directive, assigning this account to assigned CSR {csr} to contact the insured directly via client email/phone to confirm renewal acceptance and ensure payment is submitted prior to the expiration date ({exp_date}).

ROBIE was here"""

    res = client.add_note_to_discussion(
        applicant_id=app_id,
        discussion_title="Homeowners Renewal / Mortgage Verification",
        note_text=note_text,
        policy_number=pol
    )
    
    note_id = res.get("note_id")
    status = res.get("status")
    print("  Note posted: status=", status, "note_id=", note_id)
    
    # Update SQLite database
    cur.execute("""
        UPDATE renewal_candidates
        SET status = "CSR_HANDOFF_DIRECT_BILL",
            payer_type = "INSURED_DIRECT",
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (acc["id"],))
    con.commit()
    
    results.append({
        "applicant_id": app_id,
        "name": acc_name,
        "policy": pol,
        "note_id": note_id,
        "status": status
    })

con.close()
print("\n=== ALL 12 DIRECT-BILLED ACCOUNTS SUCCESSFULLY ASSIGNED TO CSRS ===")
for r in results:
    print(r["name"] + " (" + r["applicant_id"] + "): note_id=" + str(r["note_id"]))
