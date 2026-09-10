import sqlite3
import datetime

db_path = "/opt/busy-borg/data/mortgagee_renewals.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

updates = [
    # (candidate_id, discussion_id, discussion_title, status)
    (1, "841126647", "High Risk Renewal Review", "WAITING_MORTGAGEE_PAYMENT"),
    (5, "833790700", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (8, "833197379", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (9, "833790710", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (10, "833197380", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (11, "833790723", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (18, "833790737", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (23, "832144272", "Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (25, "833790727", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (36, "833790731", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT"),
    (37, "804687584", "Homeowners Renewal", "WAITING_MORTGAGEE_PAYMENT"),
    (38, "833790740", "Renewal 2026-2027", "WAITING_MORTGAGEE_PAYMENT"),
    (44, "828002838", "Flood Renewal 2026-2027", "WAITING_MORTGAGEE_PAYMENT"),
    (45, "826499829", "Flood Renewal 26- 27", "WAITING_MORTGAGEE_PAYMENT"),
    (46, "832945988", "Homeowners Renewal / Mortgage Verification", "WAITING_MORTGAGEE_PAYMENT")
]

for cid, disc_id, disc_title, status in updates:
    c.execute("""
        UPDATE renewal_candidates
        SET discussion_id = ?, discussion_title = ?, status = ?
        WHERE id = ?
    """, (disc_id, disc_title, status, cid))

# Update mortgagee_details for candidates 44, 45, 46
now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
c.execute("""
    UPDATE mortgagee_details
    SET verification_status = 'DOC_UPLOADED',
        confirmation_number = 'EZLYNX_DOC_LIB_VERIFIED',
        uploaded_at = ?
    WHERE candidate_id IN (44, 45, 46)
""", (now_iso,))

conn.commit()
print("Successfully updated renewal_candidates and mortgagee_details for all 15 candidates.")

# Verify
c.execute("""
    SELECT r.id, r.applicant_id, r.account_name, r.policy_number, r.status, r.discussion_id, r.discussion_title, m.confirmation_number
    FROM renewal_candidates r
    JOIN mortgagee_details m ON r.id = m.candidate_id
    WHERE r.id IN (1, 5, 8, 9, 10, 11, 18, 23, 25, 36, 37, 38, 44, 45, 46)
    ORDER BY r.id
""")
for row in c.fetchall():
    print(row)

conn.close()
