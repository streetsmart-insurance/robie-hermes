"""Ingests daily renewal reports (CSV/Excel/JSON) and syncs expiring policies into the database."""

import csv
import json
import logging
import re
import base64
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Any, Dict
from sqlalchemy.orm import Session

from src.config import settings
from src.database.models import PolicyRenewal, RenewalStatus, ActionType, AuditNoteLog
from src.database.policy_aliases import register_policy_alias, sibling_policy_for_intake
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

    def _normalize_row_keys(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes row dictionary keys by lowercasing and replacing non-alphanumeric chars with underscores."""
        return {re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_"): v for k, v in row.items() if k}

    def _process_row_dicts(self, rows: List[Dict[str, Any]], source_name: str) -> List[RawRenewalItem]:
        """Parses a normalized list of dictionary rows into RawRenewalItem instances."""
        items: List[RawRenewalItem] = []
        for row in rows:
            clean_row = self._normalize_row_keys(row)

            # 1. Check Source column - FOCUS ONLY ON MANUAL
            raw_source = str(
                clean_row.get("source") or 
                clean_row.get("policy_source") or 
                clean_row.get("download_status") or 
                clean_row.get("download") or 
                ""
            ).strip().lower()

            # If source is explicitly provided, it MUST be manual (skip all download/electronic policies)
            if raw_source:
                if not raw_source.startswith("manual"):
                    logger.debug(f"Skipping non-manual policy {clean_row.get('policynumber') or clean_row.get('policy_number')}: source is '{raw_source}'")
                    continue
            if raw_source in ("download", "downloaded", "ivans", "edocs", "edocs & messages", "edocs_&_messages", "yes", "true"):
                continue

            # 2. Check Policy Status / Cancellation Status (Only Active Accounts & Policies)
            policy_status = str(clean_row.get("policy_status") or clean_row.get("current_policy_status") or clean_row.get("status") or "").strip().lower()
            if policy_status in ("inactive", "cancelled", "canceled", "expired", "terminated", "void", "dead"):
                continue

            date_cancelled = str(clean_row.get("date_cancelled") or clean_row.get("date_canceled") or clean_row.get("cancellation_date") or "").strip()
            if date_cancelled and date_cancelled.lower() != "none":
                continue

            trans_type = str(clean_row.get("transaction_type") or clean_row.get("transaction") or "").strip().lower()
            if trans_type in ("cancel conf", "cancel req", "non-renewal", "cancellation", "cancel"):
                continue

            # 3. Check Account / Client Status (Active Accounts Only)
            account_status = str(
                clean_row.get("account_status") or 
                clean_row.get("applicant_status") or 
                clean_row.get("client_status") or 
                clean_row.get("applicant_type") or 
                clean_row.get("client_type") or 
                clean_row.get("type") or 
                ""
            ).strip().lower()
            if any(term in account_status for term in ("inactive", "review", "account review", "closed", "prospect", "lead", "deceased", "lost")):
                continue

            # 4. Explicitly Excluded Inactive Accounts (e.g. PEPPEP N SONS 169788491)
            EXCLUDED_INACTIVE_APPLICANTS = {
                "169788491",  # PEPPEP N SON'S TRUCKING LLC (Inactive Client / Account Review in EZLynx)
            }
            raw_app_id = str(clean_row.get("applicant_id") or clean_row.get("applicantid") or "").strip()
            if raw_app_id in EXCLUDED_INACTIVE_APPLICANTS:
                continue
            raw_insured = str(clean_row.get("account_name") or clean_row.get("insured_name") or "").lower()
            if "peppep" in raw_insured:
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
                clean_row.get("policy_expiration_date") or 
                clean_row.get("expiration_date") or 
                clean_row.get("exp_date") or 
                clean_row.get("expiration")
            )

            if not (pol_num and insured and carrier and exp_date):
                continue

            # Fallback applicant ID if missing in report
            if not app_id:
                app_id = f"EZL-POL-{pol_num}"

            eff_date = self._parse_date(
                clean_row.get("policy_effective_date") or 
                clean_row.get("effective_date") or 
                clean_row.get("eff_date")
            )
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
            lob = clean_row.get("line_of_business") or clean_row.get("lob") or clean_row.get("policy_type") or "General Liability"
            disc_title = clean_row.get("discussion_title") or self._derive_discussion_title(lob)
            uw_name = clean_row.get("underwriter_name") or clean_row.get("underwriter")
            uw_email = clean_row.get("underwriter_email") or clean_row.get("email")
            agent = (
                clean_row.get("csr") or 
                clean_row.get("assigned_producer") or 
                clean_row.get("assigned_agent") or 
                clean_row.get("agent") or 
                clean_row.get("producer")
            )
            # Exclude policies that have active automated IVANS downloads
            from src.ivans.matrix_matcher import ivans_matcher
            if ivans_matcher.is_ivans_downloading(str(carrier).strip(), str(lob).strip()):
                logger.info(
                    f"[IVANS Auto-Download Excluded] {carrier} | {lob} | Policy {pol_num} ({insured}) - automated download active in IVANS"
                )
                continue

            from src.portals.carrier_routing import CarrierRoutingMatrix
            routing = CarrierRoutingMatrix.get_carrier_config(str(carrier).strip())
            portal_supp = False
            portal_url = None
            if routing.get("channel") == "PORTAL":
                portal_supp = True
                portal_url = routing.get("portal_url") or portal_url

            items.append(RawRenewalItem(
                policy_number=str(pol_num).strip(),
                insured_name=str(insured).strip(),
                applicant_id=str(app_id).strip(),
                carrier_name=str(carrier).strip(),
                source="Manual",
                line_of_business=str(lob).strip(),
                discussion_title=disc_title,
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

    def parse_csv_file(self, file_path: Path) -> List[RawRenewalItem]:
        """Parses a CSV file containing renewal records."""
        with open(file_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = [dict(row) for row in reader]
        return self._process_row_dicts(rows, str(file_path))

    def parse_xlsx_file(self, file_path: Path) -> List[RawRenewalItem]:
        """Parses an Excel (.xlsx) file into RawRenewalItem list."""
        try:
            import openpyxl
        except ImportError:
            logger.error("openpyxl is not installed, cannot parse XLSX.")
            return []

        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheet = wb.active
        header_row_idx = 1
        headers = []
        for r in range(1, min(15, sheet.max_row + 1)):
            row_vals = [str(sheet.cell(r, c).value or "").strip() for c in range(1, sheet.max_column + 1)]
            row_str = " ".join(row_vals).lower()
            if "policy" in row_str and ("carrier" in row_str or "company" in row_str or "account" in row_str or "insured" in row_str):
                header_row_idx = r
                headers = row_vals
                break

        if not headers:
            headers = [str(sheet.cell(1, c).value or "").strip() for c in range(1, sheet.max_column + 1)]

        rows_as_dicts = []
        for r in range(header_row_idx + 1, sheet.max_row + 1):
            row_dict = {}
            for col_idx, h in enumerate(headers):
                if h:
                    val = sheet.cell(r, col_idx + 1).value
                    row_dict[h] = "" if val is None else str(val).strip()
            if any(row_dict.values()):
                rows_as_dicts.append(row_dict)

        return self._process_row_dicts(rows_as_dicts, str(file_path))

    def poll_email_reports(self, gmail_client: Optional[Any] = None) -> List[Path]:
        """
        Polls robie@streetsmart.insurance for scheduled EZLynx / Applied Systems renewal reports.
        Downloads attached CSV or Excel files into self.input_dir.
        """
        downloaded_paths = []
        try:
            if gmail_client is None:
                from src.email_outreach.gmail_client import GmailRenewalClient
                gmail_client = GmailRenewalClient()

            active_inboxes = getattr(gmail_client, "inbox_services", {})
            svc = active_inboxes.get("robie@streetsmart.insurance")
            if not svc:
                logger.info("No active Gmail service for robie@streetsmart.insurance; skipping email report polling.")
                return []

            query = 'from:(appliedsystems.com OR ezlynx.com) has:attachment'
            logger.info(f"Checking robie@streetsmart.insurance for scheduled reports: query='{query}'")
            res = svc.users().messages().list(userId="me", q=query, maxResults=15).execute()
            messages = res.get("messages", [])

            self.input_dir.mkdir(parents=True, exist_ok=True)

            for m in messages:
                msg_id = m["id"]
                msg_data = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
                payload = msg_data.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                subject = headers.get("subject", "")

                parts = payload.get("parts", [payload])
                for part in parts:
                    filename = part.get("filename", "")
                    att_id = part.get("body", {}).get("attachmentId")
                    if filename and att_id and any(filename.lower().endswith(ext) for ext in (".csv", ".xlsx", ".xls")):
                        safe_filename = f"EZLynx_Scheduled_{msg_id[:8]}_{filename}"
                        dest = self.input_dir / safe_filename
                        if dest.exists():
                            continue

                        att_data = svc.users().messages().attachments().get(
                            userId="me", messageId=msg_id, id=att_id
                        ).execute()
                        file_bytes = base64.urlsafe_b64decode(att_data["data"])
                        dest.write_bytes(file_bytes)
                        logger.info(f"Downloaded scheduled EZLynx report: {dest.name} (Subject: '{subject}')")
                        downloaded_paths.append(dest)

        except Exception as e:
            logger.error(f"Error polling scheduled email reports: {e}")

        return downloaded_paths

    def fetch_renewals(self, target_date: Optional[date] = None) -> List[RawRenewalItem]:
        """Scans input directory for all CSVs and XLSX files and returns parsed items."""
        all_items: List[RawRenewalItem] = []
        files = [
            f for f in (list(self.input_dir.glob("*.csv")) + list(self.input_dir.glob("*.xlsx")))
            if not f.name.startswith(".")
        ]
        for f in files:
            try:
                if f.suffix.lower() == ".csv":
                    parsed = self.parse_csv_file(f)
                else:
                    parsed = self.parse_xlsx_file(f)
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
                sibling = sibling_policy_for_intake(
                    db,
                    applicant_id=item.applicant_id,
                    expiration_date=item.expiration_date,
                    line_of_business=item.line_of_business,
                    policy_number=item.policy_number,
                )
                if sibling:
                    register_policy_alias(db, sibling, item.policy_number, alias_kind="renewal_term")
                    logger.info(
                        f"Term-number flip for {item.insured_name}: "
                        f"{sibling.policy_number} ↔ {item.policy_number} (aliased, no new row)"
                    )
                    continue

            if not existing:
                policy = PolicyRenewal(
                    policy_number=item.policy_number,
                    insured_name=item.insured_name,
                    applicant_id=item.applicant_id,
                    discussion_title=item.discussion_title,
                    line_of_business=item.line_of_business,
                    carrier_name=item.carrier_name,
                    source=item.source or "Manual",
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
