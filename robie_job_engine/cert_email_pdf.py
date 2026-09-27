"""Render a certificate-request email as a PDF for EZLynx Documents.

Stdlib only — no fpdf/reportlab dependency, so it runs unchanged on the
production box. Output is a plain multi-page text PDF (Courier) carrying
the complete original email: headers, body, and attachment manifest.

Carlo 2026-09-27: the email itself must live in the applicant's Documents
so the certificates team never has to dig through the inbox for it.
"""

from __future__ import annotations

import re
from typing import Any

_LINES_PER_PAGE = 66
_CHARS_PER_LINE = 100


def _safe_filename_token(subject: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(subject or "")).strip("-")
    return token[:80] or "email"


def email_pdf_document(email: Any) -> tuple[str, bytes]:
    """Return (document_name, pdf_bytes) for the full original email.

    Naming matches the convention Steffany already sees in Documents:
    ``COI request email - <subject>.pdf``.
    """
    return (
        f"COI request email - {_safe_filename_token(getattr(email, 'subject', ''))}.pdf",
        email_to_pdf_bytes(email),
    )


def _email_text(email: Any) -> str:
    def g(name: str, default: str = "") -> str:
        return str(getattr(email, name, default) or default)

    lines = [
        f"From: {g('from_header')}",
        f"Date: {g('date')}",
        f"Subject: {g('subject')}",
        f"Gmail-ID: {g('gmail_id')}",
        f"Message-ID: {g('rfc_message_id')}",
        "",
        g("body_text") or "(no text body)",
        "",
        "Attachments:",
    ]
    attachments = getattr(email, "attachments", None) or []
    if attachments:
        for att in attachments:
            an = str(getattr(att, "filename", "") or "")
            asize = getattr(att, "size", None)
            ahash = str(getattr(att, "sha256", "") or "")
            lines.append(f"- {an} ({asize} bytes, sha256 {ahash})")
    else:
        lines.append("(none)")
    return "\n".join(lines).strip() + "\n"


def _wrap(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.expandtabs(4)
        while len(line) > _CHARS_PER_LINE:
            cut = line.rfind(" ", 0, _CHARS_PER_LINE)
            cut = cut if cut > _CHARS_PER_LINE // 2 else _CHARS_PER_LINE
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def _pdf_escape(text: str) -> str:
    # WinAnsi (latin-1); anything outside becomes "?" rather than breaking
    # the stream.
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def email_to_pdf_bytes(email: Any) -> bytes:
    """Render the email as PDF bytes. Raises on empty content, never silently."""
    lines = _wrap(_email_text(email))
    if not any(line.strip() for line in lines):
        raise ValueError("refusing to render an empty email to PDF")

    pages: list[list[str]] = [
        lines[i : i + _LINES_PER_PAGE]
        for i in range(0, len(lines), _LINES_PER_PAGE)
    ]

    objects: list[bytes] = []
    # 1: catalog, 2: pages, 3: font — page objects and streams follow.
    content_ids: list[int] = []
    next_id = 4
    page_ids: list[int] = []
    for page_lines in pages:
        stream_lines = ["BT", "/F1 9 Tf", "40 780 Td", "11 TL"]
        for line in page_lines:
            stream_lines.append(f"({_pdf_escape(line)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1")
        cid = next_id
        next_id += 1
        pid = next_id
        next_id += 1
        content_ids.append(cid)
        page_ids.append(pid)
        objects.append(
            f"{cid} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream
            + b"\nendstream\nendobj\n"
        )
        objects.append(
            f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] /Contents {cid} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>\nendobj\n".encode("latin-1")
        )

    header = b"%PDF-1.4\n"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    fixed = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n".encode(
            "latin-1"
        ),
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>\nendobj\n",
    ]
    body = header + b"".join(fixed + objects)
    offsets = [0]
    pos = len(header)
    for obj in fixed + objects:
        offsets.append(pos)
        pos += len(obj)
    xref_pos = pos
    xref = [f"xref\n0 {len(offsets)}\n".encode("latin-1")]
    xref.append(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode("latin-1"))
    xref.append(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return body + b"".join(xref)
