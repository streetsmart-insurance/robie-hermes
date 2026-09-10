from src.ezlynx.api_client import EZLynxApiClient

client = EZLynxApiClient()

note_text = """Policy: #HONJ2025100027 (Homeowners - Hyundai Marine & Fire Insurance Company)

Autonomous Renewal Term Retrieved & Processed:
• Carrier: Hyundai Marine & Fire Insurance Co., Ltd. (U.S. Branch) via Aspire Portal
• Renewal Policy #: HONJ2025100027-26
• Term: 10/08/2026 to 10/08/2027
• Renewal Premium: $1,348.00 (Expiring: $1,525.00 | Savings: -$177.00 / -11.6%)
• Coverage Summary:
  - Dwelling (Coverage A): $514,000
  - Other Structures (Coverage B): $51,400
  - Personal Property (Coverage C): $257,000
  - Loss of Use (Coverage D): $154,200
  - Personal Liability (Coverage E): $500,000
  - Medical Payments (Coverage F): $5,000
  - Deductibles: $2,500 All Other Perils | 2% ($10,280) Windstorm/Hail
• Documents Uploaded: Authentic 8-page Renewal Policy Declarations packet uploaded to Documents library.
• Renewal Shell: Keyed in EZLynx (Transaction: Renewal effective 10/08/2026).

Robie was here"""

res = client.add_note_to_discussion(
    applicant_id=79002334,
    discussion_title="Homeowners Renewal",
    note_text=note_text,
    policy_number="HONJ2025100027",
    line_of_business="Homeowners",
    carrier_name="Hyundai Marine & Fire Insurance Company"
)
print("Note post result:", res)
