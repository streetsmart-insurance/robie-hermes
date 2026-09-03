"""Ingests daily renewal reports (CSV/Excel/JSON) and syncs expiring policies into the database."""

import csv
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Any, Dict
from sqlalchemy.orm import Session

from src.config import settings
from src.database.models import PolicyRenewal, RenewalStatus, ActionType, AuditNoteLog
from src.intake.base_source import BaseRenewalSource, RawRenewalItem

logger = logging.getLogger("renewal_intake")

class ReportIngestor(BaseRenewalSource):
    """Parses daily EZLynx renewal export files and ingests policies due for renewal."""

    def __init__(self, input_dir: Optional[Path] = None):
        self.input_dir = input_dir or settings.input_reports_path

    def _parse_date(self, val: Any) -> Optional[date]:
        if not val:
            return None
        if isinstance(val, date):
            return val
        if isinstance(val, datetime):
            return val.date()
        s = str(val).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def _parse_float(self, val: Any) -> Optional[float]:
        if val is None or val == "":
            return None
        s = str(val).replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    def _derive_discussion_title(self, lob: str) -> str:
        lob_lower = (lob or "").lower()
        if "commercial auto" in lob_lower:
            return "Manual Commercial Auto Renewal"
        elif "commercial pkg" in lob_lower or "commercial package" in lob_lower or "bop" in lob_lower:
            return "Manual Commercial Package Renewal"
        elif "commercial property" in lob_lower:
            return "Manual Commercial Property Renewal"
        elif "commercial" in lob_lower or "liability" in lob_lower or "gl" in lob_lower:
            return "Manual Commercial Renewal"
        elif "home" in lob_lower or "dwelling" in lob_lower:
            return "Manual Homeowners Renewal"
        elif "auto" in lob_lower or "car" in lob_lower:
            return "Manual Auto Renewal"
        elif "flood" in lob_lower:
            return "Manual Flood Renewal"
        elif "umbrella" in lob_lower:
            return "Manual Umbrella Renewal"
        return f"Manual {lob} Renewal" if lob else "Manual Policy Renewal"

    def parse_csv_file(self, file_path: Path) -> List[RawRenewalItem]:
        """Parses a CSV file containing renewal records."""
        items: List[RawRenewalItem] = []
        with open(file_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalize keys (strip whitespace, lowercase)
                clean_row = {k.strip().lower().replace(" ", "_"): v for k, v in row.items() if k}

                # 1. Check Source column if present (skip electronic/IVANS downloads)
                raw_source = str(
                    clean_row.get("source") or 
                    clean_row.get("policy_source") or 
                    clean_row.get("download_status") or 
                    clean_row.get("download") or 
                    ""
                ).strip().lower()

                if raw_source in ("download", "downloaded", "ivans", "edocs", "edocs & messages", "edocs_&_messages", "yes", "true"):
                    # Electronic download policy - skip for manual renewal outreach
                    continue

                pol_num = (
                    clean_row.get("policynumber") or 
                    clean_row.get("policy_number") or 
                    clean_row.get("policy_#") or 
                    clean_row.get("policy_no") or 
                    clean_row.get("pol_num") or 
                    clean_row.get("policy")
                )
                insured = (
                    clean_row.get("account_name") or 
                    clean_row.get("insured_name") or 
                    clean_row.get("named_insured") or 
                    clean_row.get("customer_name") or 
                    clean_row.get("applicant_name") or 
                    clean_row.get("client_name") or 
                    clean_row.get("applicant")
                )
                app_id = (
                    clean_row.get("applicant_id") or 
                    clean_row.get("applicantid") or 
                    clean_row.get("ezlynx_id") or 
                    clean_row.get("client_id") or 
                    clean_row.get("id")
                )
                carrier = (
                    clean_row.get("master_company") or 
                    clean_row.get("writing_company") or 
                    clean_row.get("carrier_name") or 
                    clean_row.get("carrier") or 
                    clean_row.get("company")
                )
                exp_date = self._parse_date(
                    clean_row.get("expiration_date") or 
                    clean_row.get("exp_date") or 
                    clean_row.get("expiration")
                )

                if not (pol_num and insured and carrier and exp_date):
                    continue

                # Fallback applicant ID if missing in report
                if not app_id:
                    app_id = f"EZL-POL-{pol_num}"

                eff_date = self._parse_date(clean_row.get("effective_date") or clean_row.get("eff_date"))
                premium = self._parse_float(
                    clean_row.get("premium_annualized") or 
                    clean_row.get("annualized_premium") or 
                    clean_row.get("premium_full_term") or 
                    clean_row.get("premium_written") or 
                    clean_row.get("written_premium") or 
                    clean_row.get("expiring_premium") or 
                    clean_row.get("premium") or 
                    clean_row.get("prior_premium")
                )
                lob = clean_row.get("line_of_business") or clean_row.get("lob") or clean_row.get("policy_type") or "Homeowners"
                disc_title = clean_row.get("discussion_title") or self._derive_discussion_title(lob)
                uw_name = clean_row.get("underwriter_name") or clean_row.get("underwriter")
                uw_email = clean_row.get("underwriter_email") or clean_row.get("email")
                agent = clean_row.get("assigned_agent") or clean_row.get("agent") or clean_row.get("csr")
                # Exclude known download policies
                if str(pol_num).strip() in ["13WECAN7A7K"]:
                    continue

                from src.portals.carrier_routing import CarrierRoutingMatrix
                routing = CarrierRoutingMatrix.get_carrier_config(str(carrier).strip())
                if routing.get("channel") == "PORTAL":
                    portal_supp = True
                    portal_url = routing.get("portal_url") or portal_url

                items.append(RawRenewalItem(
                    policy_number=str(pol_num).strip(),
                    insured_name=str(insured).strip(),
                    applicant_id=str(app_id).strip(),
                    carrier_name=str(carrier).strip(),
                    line_of_business=str(lob).strip(),
                    discussion_title=str(disc_title).strip(),
                    expiration_date=exp_date,
                    effective_date=eff_date,
                    expiring_premium=premium,
                    underwriter_name=str(uw_name).strip() if uw_name else None,
                    underwriter_email=str(uw_email).strip() if uw_email else routing.get("underwriter_email"),
                    assigned_agent=str(agent).strip() if agent else None,
                    portal_supported=portal_supp,
                    portal_url=str(portal_url).strip() if portal_url else None
                ))
        return items

    def fetch_renewals(self, target_date: Optional[date] = None) -> List[RawRenewalItem]:
        """Scans input directory for all CSVs and returns parsed items."""
        all_items: List[RawRenewalItem] = []
        csv_files = list(self.input_dir.glob("*.csv"))
        for f in csv_files:
            try:
                parsed = self.parse_csv_file(f)
                all_items.extend(parsed)
            except Exception as e:
                logger.error(f"Error reading renewal report {f}: {e}")
        return all_items

    def sync_to_database(
        self,
        db: Session,
        reference_date: Optional[date] = None
    ) -> Tuple[int, int]:
        """
        Filters raw renewals to only those within the 30-45 day window and saves to DB.
        Returns: (new_added_count, total_in_window_count)
        """
        ref_date = reference_date or date.today()
        window_start = ref_date + timedelta(days=settings.renewal_window_min_days)
        window_end = ref_date + timedelta(days=settings.renewal_window_max_days)

        raw_items = self.fetch_renewals(ref_date)
        new_count = 0
        in_window_count = 0

        for item in raw_items:
            # Check 30-45 day window
            if not (window_start <= item.expiration_date <= window_end):
                continue

            in_window_count += 1

            # Check if policy already exists for this term
            existing = db.query(PolicyRenewal).filter(
                PolicyRenewal.policy_number == item.policy_number,
                PolicyRenewal.expiration_date == item.expiration_date
            ).first()

            if not existing:
                policy = PolicyRenewal(
                    policy_number=item.policy_number,
                    insured_name=item.insured_name,
                    applicant_id=item.applicant_id,
                    discussion_title=item.discussion_title,
                    line_of_business=item.line_of_business,
                    carrier_name=item.carrier_name,
                    effective_date=item.effective_date,
                    expiration_date=item.expiration_date,
                    expiring_premium=item.expiring_premium,
                    underwriter_name=item.underwriter_name,
                    underwriter_email=item.underwriter_email,
                    assigned_agent=item.assigned_agent,
                    portal_supported=item.portal_supported,
                    portal_url=item.portal_url,
                    status=RenewalStatus.PENDING_EVALUATION
                )
                db.add(policy)
                db.flush()

                # Log initial discovery note
                note = AuditNoteLog(
                    policy_id=policy.id,
                    applicant_id=policy.applicant_id,
                    discussion_title=policy.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=(
                        f"[AUTOMATION INTAKE] Policy identified for manual renewal review.\n"
                        f"• Carrier: {policy.carrier_name}\n"
                        f"• Expiring: {policy.expiration_date} (in {(policy.expiration_date - ref_date).days} days)\n"
                        f"• Expiring Premium: ${policy.expiring_premium:,.2f}" if policy.expiring_premium else ""
                    ).strip()
                )
                db.add(note)
                new_count += 1

        db.commit()
        return new_count, in_window_count

    def generate_sample_report_if_empty(self, reference_date: Optional[date] = None) -> Path:
        """Creates a sample daily renewal CSV for testing & demonstration."""
        ref_date = reference_date or date.today()
        # Check if any report already exists in directory
        existing = list(self.input_dir.glob("*.csv")) + list(self.input_dir.glob("*.xlsx"))
        if existing:
            return existing[0]

        self.input_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "policy_number": "HO3-982341-NY",
                "insured_name": "Jonathan Vance",
                "applicant_id": "EZL-884920",
                "carrier_name": "Travelers Insurance",
                "line_of_business": "Homeowners",
                "discussion_title": "Manual Homeowners Renewal",
                "effective_date": (ref_date - timedelta(days=330)).strftime("%Y-%m-%d"),
                "expiration_date": (ref_date + timedelta(days=35)).strftime("%Y-%m-%d"),
                "expiring_premium": "1850.00",
                "underwriter_name": "Sarah Miller",
                "underwriter_email": "sarah.miller@travelers.com",
                "assigned_agent": "Carlo Ferrara",
                "portal_supported": "True",
                "portal_url": "https://agent.travelers.com"
            },
            {
                "policy_number": "HO3-441908-FL",
                "insured_name": "Elena Rostova",
                "applicant_id": "EZL-903144",
                "carrier_name": "Universal Property",
                "line_of_business": "Homeowners",
                "discussion_title": "Manual Homeowners Renewal",
                "effective_date": (ref_date - timedelta(days=325)).strftime("%Y-%m-%d"),
                "expiration_date": (ref_date + timedelta(days=40)).strftime("%Y-%m-%d"),
                "expiring_premium": "2450.00",
                "underwriter_name": "Mark Davis",
                "underwriter_email": "mdavis@universalproperty.com",
                "assigned_agent": "Carlo Ferrara",
                "portal_supported": "False",
                "portal_url": ""
            },
            {
                "policy_number": "CAP-771239-NJ",
                "insured_name": "Apex Logistic Solutions",
                "applicant_id": "EZL-512049",
                "carrier_name": "Liberty Mutual Commercial",
                "line_of_business": "Commercial Auto",
                "discussion_title": "Manual Commercial Auto Renewal",
                "effective_date": (ref_date - timedelta(days=328)).strftime("%Y-%m-%d"),
                "expiration_date": (ref_date + timedelta(days=37)).strftime("%Y-%m-%d"),
                "expiring_premium": "6800.00",
                "underwriter_name": "Rachel Adams",
                "underwriter_email": "rachel.adams@libertymutual.com",
                "assigned_agent": "Carlo Ferrara",
                "portal_supported": "True",
                "portal_url": "https://business.libertymutual.com"
            }
        ]

        with open(sample_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        return sample_path
