"""StreetSmart SOP retrieval for the Playground.

Folder ids are configurable. The exclude list is
``playground_sop_excludes.txt``, one file Carlo can edit. Copies and
same-title files keep the most recently modified one. Nothing here
reads or stores a secret.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .playground_config import sop_folders, sop_index_path
from .secrets import redact_text

_EXCLUDE_PATH = Path(__file__).with_name("playground_sop_excludes.txt")
_COPY_PREFIX = re.compile(r"^(?:copy\s+of\s+)+", re.IGNORECASE)
_COPY_SUFFIX = re.compile(r"(?:\s*[-–]\s*copy|\s+copy|\s*\(\d+\))+$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    "a an the of to for and or in on our we how what is do does with a".split()
)
_TITLE_EXTENSIONS = (
    ".tar.gz",
    ".tgz",
    ".zip",
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".gdoc",
    ".xlsx",
    ".csv",
)


class DrivePort(Protocol):
    def list_folder(self, folder_id: str) -> list[dict[str, Any]]: ...

    def export_text(self, file_id: str, mime: str) -> str: ...


@dataclass(frozen=True)
class SopHit:
    title: str
    folder: str
    excerpt: str
    doc_id: str
    modified: str = ""
    freshness_note: str = ""

    @property
    def citation(self) -> str:
        return f"{self.title} ({self.folder})"


@dataclass(frozen=True)
class ExcludeRules:
    names: tuple[str, ...] = ()
    all_words: tuple[tuple[str, ...], ...] = ()
    extensions: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    drives: frozenset[str] = field(default_factory=frozenset)
    deny_all: bool = False


def exclude_list_path() -> Path:
    return _EXCLUDE_PATH


def load_exclude_rules(path: Path | None = None) -> ExcludeRules:
    """Read the one exclude file. A missing file excludes everything."""
    source = path or _EXCLUDE_PATH
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError:
        return ExcludeRules(deny_all=True)
    names: list[str] = []
    all_words: list[tuple[str, ...]] = []
    extensions: list[str] = []
    types: list[str] = []
    texts: list[str] = []
    patterns: list[re.Pattern[str]] = []
    drives: set[str] = set()
    for line in raw.splitlines():
        body = line.split("#", 1)[0].strip()
        if not body or ":" not in body:
            continue
        kind, value = body.split(":", 1)
        kind = kind.strip().casefold()
        value = value.strip()
        if not value:
            continue
        if kind == "name":
            names.append(value.casefold())
        elif kind == "all":
            words = tuple(part.casefold() for part in value.split() if part)
            if words:
                all_words.append(words)
        elif kind == "ext":
            ext = value.casefold()
            if not ext.startswith("."):
                ext = f".{ext}"
            extensions.append(ext)
        elif kind == "type":
            types.append(value.casefold())
        elif kind == "text":
            texts.append(value.casefold())
        elif kind == "pattern":
            patterns.append(re.compile(value, re.IGNORECASE))
        elif kind == "drive":
            drives.add(value)
    extensions.sort(key=len, reverse=True)
    return ExcludeRules(
        names=tuple(names),
        all_words=tuple(all_words),
        extensions=tuple(extensions),
        types=tuple(types),
        texts=tuple(texts),
        patterns=tuple(patterns),
        drives=frozenset(drives),
    )


def is_excluded_drive_file(meta: dict[str, Any], *, folder_label: str = "") -> bool:
    """True when the exclude file matches this file's name, type, or drive."""
    del folder_label
    rules = load_exclude_rules()
    if rules.deny_all:
        return True
    name = str(meta.get("name") or "")
    folded = name.casefold()
    mime = str(meta.get("mimeType") or meta.get("mime") or "").casefold()
    drive_id = str(meta.get("driveId") or meta.get("drive_id") or "")
    parent_drive = str(meta.get("parents_drive_id") or "")
    if drive_id in rules.drives or parent_drive in rules.drives:
        return True
    if any(folded.endswith(ext) for ext in rules.extensions):
        return True
    if mime and any(kind in mime for kind in rules.types):
        return True
    if any(phrase in folded for phrase in rules.names):
        return True
    if any(all(word in folded for word in words) for words in rules.all_words):
        return True
    if any(pattern.search(folded) for pattern in rules.patterns):
        return True
    if any(phrase in folded for phrase in rules.texts):
        return True
    return False


