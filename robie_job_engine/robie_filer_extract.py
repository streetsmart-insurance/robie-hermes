"""Text extraction and dec-page summary for robie-filer.

Runs on the worker host. ``pdftotext -layout`` reads each page; a page
with almost no text is rendered with ``pdftoppm`` and read by
``tesseract``. Nothing here calls a model.

The summary is rule-based and keeps a page number on every value so a
person can check it against the PDF. A field that is not found is left
empty. Nothing is guessed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

OCR_MIN_CHARS = 100
COMMAND_TIMEOUT_SECONDS = 120

_MONEY = r"\$\s?[\d,]+(?:\.\d{2})?"
_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}|(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},\s+\d{4}"
_DATE_RE = re.compile(_DATE, re.IGNORECASE)
_MONEY_RE = re.compile(_MONEY)
_VIN_RE = re.compile(r"\b(?=(?:[A-HJ-NPR-Z]*\d){3})[A-HJ-NPR-Z0-9]{17}\b")
_POLICY_VALUE = r"[A-Z]{0,5}[ -]{0,2}\d[\d\-]{4,20}[A-Z0-9]*"
_POLICY_LABEL_RE = re.compile(r"POLICY\s*(?:NO\.?|NUMBER|#)\s*:?\s*(" + _POLICY_VALUE + r")")
_POLICY_LABEL_ONLY_RE = re.compile(r"POLICY\s*(?:NO\.?|NUMBER|#)\b", re.IGNORECASE)
_POLICY_START_RE = re.compile(r"^\s*(" + _POLICY_VALUE + r")\b")
_FORM_ID_RE = re.compile(r"^[A-Z]{2,4}[ -]?\d{2}\s?\d{2}(?:\s\d{2}){1,2}$")
_RUN_ON_RE = re.compile(r"\S{26,}")
_SCHEDULE_ROW_RE = re.compile(r"^\s*(\d{1,3})\s+((?:19|20)\d{2}\s+.+?)\s+(" + _MONEY + r")\s*$")
_PARTY_HEADINGS = re.compile(r"LOSS\s+PAYEE|ADDITIONAL\s+INSURED|MORTGAGEE|LIENHOLDER|LENDER'?S\s+LOSS", re.IGNORECASE)
_BOILERPLATE = re.compile(
    r"THIS ENDORSEMENT|PLEASE READ|modifies insurance|COVERAGE PART|Copyright|INSURED'S COPY|"
    r"If no\s*entry|^SCHEDULE$|Name of Person or Organization|POLICY NUMBER",
    re.IGNORECASE,
)


@dataclass
class Found:
    value: str
    page: int


@dataclass
class DecSummary:
    named_insured: list[Found] = field(default_factory=list)
    policy_number: list[Found] = field(default_factory=list)
    term: list[Found] = field(default_factory=list)
    total_premium: list[Found] = field(default_factory=list)
    coverages_limits: list[Found] = field(default_factory=list)
    deductibles: list[Found] = field(default_factory=list)
    scheduled_items: list[Found] = field(default_factory=list)
    loss_payees_additional_insureds: list[Found] = field(default_factory=list)
    pages: int = 0
    ocr_pages: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def one_line(self) -> str:
        """Short text for the sheet. Full detail is in the JSON file."""

        def first(items: list[Found]) -> str:
            return f"{items[0].value} (p{items[0].page})" if items else "-"

        return (
            f"Insured: {first(self.named_insured)} | Policy: {first(self.policy_number)} | "
            f"Term: {first(self.term)} | Premium: {first(self.total_premium)} | "
            f"Items: {len(self.scheduled_items)} | Payees/AIs: {len(self.loss_payees_additional_insureds)}"
        )


class ExtractionUnavailable(RuntimeError):
    """pdftotext is missing. The filing still counts; the summary does not."""


def _run(args: list[str], *, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(
        args, input=stdin, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {done.returncode}")
    return done.stdout


def page_count(pdf_path: Path) -> int:
    info = _run(["pdfinfo", str(pdf_path)]).decode("utf-8", "replace")
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    return int(match.group(1)) if match else 0


def ocr_page(pdf_path: Path, page: int, workdir: Path) -> str:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    prefix = workdir / f"p{page}"
    _run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-png", "-singlefile", str(pdf_path), str(prefix)])
    return _run(["tesseract", f"{prefix}.png", "-", "--psm", "6"]).decode("utf-8", "replace")


def extract_pages(pdf_bytes: bytes) -> tuple[list[str], list[int]]:
    """Text of each page, and the page numbers that needed OCR."""

    if not shutil.which("pdftotext") or not shutil.which("pdfinfo"):
        raise ExtractionUnavailable("pdftotext/pdfinfo are not installed on this host")
    with tempfile.TemporaryDirectory(prefix="robie-filer-") as tmp:
        work = Path(tmp)
        pdf = work / "doc.pdf"
        pdf.write_bytes(pdf_bytes)
        pages: list[str] = []
        ocr: list[int] = []
        for number in range(1, page_count(pdf) + 1):
            text = _run(["pdftotext", "-layout", "-f", str(number), "-l", str(number), str(pdf), "-"]).decode(
                "utf-8", "replace"
            )
            if len(text.strip()) < OCR_MIN_CHARS:
                scanned = ocr_page(pdf, number, work)
                if len(scanned.strip()) > len(text.strip()):
                    text = scanned
                    ocr.append(number)
            pages.append(text)
        return pages, ocr


def _add(bucket: list[Found], value: str, page: int, limit: int = 40) -> None:
    value = re.sub(r"\s+", " ", value).strip(" :-")
    if not value or len(bucket) >= limit:
        return
    if any(item.value == value for item in bucket):
        return
    bucket.append(Found(value, page))


def summarize_pages(pages: list[str], ocr_pages: list[int] | None = None) -> DecSummary:
    """Rule-based dec-page fields with page numbers (1-based)."""

    summary = DecSummary(pages=len(pages), ocr_pages=list(ocr_pages or []))
    for number, text in enumerate(pages, 1):
        lines = [line.rstrip() for line in text.splitlines()]
        upper_page = text.upper()
        for match in _POLICY_LABEL_RE.finditer(text):
            _add(summary.policy_number, match.group(1), number, limit=3)
        for index, line in enumerate(lines[:-1]):
            if _POLICY_LABEL_ONLY_RE.search(line) and not _POLICY_LABEL_RE.search(line):
                nxt = _POLICY_START_RE.match(lines[index + 1])
                if nxt:
                    _add(summary.policy_number, nxt.group(1), number, limit=3)
        for index, line in enumerate(lines):
            upper = line.upper()
            stripped = line.strip()
            if re.search(r"NAMED INSURED", upper) and not re.search(r"NAMED INSURED IS", upper):
                after = re.split(r"NAMED INSURED(?: AND ADDRESS)?:?", stripped, flags=re.IGNORECASE)[-1].strip()
                if not after or re.fullmatch(r"(?:IS:?|AND ADDRESS)", after, re.IGNORECASE):
                    after = next((nxt.strip() for nxt in lines[index + 1 : index + 3] if nxt.strip()), "")
                if after and not re.search(r"\bIS\b:?$", after) and len(after) < 80:
                    _add(summary.named_insured, after, number, limit=3)
            if re.search(r"POLICY PERIOD|\bPERIOD\b|EFFECTIVE", upper):
                window = " ".join(lines[index : index + 4])
                dates = _DATE_RE.findall(window)
                if len(dates) >= 2 and dates[0].casefold() != dates[1].casefold():
                    _add(summary.term, f"{dates[0]} to {dates[1]}", number, limit=3)
            if re.search(r"TOTAL (?:POLICY )?PREMIUM|TOTAL ANNUAL PREMIUM|POLICY PREMIUM", upper):
                money = _MONEY_RE.findall(line) or _MONEY_RE.findall(" ".join(lines[index + 1 : index + 2]))
                if money:
                    _add(summary.total_premium, money[-1].replace(" ", ""), number, limit=3)
            if "DEDUCTIBLE" in upper:
                money = _MONEY_RE.findall(line) or _MONEY_RE.findall(" ".join(lines[index + 1 : index + 2]))
                if money:
                    _add(summary.deductibles, f"{stripped[:80]}", number, limit=10)
            row = _SCHEDULE_ROW_RE.match(line)
            if row:
                _add(summary.scheduled_items, f"{row.group(1)}. {row.group(2).strip()} {row.group(3)}", number)
            elif _VIN_RE.search(line) and _MONEY_RE.search(line) is None:
                _add(summary.scheduled_items, stripped[:120], number)
            if (
                _MONEY_RE.search(line)
                and re.search(r"LIMIT|EACH OCCURRENCE|AGGREGATE|COVERAGE|TOTAL\s+\$|ALL COVERED PROPERTY", upper)
                and "PREMIUM" not in upper
                and "TERRORISM" not in upper
                and "RATE" not in upper
                and not _RUN_ON_RE.search(stripped)
                and not re.fullmatch(r"TOTAL\s+" + _MONEY, stripped.upper())
                and len(stripped) < 140
            ):
                _add(summary.coverages_limits, stripped, number, limit=30)
        if _PARTY_HEADINGS.search(upper_page) and "SCHEDULE" in upper_page:
            heading_seen = False
            for line in lines:
                if _PARTY_HEADINGS.search(line):
                    heading_seen = True
                    continue
                if not heading_seen:
                    continue
                stripped = line.strip()
                if not stripped or _BOILERPLATE.search(stripped) or _FORM_ID_RE.match(stripped):
                    continue
                if re.match(r"^[A-Z][A-Z0-9&.,' \-]{4,}", stripped) and not re.search(r"[a-z]{4,}", stripped):
                    _add(summary.loss_payees_additional_insureds, stripped[:120], number, limit=12)
    return summary


def summarize_pdf(pdf_bytes: bytes) -> DecSummary:
    pages, ocr = extract_pages(pdf_bytes)
    return summarize_pages(pages, ocr)
