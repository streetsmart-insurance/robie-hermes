"""Yelp Lead Extractor & Parser."""

import re
import base64
from html.parser import HTMLParser
from typing import Dict, Any, Optional, List


class HTMLDataExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts: List[str] = []
        self.links: List[Dict[str, str]] = []
        self._current_tag = None
        self._current_href = None
        self._ignore = False

    def handle_starttag(self, tag: str, attrs: List[tuple]):
        self._current_tag = tag
        if tag in ["style", "script", "head"]:
            self._ignore = True
            return
        if tag == "a":
            for k, v in attrs:
                if k == "href":
                    self._current_href = v

    def handle_endtag(self, tag: str):
        if tag in ["style", "script", "head"]:
            self._ignore = False
        if tag == "a":
            self._current_href = None
        self._current_tag = None

    def handle_data(self, data: str):
        if not self._ignore:
            val = data.strip()
            if val:
                self.texts.append(val)
                if self._current_href:
                    self.links.append({"text": val, "href": self._current_href})

    def get_full_text(self) -> str:
        return "\n".join(self.texts)


def extract_lead_from_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a Gmail message object into a structured Yelp lead."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    from_header = headers.get("From", "")
    reply_to = headers.get("Reply-To", "")
    date_header = headers.get("Date", "")

    # Check if this is a Yelp lead message
    is_yelp = (
        "yelp.com" in from_header.lower()
        or "yelp" in subject.lower()
        or "messaging.yelp.com" in reply_to.lower()
    )
    if not is_yelp:
        return None

    # Extract HTML body
    def get_html(part: Dict[str, Any]) -> str:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
        for sub in part.get("parts", []):
            res = get_html(sub)
            if res:
                return res
        return ""

    html = get_html(msg.get("payload", {}))
    parser = HTMLDataExtractor()
    if html:
        parser.feed(html)
    full_text = parser.get_full_text()

    # Extract customer name
    name = "Customer"
    name_subj = re.search(r"Message from ([\w\s\.]+) for|Reply to ([\w\s\.]+)\'s", subject, re.I)
    if name_subj:
        name = (name_subj.group(1) or name_subj.group(2)).strip()
    else:
        name_body = re.search(r"([A-Z][a-z]+ [A-Z]\.)\s+(?:Homeowner|\d+)", full_text)
        if name_body:
            name = name_body.group(1).strip()

    # Extract property / request type
    property_type = "Homeowner / Property"
    if "What type of property needs insurance?" in full_text:
        lines = full_text.splitlines()
        for idx, line in enumerate(lines):
            if "What type of property needs insurance?" in line and idx + 1 < len(lines):
                property_type = lines[idx + 1].strip()
                break
    elif "car and truck" in subject.lower() or "auto" in full_text.lower():
        property_type = "Personal Auto"

    # Extract ZIP / Location
    location_zip = ""
    if "In what location do you need the service?" in full_text:
        lines = full_text.splitlines()
        for idx, line in enumerate(lines):
            if "In what location do you need the service?" in line and idx + 1 < len(lines):
                location_zip = lines[idx + 1].strip()
                break
    if not location_zip:
        zip_match = re.search(r"\b(0\d{4})\b", full_text)
        if zip_match:
            location_zip = zip_match.group(1)

    # Extract urgency / timeline
    timing = "Standard"
    if "When do you require this service?" in full_text:
        lines = full_text.splitlines()
        for idx, line in enumerate(lines):
            if "When do you require this service?" in line and idx + 1 < len(lines):
                timing = lines[idx + 1].strip()
                break
    elif "asap" in full_text.lower():
        timing = "ASAP"

    # Extract Direct Lead URL
    lead_url = ""
    for link in parser.links:
        href = link["href"]
        if "biz.yelp.com/leads_center" in href and "/leads/" in href:
            lead_url = href
            break
    if not lead_url:
        match_url = re.search(r"https://biz\.yelp\.com/leads_center/[^\s\"<>]+", full_text)
        if match_url:
            lead_url = match_url.group(0)

    # Extract Phone number if present in body (ignoring agency and yelp numbers)
    phone = ""
    phone_matches = re.findall(r"(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}", full_text)
    for p in phone_matches:
        digits = re.sub(r"\D", "", p)
        if not any(ign in digits for ign in ["4628343", "7679357", "0000000", "1234567"]):
            phone = p
            break

    # Pricing benchmark
    estimate_min = 1000.0
    estimate_max = 1400.0
    if "auto" in property_type.lower():
        estimate_min = 1200.0
        estimate_max = 2200.0

    return {
        "message_id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": subject,
        "customer_name": name,
        "phone": phone,
        "property_type": property_type,
        "location_zip": location_zip or "NJ",
        "timing": timing,
        "reply_to_email": reply_to,
        "lead_url": lead_url,
        "estimate_min": estimate_min,
        "estimate_max": estimate_max,
        "date_received": date_header,
        "raw_text_snippet": full_text[:600]
    }