def text_is_excluded(text: str) -> bool:
    """True when the procedure body contains a text rule, such as LOGIN_SECRETS."""
    rules = load_exclude_rules()
    if rules.deny_all:
        return True
    folded = str(text or "").casefold()
    return any(phrase in folded for phrase in rules.texts)


def _normalize_sop_title(name: str) -> str:
    base = str(name or "").strip()
    folded = base.casefold()
    for ext in _TITLE_EXTENSIONS:
        if folded.endswith(ext):
            base = base[: -len(ext)].strip()
            folded = base.casefold()
            break
    changed = True
    while changed and base:
        changed = False
        nxt = _COPY_PREFIX.sub("", base).strip()
        nxt = _COPY_SUFFIX.sub("", nxt).strip()
        if nxt != base:
            base = nxt
            changed = True
    return " ".join(base.casefold().split())


def _is_copy_name(name: str) -> bool:
    folded = str(name or "").casefold().strip()
    return (
        folded.startswith("copy of ")
        or folded.endswith(" copy")
        or bool(re.search(r"\(\d+\)\s*$", folded))
    )


def _modified_datetime(meta: dict[str, Any]) -> datetime | None:
    raw = str(
        meta.get("modifiedTime")
        or meta.get("modified_time")
        or meta.get("modified")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dedupe_rank(item: dict[str, Any], index: int) -> tuple[float, int, int]:
    stamp = _modified_datetime(item)
    ordinal = stamp.timestamp() if stamp is not None else float("-inf")
    copy_penalty = 1 if _is_copy_name(str(item.get("name") or "")) else 0
    return (ordinal, -copy_penalty, -index)


def dedupe_documents(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest file when titles match or one name is "Copy of" the other.

    Same checksums are the same document. A newer modified time wins. When
    the times match, the file that is not named as a copy wins.
    """
    if not files:
        return []
    parent = list(range(len(files)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    by_md5: dict[str, int] = {}
    by_title: dict[str, int] = {}
    for index, item in enumerate(files):
        digest = str(item.get("md5Checksum") or item.get("md5") or "").strip()
        if digest:
            if digest in by_md5:
                union(index, by_md5[digest])
            else:
                by_md5[digest] = index
        title = _normalize_sop_title(str(item.get("name") or ""))
        if title:
            if title in by_title:
                union(index, by_title[title])
            else:
                by_title[title] = index
    groups: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(files):
        groups.setdefault(find(index), []).append((index, item))
    winners: list[tuple[int, dict[str, Any]]] = []
    for members in groups.values():
        _best_index, best = max(members, key=lambda pair: _dedupe_rank(pair[1], pair[0]))
        winners.append((min(index for index, _item in members), best))
    winners.sort(key=lambda pair: pair[0])
    return [item for _index, item in winners]


def guide_freshness_note(modified: str, *, now: datetime | None = None) -> str:
    """A short note when the cited file was last changed 12 months ago or more."""
    when = _modified_datetime({"modifiedTime": modified})
    if when is None:
        return ""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    threshold = _months_before(moment, 12)
    if when > threshold:
        return ""
    return f"(guide last updated {when.year} — double-check)"


def _months_before(moment: datetime, months: int) -> datetime:
    year = moment.year
    month = moment.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = moment.day
    while True:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1


def collect_documents(drive: DrivePort) -> list[dict[str, Any]]:
    """List configured folders, drop excludes, and drop duplicate copies."""
    collected: list[dict[str, Any]] = []
    for label, folder_id in sop_folders():
        for meta in drive.list_folder(folder_id) or []:
            row = dict(meta)
            row["folder_label"] = label
            row["folder_id"] = folder_id
            if is_excluded_drive_file(row, folder_label=label):
                continue
            collected.append(row)
    return dedupe_documents(collected)


def ingest_sops(drive: DrivePort, index_path: str | None = None) -> dict[str, Any]:
    """Write a local index of procedure text. Folder ids only; no secret values."""
    path = index_path if index_path is not None else sop_index_path()
    if not path:
        raise RuntimeError("ROBIE_PLAYGROUND_SOP_INDEX is not set")
    docs = []
    for meta in collect_documents(drive):
        file_id = str(meta.get("id") or "").strip()
        if not file_id:
            continue
        mime = str(meta.get("mimeType") or meta.get("mime") or "")
        text = redact_text(drive.export_text(file_id, mime) or "")
        if text_is_excluded(text):
            continue
        modified = str(
            meta.get("modifiedTime") or meta.get("modified_time") or meta.get("modified") or ""
        )
        docs.append(
            {
                "doc_id": file_id,
                "title": str(meta.get("name") or "Untitled procedure"),
                "folder": str(meta.get("folder_label") or ""),
                "text": text,
                "modified": modified,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    payload = {"documents": docs}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return {"count": len(docs), "path": path}


def load_index(index_path: str | None = None) -> list[dict[str, Any]]:
    path = index_path if index_path is not None else sop_index_path()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    docs = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        return []
    return [item for item in docs if isinstance(item, dict)]


def retrieve_sop(
    question: str,
    docs: list[dict[str, Any]] | None = None,
    *,
    limit: int = 1,
    now: datetime | None = None,
) -> list[SopHit]:
    """Keyword overlap. No model call. Empty when nothing loaded matches."""
    corpus = docs if docs is not None else load_index()
    words = [word for word in _WORD.findall(question.casefold()) if word not in _STOPWORDS and len(word) > 2]
    if not words or not corpus:
        return []
    scored: list[tuple[int, SopHit]] = []
    for doc in corpus:
        text = str(doc.get("text") or "")
        title = str(doc.get("title") or "Procedure")
        haystack = f"{title}\n{text}".casefold()
        score = sum(1 for word in words if word in haystack)
        if score <= 0:
            continue
        excerpt = _excerpt(text, words) or title
        modified = str(doc.get("modified") or doc.get("modifiedTime") or "")
        scored.append(
            (
                score,
                SopHit(
                    title=title,
                    folder=str(doc.get("folder") or "procedures"),
                    excerpt=excerpt,
                    doc_id=str(doc.get("doc_id") or ""),
                    modified=modified,
                    freshness_note=guide_freshness_note(modified, now=now),
                ),
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in scored[: max(1, limit)]]


def build_drive_port() -> DrivePort:
    """Read-only Drive client from Application Default Credentials.

    Folder ids come from config. No secret values are read or stored here.
    """
    from google.auth import default as google_auth_default
    from googleapiclient.discovery import build

    credentials, _project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _GoogleDrivePort(service)


class _GoogleDrivePort:
    def __init__(self, service: Any) -> None:
        self._service = service

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        query = f"'{folder_id}' in parents and trashed=false"
        listed: list[dict[str, Any]] = []
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=query,
                    fields=(
                        "nextPageToken, files(id, name, mimeType, md5Checksum, "
                        "size, driveId, modifiedTime)"
                    ),
                    pageSize=100,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            listed.extend(response.get("files") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return listed

    def export_text(self, file_id: str, mime: str) -> str:
        if mime == "application/vnd.google-apps.document":
            data = self._service.files().export(fileId=file_id, mimeType="text/plain").execute()
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            return str(data or "")
        data = self._service.files().get_media(fileId=file_id).execute()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ingest Playground SOP folders")
    parser.add_argument("--index", default="")
    args = parser.parse_args(argv)
    try:
        result = ingest_sops(build_drive_port(), index_path=args.index or None)
    except Exception as exc:
        print(f"SOP ingest did not finish ({type(exc).__name__}).")
        return 1
    print(f"Loaded {result['count']} procedures.")
    return 0


def _excerpt(text: str, words: list[str]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        folded = line.casefold()
        if any(word in folded for word in words):
            return line[:400]
    if lines:
        return lines[0][:400]
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
