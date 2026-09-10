"""Audit Verification Module for StreetSmart Insurance EZLynx System.

Automates the 10-step Workers' Compensation payroll audit verification workflow.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("audit_verification")

ROBIE_SIGNATURE = "ROBIE was here"

AUDIT_SKIP_CATEGORIES = {
    "audits",
    "audit requests",
    "audit results",
    "audit disputes"
}

class CarrierChannelMatcher:
    """Resolves carrier contact channel, portal URL, and underwriter emails."""

    KNOWN_ROUTING = {
        "pennsylvania lumbermans": {
            "matched_name": "NJCRIB - Pennsylvania Lumbermans Assigned Risk (PMA / Ormarks)",
            "channel": "EMAIL_AND_PHONE",
            "phone": "800-752-1895",
            "portal_url": "https://www.ormarks.com",
            "underwriter_emails": ["policyservices@ormarks.com", "custserv@plmins.com"],
            "notes": "Assigned Risk serviced by PMA / Ormarks. Follow up via email (policyservices@ormarks.com) and phone desk (800-752-1895)."
        },
        "new jersey manufacturers": {
            "matched_name": "NJCRIB - New Jersey Manufacturers Assigned Risk",
            "channel": "EMAIL_AND_PHONE",
            "phone": "800-232-6600",
            "portal_url": "https://www.njm.com/",
            "underwriter_emails": ["wcumail@njm.com"],
            "notes": "Assigned Risk serviced by NJM. Follow up via email (wcumail@njm.com) and phone desk (800-232-6600)."
        },
        "hartford assigned risk": {
            "matched_name": "NJCRIB - Hartford Assigned Risk",
            "channel": "EMAIL",
            "portal_url": "https://ebc.thehartford.com/",
            "underwriter_emails": ["assignedrisk@hartford.com", "arwc@travelers.com"],
            "notes": "Assigned Risk serviced by The Hartford / Travelers ARWC."
        },
        "the hartford": {
            "matched_name": "The Hartford",
            "channel": "PORTAL",
            "portal_url": "https://ebc.thehartford.com/",
            "underwriter_emails": ["agency.service@thehartford.com"],
            "notes": "Retrieve audit docs directly from Electronic Business Center (EBC)."
        },
        "travelers": {
            "matched_name": "Travelers",
            "channel": "PORTAL",
            "portal_url": "https://logon.travelers.com/travelerslogin.asp",
            "underwriter_emails": ["PIUWGFBC@travelers.com"],
            "notes": "Retrieve audit docs directly from Travelers Agent Portal."
        },
        "pie": {
            "matched_name": "Pie Insurance",
            "channel": "PORTAL",
            "portal_url": "https://portal.pieinsurance.com",
            "phone": "855-965-1840",
            "underwriter_emails": ["service@pieinsurance.com"],
            "notes": "Check Pie Partner Portal for audit status and call Partner Support 855-965-1840."
        },
        "selective": {
            "matched_name": "Selective Insurance",
            "channel": "EMAIL",
            "phone": "800-777-9656",
            "portal_url": "https://home.selectiveinsurance.com/WebApplications/EDS/eSelect/Home_Agent.aspx",
            "underwriter_emails": ["myaudit@Selective.com"],
            "notes": "Email audit inquiries directly to myaudit@Selective.com."
        },
        "associated specialty": {
            "matched_name": "Associated Specialty Insurance Agency MGA",
            "channel": "EMAIL",
            "portal_url": "http://www.asiaworkerscomp.com/",
            "underwriter_emails": ["underwriting@asiaworkerscomp.com"],
            "notes": "MGA for Workers Comp. Request audit statement via underwriting@asiaworkerscomp.com."
        },
        "continental assigned risk": {
            "matched_name": "NJCRIB - Continental Assigned Risk",
            "channel": "EMAIL",
            "portal_url": None,
            "underwriter_emails": ["policyservices@berkleyrisk.com"],
            "notes": "Assigned Risk servicing carrier. Check underwriter contacts or request audit via email."
        },
        "nysif": {
            "matched_name": "NYSIF (New York State Insurance Fund)",
            "channel": "EMAIL_AND_PHONE",
            "phone": "888-875-5790",
            "portal_url": "https://www.nysif.com/",
            "underwriter_emails": ["CustomerService@nysif.com"],
            "notes": "New York State Insurance Fund. Annual audits designated as Payroll Verifications. Submit/verify via CustomerService@nysif.com, phone 888-875-5790, or PASS portal. Agency producer code 1960636."
        }
    }

    @classmethod
    def match(cls, carrier_name: str, directory: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        norm = carrier_name.lower().strip()
        for key, conf in cls.KNOWN_ROUTING.items():
            if key in norm:
                return dict(conf)
        if directory:
            for name, data in directory.items():
                if norm == name.lower() or name.lower() in norm or norm in name.lower():
                    channel = data.get("channel")
                    if not channel or channel == "UNKNOWN":
                        if data.get("website") and any(w in data.get("website", "").lower() for w in ["portal", "agent", "ebusiness", "ebc", "logon"]):
                            channel = "PORTAL"
                        elif data.get("underwriters") or data.get("org_email"):
                            channel = "EMAIL"
                        else:
                            channel = "UNKNOWN"
                    return {
                        "matched_name": name,
                        "channel": channel,
                        "portal_url": data.get("portal_url") or data.get("website"),
                        "underwriter_emails": [u.get("email") for u in data.get("underwriters", []) if u.get("email")] or ([data.get("org_email")] if data.get("org_email") else []),
                        "notes": data.get("notes") or data.get("directory_notes", "")
                    }
        return {
            "matched_name": carrier_name,
            "channel": "UNKNOWN",
            "portal_url": None,
            "underwriter_emails": [],
            "notes": "Directory entry incomplete or not yet mapped."
        }


class AuditDocumentClassifier:
    """Classifies documents in an applicant's Document Library into audit categories."""

    @staticmethod
    def classify(
        docs_payload: Any,
        eff_date_str: str = "",
        target_policy_number: str = ""
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not isinstance(docs_payload, dict):
            return {
                "current_statements": [],
                "historical_statements": [],
                "requests": [],
                "voicemails": [],
                "non_compliance": []
            }

        data = docs_payload.get("data", docs_payload)
        docs = data.get("Documents", data.get("documents", []))

        eff_year = None
        if eff_date_str:
            try:
                eff_year = int(eff_date_str[:4])
            except Exception:
                pass

        classified = {
            "current_statements": [],
            "historical_statements": [],
            "requests": [],
            "voicemails": [],
            "non_compliance": []
        }

        raw_statements = []

        for d in docs:
            raw_desc = (d.get("Description") or d.get("FileName") or d.get("DocumentName") or "").strip()
            desc = raw_desc.lower()
            if not desc or desc in AUDIT_SKIP_CATEGORIES:
                continue

            doc_pid = str(d.get("PolicyId") or "0")

            if not any(k in desc for k in ["audit", "prmau", "payroll", "adjustment"]):
                continue

            doc_info = {
                "id": d.get("Id") or d.get("DocumentId"),
                "filename": raw_desc,
                "policy_id": doc_pid,
                "date": d.get("CreatedDate") or d.get("UploadedDate") or d.get("DateTimeCreated")
            }

            if any(ext in desc for ext in [".mp3", ".wav", ".m4a"]) or "voice mail" in desc or "voicemail" in desc:
                classified["voicemails"].append(doc_info)
            elif "noncompliance" in desc or "non-compliance" in desc or ("cancellation" in desc and "audit" in desc):
                # Filter out stale prior year non-compliance notices if eff_year is provided
                doc_date_str = str(doc_info.get("date") or "")
                is_stale = False
                if eff_year and doc_date_str:
                    try:
                        doc_year = int(doc_date_str[:4])
                        if doc_year < eff_year:
                            is_stale = True
                    except Exception:
                        pass
                if not is_stale:
                    classified["non_compliance"].append(doc_info)
                else:
                    classified["historical_statements"].append(doc_info)
            elif any(k in desc for k in [
                "final audit",
                "statement of premium adjustment",
                "audit statement",
                "audit bill",
                "audit invoice",
                "audit results",
                "prmau"
            ]):
                raw_statements.append(doc_info)
            elif any(k in desc for k in ["audit request", "audit form", "voluntary audit", "payroll report"]):
                classified["requests"].append(doc_info)

        raw_statements.sort(key=lambda x: str(x.get("date") or ""), reverse=True)

        for stmt in raw_statements:
            stmt_date_str = str(stmt.get("date") or "")
            stmt_desc = stmt.get("filename", "").lower()

            is_current = False
            if eff_year:
                expiring_period_match = f"{eff_year - 1}" in stmt_desc and f"{eff_year}" in stmt_desc
                if expiring_period_match:
                    is_current = True
                elif stmt_date_str.startswith(str(eff_year)):
                    is_current = True
            else:
                is_current = True

            if is_current:
                classified["current_statements"].append(stmt)
            else:
                classified["historical_statements"].append(stmt)

        return classified


class AuditNoteBuilder:
    """Builds standardized discussion notes concluding with 'ROBIE was here'."""

    @staticmethod
    def build(
        policy_number: str,
        carrier_name: str,
        lob: str,
        term_dates: str,
        csr_name: str,
        status_summary: str,
        actions_taken: List[str],
        next_steps: str,
        follow_up_date: str
    ) -> str:
        lines = [
            f"Policy: #{policy_number} ({lob} - {carrier_name})",
            f"Audit Verification - {term_dates}",
            f"CSR: {csr_name} | Date: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"Status: {status_summary}",
            "",
            "Actions / Evidence:"
        ]
        for act in actions_taken:
            lines.append(f"- {act}")
        lines.append("")
        lines.append(f"Next Steps: {next_steps}")
        lines.append(f"Follow-up Date: {follow_up_date}")
        lines.append("")
        lines.append(ROBIE_SIGNATURE)
        return "\n".join(lines)


class AuditEmailBuilder:
    """Builds client delivery email drafts and carrier underwriter requests."""

    @staticmethod
    def build_client_delivery(
        client_name: str,
        contact_name: str,
        policy_number: str,
        carrier_name: str,
        audit_file_name: str,
        csr_name: str,
        term_dates: str
    ) -> Dict[str, str]:
        first_name = contact_name.split()[0] if contact_name else client_name
        subject = f"Audit - Workers Compensation Policy #{policy_number} - {client_name}"
        body_lines = [
            f"Hello {first_name},",
            "",
            f"Attached please find the completed payroll audit statement from {carrier_name} for Workers' Compensation policy #{policy_number} (Term: {term_dates}).",
            "",
            f"Attachment: {audit_file_name}",
            "",
            "Please review the audited payroll and classifications. If you have any questions or if any revisions are needed, please reply to this email so we can assist.",
            "",
            "Best regards,",
            f"{csr_name or 'StreetSmart Insurance Services'}",
            "StreetSmart Insurance"
        ]
        return {
            "subject": subject,
            "body": "\n".join(body_lines),
            "recipient": contact_name,
            "attachment": audit_file_name,
            "template_name": "Audit (StreetSmart SOP Built-in)"
        }

    @staticmethod
    def build_client_audit_results(
        client_name: str,
        contact_name: str,
        policy_number: str,
        carrier_name: str,
        audit_file_name: str,
        adjustment_summary: str = "",
        csr_name: str = "",
        term_dates: str = ""
    ) -> Dict[str, str]:
        first_name = contact_name.split()[0] if contact_name else client_name
        subject = f"Completed Premium Audit Results - Workers Compensation #{policy_number} - {client_name}"
        adj_clause = f"\nResult: {adjustment_summary}\n" if adjustment_summary else ""
        body_lines = [
            f"Hello {first_name},",
            "",
            f"We are pleased to inform you that {carrier_name} has completed the final payroll audit for your Workers' Compensation policy #{policy_number} (Term: {term_dates}).",
            adj_clause,
            f"Attached please find your final audit statement for your records: {audit_file_name}",
            "",
            "Please review the audited payroll figures. If you have any questions or need clarification regarding the adjustment, please feel free to reach out to our team at 732-298-6745.",
            "",
            "Best regards,",
            f"{csr_name or 'StreetSmart Insurance Services'}",
            "StreetSmart Insurance"
        ]
        return {
            "subject": subject,
            "body": "\n".join(body_lines),
            "recipient": contact_name,
            "attachment": audit_file_name,
            "template_name": "Audit Results (Completed Statement)"
        }

    @staticmethod
    def build_carrier_outreach(
        carrier_name: str,
        client_name: str,
        policy_number: str,
        term_dates: str,
        recipient_email: str,
        csr_name: str
    ) -> Dict[str, str]:
        subject = f"Final Payroll Audit Request - {client_name} - Policy #{policy_number}"
        body_lines = [
            f"Hello {carrier_name} Audit Department / Underwriting Team,",
            "",
            "Could you please provide the final payroll audit statement / workpapers for the following policy?",
            "",
            f"  Named Insured : {client_name}",
            f"  Policy Number : {policy_number}",
            f"  Expiring Term : {term_dates}",
            "",
            "Please send the audit document to this thread so we can review with the insured.",
            "",
            "Thank you,",
            f"{csr_name or 'StreetSmart Insurance'}",
            "StreetSmart Insurance"
        ]
        return {
            "subject": subject,
            "body": "\n".join(body_lines),
            "recipient": recipient_email,
            "template_name": "Carrier Audit Request (SOP)"
        }


class AuditVerifier:
    """Orchestrates audit verification across EZLynx API and carrier rules."""

    def __init__(self, client: Any, carrier_directory: Optional[Dict[str, Any]] = None):
        self.client = client
        self.carrier_directory = carrier_directory or {}

    def process_account(self, row: Dict[str, str]) -> Dict[str, Any]:
        app_id = row.get("Applicant ID", "").strip()
        account_name = row.get("Account Name", "").strip()
        policy_num = row.get("Policy Number", "").strip()
        carrier_raw = row.get("Master Company", "").strip()
        eff_date = row.get("Effective Date", "").strip()
        exp_date = row.get("Expiration Date", "").strip()
        lob = row.get("Line of Business", "Workers comp").strip()
        csr = row.get("CSR", "").strip()
        producer = row.get("Assigned Producer", "").strip()
        term = f"{eff_date} to {exp_date}"

        # Pre-Flight Safety Filter: Auditable Line of Business Hardening
        # Workers' Comp is audited. Commercial Auto, Personal Auto, Cargo, APD, Homeowners, Umbrella, Bonds, and standard BOP are NOT audited.
        lob_lower = (lob or "").lower().strip()
        NON_AUDITABLE_LOBS = {
            "commercial auto", "auto (commercial)", "autob", "autop", "personal auto",
            "cargo", "motor truck cargo", "auto physical damage", "apd", "fleet",
            "homeowners", "home", "dwelling", "bonds", "bonds miscellaneous", "bmisc",
            "commercial umbrella", "umbrella - comm", "cumbr", "business owners", "bop",
            "business owners policy"
        }
        AUDITABLE_LOBS = {"workers comp", "workers' comp", "workers compensation", "workers' compensation", "work", "wc"}

        is_explicitly_non_auditable = any(non in lob_lower for non in NON_AUDITABLE_LOBS)
        is_auditable = any(aud in lob_lower for aud in AUDITABLE_LOBS)

        if is_explicitly_non_auditable and not is_auditable:
            logger.warning(
                f"[AUDIT SAFETY GATE] Blocked non-auditable account '{account_name}' (App #{app_id}, Policy #{policy_num}): "
                f"LOB '{lob}' is not subject to payroll audit."
            )
            return {
                "applicant_id": app_id,
                "account_name": account_name,
                "policy_number": policy_num,
                "carrier": carrier_raw,
                "term": term,
                "csr": csr,
                "producer": producer,
                "lob": lob,
                "state": "rejected_non_auditable_lob",
                "status": "REJECTED_NON_AUDITABLE_LOB",
                "error": f"LOB '{lob}' is not subject to annual payroll audit.",
                "actions_taken": [f"Audit Safety Gate rejected non-auditable LOB '{lob}' (Commercial Auto / Cargo / Personal lines are excluded)."],
                "evidence": {}
            }

        result = {
            "applicant_id": app_id,
            "account_name": account_name,
            "policy_number": policy_num,
            "carrier": carrier_raw,
            "term": term,
            "csr": csr,
            "producer": producer,
            "lob": lob,
            "actions_taken": [],
            "evidence": {}
        }

        # 1. Applicant Profile Validation
        try:
            app_profile = self.client.get_applicant(app_id)
            applicant_info = app_profile.get("applicant", {}) if isinstance(app_profile, dict) else {}
            person_name = applicant_info.get("ContactName") or (applicant_info.get("FirstName", "") + " " + applicant_info.get("LastName", "")).strip()
            contact_name = person_name if person_name else (applicant_info.get("BusinessName") or account_name)
            contact_email = applicant_info.get("BusinessEmail")
            contact_phone = applicant_info.get("BusinessPhone") or applicant_info.get("CellPhone") or applicant_info.get("HomePhone")
            result["evidence"]["contact_person"] = person_name or "Not Specified"
            result["evidence"]["contact_email"] = contact_email
            result["evidence"]["contact_phone"] = contact_phone
            result["actions_taken"].append(
                f"Confirmed applicant profile for '{account_name}' (Contact: {contact_name}, Email: {contact_email or 'None on file'})"
            )
        except Exception as e:
            contact_name = account_name
            contact_email = None
            contact_phone = None
            result["actions_taken"].append(f"Applicant profile check skipped/errored: {e}")

                # 1b. Check Existing Discussions and Active Tasks First
        existing_audit_discussion = None
        active_team_task = None
        try:
            discs = self.client.get_applicant_discussions(app_id)
            for d in discs:
                d_title = d.get("title", "")
                if any(w in d_title.lower() for w in ["audit", "wc audit", "policy audit verification"]):
                    existing_audit_discussion = d
                    dn = d.get("discussionNote", {})
                    task = dn.get("task") if dn else None
                    if task:
                        active_team_task = {
                            "discussion_id": d.get("discussionId"),
                            "discussion_title": d_title,
                            "task_id": task.get("taskId"),
                            "task_desc": task.get("taskDescription"),
                            "assigned_to": task.get("taskAssignment", {}).get("name"),
                            "created_by": d.get("lastModifiedByName") or d.get("createdByName") or "Teammate",
                            "due_date": (task.get("dueDate") or "")[:10]
                        }
                    break
        except Exception as e:
            logger.debug(f"Could not fetch discussions for {app_id}: {e}")

        if active_team_task:
            result["existing_task"] = active_team_task
            result["discussion_title"] = active_team_task["discussion_title"]
            result["actions_taken"].append(
                f"Active team task found on '{active_team_task['discussion_title']}': '{active_team_task['task_desc']}' "
                f"(Assigned to {active_team_task['assigned_to']} by {active_team_task['created_by']}, Due {active_team_task['due_date']})"
            )
        elif existing_audit_discussion:
            result["discussion_title"] = existing_audit_discussion.get("title", "Policy Audit Verification")
            result["actions_taken"].append(f"Hooked into existing discussion card: '{result['discussion_title']}'")
        else:
            result["discussion_title"] = "Audit"

# 2. Carrier Channel Resolution
        carrier_info = CarrierChannelMatcher.match(carrier_raw, self.carrier_directory)
        result["carrier_info"] = carrier_info
        channel = carrier_info.get("channel", "UNKNOWN")
        result["actions_taken"].append(
            f"Carrier Directory: Matched '{carrier_info.get('matched_name')}' -> Channel: {channel}"
        )

        # 3. Document Library Classification
        try:
            docs_payload = self.client.list_applicant_documents(app_id, page_index=1, page_size=100)
            classified_docs = AuditDocumentClassifier.classify(
                docs_payload,
                eff_date_str=eff_date,
                target_policy_number=policy_num
            )
            result["evidence"]["classified_documents"] = classified_docs
        except Exception as e:
            classified_docs = {
                "current_statements": [],
                "historical_statements": [],
                "requests": [],
                "voicemails": [],
                "non_compliance": []
            }
            result["evidence"]["audit_documents_error"] = str(e)

        # 4. State Assignment & Drafting
        current_statements = classified_docs.get("current_statements", [])
        historical_statements = classified_docs.get("historical_statements", [])
        voicemails = classified_docs.get("voicemails", [])
        non_compliance = classified_docs.get("non_compliance", [])

        if current_statements:
            primary_doc = current_statements[0]
            pname = primary_doc['filename']
            result["actions_taken"].append(f"Verified completed audit statement in Download History / Document Library: '{pname}'")

            # Check if this is an already completed carrier audit statement (PRMAU / Final Audit)
            is_completed_statement = any(k in pname.lower() for k in ["prmau", "final audit", "statement of premium adjustment"])

            if is_completed_statement:
                client_email = AuditEmailBuilder.build_client_audit_results(
                    client_name=account_name,
                    contact_name=contact_name,
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    audit_file_name=pname,
                    adjustment_summary="Return premium / premium adjustment processed by carrier",
                    csr_name=csr,
                    term_dates=term
                )
                result["client_email_draft"] = client_email
                result["actions_taken"].append("Drafted client delivery email with final completed audit results")

                client_autodial = AuditVoiceDispatcher.build_client_audit_completed_prompt(
                    client_name=account_name,
                    contact_name=contact_name,
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    client_email=contact_email,
                    adjustment_text="a premium adjustment / return credit"
                )
                result["client_autodial_draft"] = {
                    "phone": contact_phone,
                    "recipient": contact_name,
                    "prompt": client_autodial
                }
                result["actions_taken"].append(f"Staged client completion notification to {contact_phone or 'phone on file'}")

                result["state"] = "completed"
                result["discussion_title"] = result.get("discussion_title") or "Audit"
                result["draft_note"] = AuditNoteBuilder.build(
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    lob=lob,
                    term_dates=term,
                    csr_name=csr,
                    status_summary="Carrier audit completed and processed in Download History. Audit statement on file; shared with insured.",
                    actions_taken=result["actions_taken"],
                    next_steps="Audit completed. Email results statement to insured and complete EZLynx task checklist.",
                    follow_up_date=date.today().strftime("%Y-%m-%d")
                )
            else:
                client_email = AuditEmailBuilder.build_client_delivery(
                    client_name=account_name,
                    contact_name=contact_name,
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    audit_file_name=pname,
                    csr_name=csr,
                    term_dates=term
                )
                result["client_email_draft"] = client_email
                result["actions_taken"].append(f"Drafted client delivery email using template '{client_email['template_name']}'")

                client_autodial = AuditVoiceDispatcher.build_client_autodial_prompt(
                    client_name=account_name,
                    contact_name=contact_name,
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    client_email=contact_email
                )
                result["client_autodial_draft"] = {
                    "phone": contact_phone,
                    "recipient": contact_name,
                    "prompt": client_autodial
                }
                result["actions_taken"].append(f"Staged client voice auto-dial reminder to {contact_phone or 'phone on file'}")

                result["state"] = "ready_for_action"
                result["discussion_title"] = result.get("discussion_title") or "Audit"
                result["draft_note"] = AuditNoteBuilder.build(
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    lob=lob,
                    term_dates=term,
                    csr_name=csr,
                    status_summary="Audit statement retrieved; client delivery drafted for review.",
                    actions_taken=result["actions_taken"],
                    next_steps="Review client email draft and send via EZLynx Overview email icon.",
                    follow_up_date=date.today().strftime("%Y-%m-%d")
                )
        elif non_compliance:
            nc_doc = non_compliance[0]
            result["actions_taken"].append(f"EXCEPTION FLAGGED: Audit Non-Compliance notice on account: '{nc_doc['filename']}'")
            result["state"] = "ready_for_action"
            result["discussion_title"] = "Audit"
            result["draft_note"] = AuditNoteBuilder.build(
                policy_number=policy_num,
                carrier_name=carrier_info.get("matched_name", carrier_raw),
                lob=lob,
                term_dates=term,
                csr_name=csr,
                status_summary=f"URGENT: Audit Non-Compliance Notice detected ('{nc_doc['filename']}'). Requires immediate CSR intervention.",
                actions_taken=result["actions_taken"],
                next_steps=f"Contact client ({contact_name} at {contact_email or 'phone'}) and carrier immediately to resolve non-compliance.",
                follow_up_date=date.today().strftime("%Y-%m-%d")
            )
        elif voicemails:
            vm_doc = voicemails[0]
            result["actions_taken"].append(f"Incoming auditor voicemail identified: '{vm_doc['filename']}'")
            result["state"] = "ready_for_research"
            result["discussion_title"] = "Audit Checker"
            result["draft_note"] = AuditNoteBuilder.build(
                policy_number=policy_num,
                carrier_name=carrier_info.get("matched_name", carrier_raw),
                lob=lob,
                term_dates=term,
                csr_name=csr,
                status_summary=f"Auditor communication received ('{vm_doc['filename']}'). Awaiting CSR listening/review.",
                actions_taken=result["actions_taken"],
                next_steps=f"Listen to auditor voicemail '{vm_doc['filename']}' and follow up with auditor.",
                follow_up_date=date.today().strftime("%Y-%m-%d")
            )
        else:
            if historical_statements:
                hist_doc = historical_statements[0]
                result["actions_taken"].append(f"Found historical audit statement for prior year ('{hist_doc['filename']}'); current expiring term statement pending.")
            else:
                result["actions_taken"].append("No audit statements currently found in EZLynx Document Library.")

            if channel == "PORTAL":
                portal_url = carrier_info.get("portal_url", "Portal")
                carrier_phone = carrier_info.get("phone") or ("855-965-1840" if "pie" in carrier_info.get("matched_name", "").lower() else "Carrier Service")
                
                carrier_call = AuditVoiceDispatcher.build_carrier_call_prompt(
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    client_name=account_name,
                    policy_number=policy_num,
                    term_dates=term
                )
                result["carrier_voice_call_draft"] = {
                    "carrier": carrier_info.get("matched_name", carrier_raw),
                    "phone": carrier_phone,
                    "prompt": carrier_call
                }
                result["actions_taken"].append(f"Carrier portal check available at {portal_url} (or call carrier support at {carrier_phone}).")

                # If teammate already has an active task, align directly with it
                if active_team_task:
                    status_summary = (
                        f"Active task assigned to {active_team_task['assigned_to']} by {active_team_task['created_by']}: "
                        f"'{active_team_task['task_desc']}'. Verifying completion status on {portal_url} or via call."
                    )
                    next_steps = (
                        f"Check {carrier_info.get('matched_name')} portal ({portal_url}) or call {carrier_phone} "
                        f"to confirm if audit was completed. Update task due {active_team_task['due_date']}."
                    )
                    result["discussion_title"] = active_team_task["discussion_title"]
                else:
                    status_summary = f"Audit check initiated. Expiring term audit file missing from EZLynx; check carrier portal ({portal_url}) or call carrier."
                    next_steps = f"Retrieve expiring term audit statement from carrier portal ({portal_url}) or call {carrier_phone}."
                    result["discussion_title"] = result.get("discussion_title") or "Audit Checker"

                result["state"] = "ready_for_research"
                result["draft_note"] = AuditNoteBuilder.build(
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    lob=lob,
                    term_dates=term,
                    csr_name=csr,
                    status_summary=status_summary,
                    actions_taken=result["actions_taken"],
                    next_steps=next_steps,
                    follow_up_date=active_team_task["due_date"] if active_team_task and active_team_task.get("due_date") else date.today().strftime("%Y-%m-%d")
                )
            elif channel in ("EMAIL", "EMAIL_ASK_PORTAL", "DIRECT_EMAIL"):
                uw_emails = carrier_info.get("underwriter_emails", [])
                primary_email = uw_emails[0] if uw_emails else None

                if primary_email:
                    carrier_email = AuditEmailBuilder.build_carrier_outreach(
                        carrier_name=carrier_info.get("matched_name", carrier_raw),
                        client_name=account_name,
                        policy_number=policy_num,
                        term_dates=term,
                        recipient_email=primary_email,
                        csr_name=csr
                    )
                    result["carrier_email_draft"] = carrier_email
                    result["actions_taken"].append(f"Drafted underwriter outreach to {primary_email} to request audit workpapers.")

                    carrier_call = AuditVoiceDispatcher.build_carrier_call_prompt(
                        carrier_name=carrier_info.get("matched_name", carrier_raw),
                        client_name=account_name,
                        policy_number=policy_num,
                        term_dates=term
                    )
                    result["carrier_voice_call_draft"] = {
                        "carrier": carrier_info.get("matched_name", carrier_raw),
                        "phone": carrier_info.get("phone") or "Carrier Service",
                        "prompt": carrier_call
                    }
                    result["actions_taken"].append("Staged carrier voice AI call to request audit packet")
                    result["state"] = "waiting_for_carrier"
                    result["discussion_title"] = "Audit Checker"
                    result["draft_note"] = AuditNoteBuilder.build(
                        policy_number=policy_num,
                        carrier_name=carrier_info.get("matched_name", carrier_raw),
                        lob=lob,
                        term_dates=term,
                        csr_name=csr,
                        status_summary=f"Audit statement requested from underwriter ({primary_email}).",
                        actions_taken=result["actions_taken"],
                        next_steps=f"Awaiting underwriter response from {primary_email}.",
                        follow_up_date=date.today().strftime("%Y-%m-%d")
                    )
                else:
                    result["actions_taken"].append("Carrier directory lacks designated underwriter/audit email contact.")
                    result["state"] = "directory_incomplete"
                    result["discussion_title"] = "Audit Checker"
                    result["draft_note"] = AuditNoteBuilder.build(
                        policy_number=policy_num,
                        carrier_name=carrier_info.get("matched_name", carrier_raw),
                        lob=lob,
                        term_dates=term,
                        csr_name=csr,
                        status_summary="Carrier directory incomplete: Missing underwriter email for audit request.",
                        actions_taken=result["actions_taken"],
                        next_steps="Update Carrier Directory with current audit/underwriter contact.",
                        follow_up_date=date.today().strftime("%Y-%m-%d")
                    )
            else:
                result["state"] = "ready_for_research"
                result["discussion_title"] = "Audit Checker"
                result["draft_note"] = AuditNoteBuilder.build(
                    policy_number=policy_num,
                    carrier_name=carrier_info.get("matched_name", carrier_raw),
                    lob=lob,
                    term_dates=term,
                    csr_name=csr,
                    status_summary=f"Carrier channel unmapped for {carrier_raw}.",
                    actions_taken=result["actions_taken"],
                    next_steps="Research carrier audit submission channel and verify status.",
                    follow_up_date=date.today().strftime("%Y-%m-%d")
                )

        return result


class AuditVoiceDispatcher:
    """Builds conversational Voice AI prompts and dispatches carrier/client calls."""

    @staticmethod
    def build_client_autodial_prompt(
        client_name: str,
        contact_name: str,
        policy_number: str,
        carrier_name: str,
        client_email: Optional[str] = None
    ) -> str:
        first_name = contact_name.split()[0] if contact_name else client_name
        email_clause = f" to {client_email}" if client_email else ""
        return (
            f"Hello {first_name}, this is Robie calling from StreetSmart Insurance Services regarding your "
            f"Workers' Compensation policy #{policy_number} with {carrier_name}. We have sent your payroll "
            f"audit reporting packet{email_clause}. Please be sure to complete and return this audit directly "
            f"to avoid any carrier estimated audit penalties or policy disruptions. If you have any questions "
            f"or need assistance submitting your payroll figures, you can reply directly to our email or call "
            f"our agency at 732-298-6745. Thank you!"
        )

    @staticmethod
    def build_client_audit_completed_prompt(
        client_name: str,
        contact_name: str,
        policy_number: str,
        carrier_name: str,
        client_email: Optional[str] = None,
        adjustment_text: str = ""
    ) -> str:
        first_name = contact_name.split()[0] if contact_name else client_name
        email_clause = f" to {client_email}" if client_email else ""
        adj_clause = f" The audit resulted in {adjustment_text}." if adjustment_text else ""
        return (
            f"Hello {first_name}, this is Robie calling from StreetSmart Insurance Services regarding your "
            f"Workers' Compensation policy #{policy_number} with {carrier_name}. We wanted to notify you that "
            f"the carrier has completed your annual payroll audit.{adj_clause} We have emailed your final audit "
            f"statement{email_clause} for your records. No further action is required unless you have questions. "
            f"Thank you and have a great day!"
        )

    @staticmethod
    def build_carrier_call_prompt(
        carrier_name: str,
        client_name: str,
        policy_number: str,
        term_dates: str,
        agency_code: Optional[str] = None
    ) -> str:
        code_clause = f" Our agency producer code is {agency_code}." if agency_code else ""
        return (
            f"Hello, my name is Robie calling from StreetSmart Insurance on behalf of our mutual insured, "
            f"{client_name}, policy number {policy_number}. We are requesting the final payroll audit packet "
            f"and workpapers for the expiring term {term_dates}. Could you please email the audit packet to "
            f"robie@streetsmart.insurance or confirm if it has been published to the agent portal?{code_clause} "
            f"Thank you!"
        )
