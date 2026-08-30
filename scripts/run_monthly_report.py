#!/usr/bin/env python3
from robie_job_engine.reporting_suite import ReportingSuite

def main():
    suite = ReportingSuite()
    kpis = {
        "month_label": "August 2026",
        "holistic_score": 76.4,
        "grade": "B-",
        "total_calls": 12353,
        "answer_rate": "51.8%",
        "active_policies": 1420,
        "retention_rate": "92.4%",
        "total_cancellations": 4
    }
    autopsies = [
        {
            "client_name": "Costa 1 Cleaning Services (Costa Morales)",
            "account_id": "132780296",
            "policy_type": "Utica First BOP ($952.85/yr)",
            "root_cause": "16m 55s hold time on Jackie offline transfer + 4-rep handoff confusion leading to cancellation form sent by Sandy.",
            "responsible_reps": ["Jackie Arriola", "Sandy Santana"],
            "preventable": True
        },
        {
            "client_name": "Piotr Gusciora",
            "account_id": "8492019",
            "policy_type": "Commercial Auto / GL",
            "root_cause": "3 unreturned voicemails (4m detailed message) ignored by Jackie Arriola over 48 hours.",
            "responsible_reps": ["Jackie Arriola"],
            "preventable": True
        },
        {
            "client_name": "Global Spray Co",
            "account_id": "9102934",
            "policy_type": "Commercial Package",
            "root_cause": "Multiple urgent COI / quote requests missed by Angie Valladarez and Commercial Queue.",
            "responsible_reps": ["Angie Valladarez"],
            "preventable": True
        },
        {
            "client_name": "Kyoungnam Moon",
            "account_id": "7729104",
            "policy_type": "Commercial Trucking",
            "root_cause": "50 inbound dials and 6 voicemails left with zero outbound return calls placed.",
            "responsible_reps": ["Ricardo Aguilar", "Mike Sosa"],
            "preventable": True
        }
    ]
    report = suite.build_monthly_report(monthly_kpis=kpis, churn_autopsies=autopsies)
    print(report)

if __name__ == "__main__":
    main()
