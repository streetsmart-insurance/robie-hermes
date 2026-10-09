"""Render one Gmail message to a plain-text PDF for EZLynx Documents.

The PDF holds the headers, the body as text, and the attachment list.
It is written with no extra library: Helvetica, Letter size, wrapped
lines. Characters outside Latin-1 print as ``?``. The original
attachments are filed as their own documents, so nothing is lost.
"""

from __future__ import annotations

import html
import re
import textwrap
from base64 import urlsafe_b64decode
from typing import Any, Iterable

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN = 54
FONT_SIZE = 9.5
LEADING = 12
WRAP_COLUMNS = 105
LINES_PER_PAGE = int((PAGE_HEIGHT - 2 * MARGIN) / LEADING)
HEADER_NAMES = ("From", "To", "Cc", "Date", "Subject", "Message-ID")


def header(message: dict[str, Any], name: str) -> str:
    for item in (message.get("payload") or {}).get("headers") or []:
        if str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value") or "")
    return ""


def iter_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [payload]
    while stack:
        part = stack.pop(0)
        yield part
        stack.extend(part.get("parts") or [])


def _decode(data: str) -> str:
    return urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def body_text(message: dict[str, Any]) -> str:
    """Plain text part if present, else the HTML part as text."""

    plain, rich = [], []
    for part in iter_parts(message.get("payload") or {}):
        if part.get("filename"):
            continue
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = str(part.get("mimeType") or "")
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(html_to_text(_decode(data)))
    return "\n".join(plain).strip() or "\n".join(rich).strip()


def attachment_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Real attachments. Small inline images (signature logos) are skipped."""

    found = []
    for part in iter_parts(message.get("payload") or {}):
        name = str(part.get("filename") or "").strip()
        body = part.get("body") or {}
        if not name or not (body.get("attachmentId") or body.get("data")):
            continue
        mime = str(part.get("mimeType") or "")
        disposition = header({"payload": part}, "Content-Disposition").lower()
        if mime.startswith("image/") and int(body.get("size") or 0) < 30_000 and "attachment" not in disposition:
            continue
        found.append(part)
    return found


def email_lines(message: dict[str, Any]) -> list[str]:
    lines = [f"{name}: {header(message, name)}" for name in HEADER_NAMES if header(message, name)]
    names = [str(p.get("filename")) for p in attachment_parts(message)]
    lines.append("Attachments: " + (", ".join(names) if names else "none"))
    lines.append("Gmail message id: " + str(message.get("id") or ""))
    lines.append("-" * 80)
    for raw in (body_text(message) or "(no text body)").splitlines():
        wrapped = textwrap.wrap(raw, WRAP_COLUMNS, replace_whitespace=False, drop_whitespace=True)
        lines.extend(wrapped or [""])
    return lines


def _pdf_escape(text: str) -> bytes:
    data = text.encode("latin-1", "replace")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def lines_to_pdf(lines: list[str]) -> bytes:
    """A valid multi-page PDF with one text line per row."""

    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, max(len(lines), 1), LINES_PER_PAGE)] or [[]]
    objects: list[bytes] = []
    font_id = 3
    page_ids = []
    content_ids = []
    next_id = 4
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objects.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(pages)).encode() + b" >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for page_lines, page_id, content_id in zip(pages, page_ids, content_ids):
        stream = [f"BT /F1 {FONT_SIZE} Tf {LEADING} TL {MARGIN} {PAGE_HEIGHT - MARGIN} Td".encode()]
        for line in page_lines:
            stream.append(b"(" + _pdf_escape(line) + b") '")
        stream.append(b"ET")
        body = b"\n".join(stream)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def render_email_pdf(message: dict[str, Any]) -> bytes:
    return lines_to_pdf(email_lines(message))
