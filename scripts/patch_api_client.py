import re

target_file = "/opt/renewal-automation-system/src/ezlynx/api_client.py"
with open(target_file, "r") as f:
    content = f.read()

# 1. Update ROBIE_SIGNATURE and add normalize_robie_signature
old_sig = 'ROBIE_SIGNATURE = "\\n\\nRobie was here"'
new_sig = '''ROBIE_SIGNATURE = "\\n\\nROBIE was here"

def normalize_robie_signature(text: str) -> str:
    """Ensure text ends with the mandatory 'ROBIE was here' signature."""
    cleaned = re.sub(r"\\n*\\s*(?:robie\\s+was\\s+here)\\s*$", "", text, flags=re.IGNORECASE).rstrip()
    return f"{cleaned}\\n\\nROBIE was here"'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("[1] Updated ROBIE_SIGNATURE and added normalize_robie_signature")

# 2. Update add_note_to_discussion signature and signature normalization
old_note_def = '''    def add_note_to_discussion(
        self,
        applicant_id: str,
        discussion_title: Optional[str] = None,
        note_text: str = "",
        policy_number: Optional[str] = None,
        line_of_business: Optional[str] = None,
        carrier_name: Optional[str] = None,
        use_playwright_fallback: bool = True,
        require_existing_discussion: bool = False,
        honor_explicit_title: bool = False,
        policy_numbers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:'''

new_note_def = '''    def add_note_to_discussion(
        self,
        applicant_id: str,
        discussion_title: Optional[str] = None,
        note_text: str = "",
        policy_number: Optional[str] = None,
        line_of_business: Optional[str] = None,
        carrier_name: Optional[str] = None,
        use_playwright_fallback: bool = True,
        require_existing_discussion: bool = False,
        honor_explicit_title: bool = False,
        policy_numbers: Optional[List[str]] = None,
        discussion_id: Optional[str] = None,
    ) -> Dict[str, Any]:'''

if old_note_def in content:
    content = content.replace(old_note_def, new_note_def, 1)
    print("[2] Updated add_note_to_discussion signature with discussion_id")

# Insert discussion_id resolution right before honor_explicit_title
target_marker = "        if honor_explicit_title:"
disc_resolution = '''        if discussion_id and not discussion_title:
            try:
                discs = self.get_applicant_discussions(str(applicant_id)) or []
                matched = next((d for d in discs if str(d.get("discussionId")) == str(discussion_id)), None)
                if matched and matched.get("title"):
                    discussion_title = matched.get("title")
            except Exception as d_err:
                logger.debug(f"Failed to lookup title for discussion_id {discussion_id}: {d_err}")

        if honor_explicit_title:'''

if target_marker in content and "if discussion_id and not discussion_title:" not in content:
    content = content.replace(target_marker, disc_resolution, 1)
    print("[3] Added discussion_id lookup before honor_explicit_title")

# Update Robie signature check in add_note_to_discussion
old_sig_check = '''        # Ensure mandatory Robie signature is included
        if "Robie was here" not in note_text:
            note_text = f"{note_text.rstrip()}{ROBIE_SIGNATURE}"'''

new_sig_check = '''        # Ensure mandatory Robie signature is normalized to uppercase ROBIE was here
        note_text = normalize_robie_signature(note_text)'''

if old_sig_check in content:
    content = content.replace(old_sig_check, new_sig_check, 1)
    print("[4] Updated signature check to use normalize_robie_signature")

# 3. Add validate_policy_payload method before upload_document
target_method_marker = "    def upload_document("
validate_method = '''    def validate_policy_payload(
        self,
        applicant_id: str,
        policy_number: str,
        expected_carrier: Optional[str] = None
    ) -> Dict[str, Any]:
        """Pre-execution validation gate.
        Validates that policy_number exists and is active for the applicant in EZLynx,
        and verifies that expected_carrier matches the companyName. Raises ValueError if invalid.
        """
        policies = self.get_applicant_policies(applicant_id) or []
        if not policies:
            raise ValueError(f"No policies found for applicant {applicant_id} in EZLynx.")

        target_pol = None
        for pol in policies:
            p_num = str(pol.get("policyNumber") or pol.get("PolicyNumber") or "")
            if p_num.strip().lower() == policy_number.strip().lower():
                target_pol = pol
                break

        if not target_pol:
            existing = [str(p.get("policyNumber") or p.get("PolicyNumber")) for p in policies]
            raise ValueError(
                f"Policy '{policy_number}' not found for applicant {applicant_id}. Existing: {existing}"
            )

        if expected_carrier:
            cname = str(
                target_pol.get("companyName") or
                target_pol.get("CompanyName") or
                target_pol.get("masterCompanyName") or
                target_pol.get("writingCompanyName") or ""
            )
            parts = [
                p.lower() for p in expected_carrier.split()
                if len(p) > 3 and p.lower() not in ("insurance", "company", "property", "casualty")
            ]
            if parts and not any(part in cname.lower() for part in parts):
                raise ValueError(
                    f"Carrier mismatch for policy '{policy_number}': Expected '{expected_carrier}', found EZLynx carrier '{cname}'."
                )

        return {"valid": True, "policy": target_pol}

    def upload_document('''

if target_method_marker in content and "def validate_policy_payload(" not in content:
    content = content.replace(target_method_marker, validate_method, 1)
    print("[5] Added validate_policy_payload method")

# 4. Remove simulation fallback in upload_document
old_sim_block = '''        logger.info(f"[SIMULATION] Document '{file_path.name}' uploaded to Applicant {applicant_id} (Folder: {folder_name})")
        return {
            "status": "simulated",
            "method": "simulation",
            "applicant_id": applicant_id,
            "file_name": file_path.name,
            "document_id": f"sim_doc_{file_path.stem}"
        }'''

new_sim_block = '''        pw_err = pw_result.get("error") if pw_result else "playwright_skipped"
        api_err = api_result.get("error") if api_result else "api_skipped"
        err_msg = (
            f"Failed to upload document '{file_path.name}' to Applicant {applicant_id}. "
            f"Playwright error: {pw_err} | API error: {api_err}"
        )
        logger.error(err_msg)
        return {
            "status": "error",
            "error": err_msg,
            "applicant_id": applicant_id,
            "file_name": file_path.name
        }'''

if old_sim_block in content:
    content = content.replace(old_sim_block, new_sim_block, 1)
    print("[6] Removed silent simulation fallback from upload_document")

with open(target_file, "w") as f:
    f.write(content)

print("api_client.py patched successfully.")
