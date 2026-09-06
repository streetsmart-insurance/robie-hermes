"""Shared Gemini video-input builder. Default processing is agentic.

Google launched agentic video understanding on 2026-09-06 so Gemini can
navigate a timeline instead of ingesting every frame at 1 FPS:

https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-agentic-video-in-gemini/

Interactions API (Gemini API / AI Studio)::

    {"type": "video", "uri": "...", "processing": "agentic"}

Vertex / Gemini Enterprise ``generateContent`` REST (this repo's existing
Vertex client is urllib JSON, not ``google-genai``)::

    {"fileData": {"fileUri": "...", "mimeType": "video/mp4"},
     "mediaProcessing": "AGENTIC"}

``GEMINI_VIDEO_PROCESSING`` selects the mode (default ``agentic``). New
video→Gemini call sites must use these helpers so the flag cannot be
omitted. This module does not add a ``google-genai`` dependency and does
not invent SDK fields that the current REST shapes do not document.

Out of scope (no video→Gemini call in this tree):
- Nous Hermes-agent runtime (VM ``hermes_cli`` / provider plugin; not vendored)
- ``video_to_skill.py`` (Playwright trace compiler, no model call)
- ``gemini_field_helper.py`` stuck-field path (text parts only)
- Google Chat adapter (ingest/send ``video/*`` files; does not call Gemini)
- Job recordings / ``post_job_audit`` (ffmpeg motion, not Gemini)
"""

from __future__ import annotations

import os
from typing import Any, Mapping, MutableMapping

VIDEO_PROCESSING_ENV = "GEMINI_VIDEO_PROCESSING"
DEFAULT_VIDEO_PROCESSING = "agentic"
ALLOWED_VIDEO_PROCESSING = frozenset({"agentic", "static"})


def video_processing_mode(value: str | None = None) -> str:
    """Return ``agentic`` or ``static``. Empty / unset defaults to agentic."""
    if value is None:
        raw = os.environ.get(VIDEO_PROCESSING_ENV, DEFAULT_VIDEO_PROCESSING)
    else:
        raw = value
    mode = str(raw or "").strip().casefold()
    if not mode:
        mode = DEFAULT_VIDEO_PROCESSING
    if mode not in ALLOWED_VIDEO_PROCESSING:
        raise ValueError(
            f"{VIDEO_PROCESSING_ENV} must be agentic or static, not {raw!r}"
        )
    return mode


def _rest_media_processing(mode: str) -> str:
    """Vertex generateContent REST documents ``mediaProcessing: AGENTIC``."""
    return video_processing_mode(mode).upper()


def interactions_video_input(
    *,
    uri: str | None = None,
    data: str | None = None,
    mime_type: str | None = None,
    processing: str | None = None,
) -> dict[str, Any]:
    """Build one Interactions API video input with ``processing`` set."""
    if not uri and not data:
        raise ValueError("interactions video input requires uri or data")
    if uri and data:
        raise ValueError("interactions video input accepts uri or data, not both")
    payload: dict[str, Any] = {
        "type": "video",
        "processing": video_processing_mode(processing),
    }
    if uri:
        payload["uri"] = str(uri)
    if data:
        payload["data"] = str(data)
    if mime_type:
        payload["mime_type"] = str(mime_type)
    return payload


def generate_content_video_part(
    *,
    file_uri: str | None = None,
    inline_data: str | None = None,
    mime_type: str | None = None,
    processing: str | None = None,
) -> dict[str, Any]:
    """Build one Vertex generateContent video part with ``mediaProcessing``."""
    if not file_uri and not inline_data:
        raise ValueError("generateContent video part requires file_uri or inline_data")
    if file_uri and inline_data:
        raise ValueError(
            "generateContent video part accepts file_uri or inline_data, not both"
        )
    mime = str(mime_type or "video/mp4")
    if not mime.casefold().startswith("video/"):
        raise ValueError(f"generateContent video part mime_type must be video/*, not {mime!r}")
    part: dict[str, Any] = {
        "mediaProcessing": _rest_media_processing(processing or video_processing_mode()),
    }
    if file_uri:
        part["fileData"] = {"fileUri": str(file_uri), "mimeType": mime}
    else:
        part["inlineData"] = {"data": str(inline_data), "mimeType": mime}
    return part


def _mime_from_mapping(value: Mapping[str, Any]) -> str:
    for key in ("mime_type", "mimeType", "mime"):
        raw = value.get(key)
        if raw:
            return str(raw)
    return ""


def _is_video_mime(mime: str) -> bool:
    return str(mime or "").casefold().startswith("video/")


def _looks_like_video_uri(uri: str) -> bool:
    text = str(uri or "").casefold()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "youtu.be/",
            "youtube.com/",
            "loom.com/",
            ".mp4",
            ".webm",
            ".mov",
            "video/",
        )
    )


def _nested_video_blob(part: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("fileData", "file_data", "inlineData", "inline_data"):
        blob = part.get(key)
        if isinstance(blob, Mapping):
            return blob
    return None


def is_gemini_video_input(value: Any) -> bool:
    """True for Interactions video inputs or generateContent video parts."""
    if not isinstance(value, Mapping):
        return False
    if str(value.get("type") or "").casefold() == "video":
        return True
    nested = _nested_video_blob(value)
    if nested is None:
        return False
    nested_mime = _mime_from_mapping(nested)
    if _is_video_mime(nested_mime):
        return True
    uri = str(nested.get("fileUri") or nested.get("file_uri") or nested.get("uri") or "")
    return _looks_like_video_uri(uri)


def _apply_processing(item: MutableMapping[str, Any], mode: str) -> None:
    """Set the documented field for the payload shape. Do not invent extras."""
    if str(item.get("type") or "").casefold() == "video":
        if not str(item.get("processing") or "").strip():
            item["processing"] = mode
        return
    if "media_processing" in item and "mediaProcessing" not in item:
        if not str(item.get("media_processing") or "").strip():
            item["media_processing"] = mode
        return
    if not str(item.get("mediaProcessing") or item.get("media_processing") or "").strip():
        item["mediaProcessing"] = _rest_media_processing(mode)


def stamp_video_processing(payload: Any, *, processing: str | None = None) -> Any:
    """Walk a request and add processing on every video input that lacks it.

    Existing explicit ``processing`` / ``mediaProcessing`` values are kept so
    a caller can mix agentic and static videos in one request.
    """
    mode = video_processing_mode(processing)
    if isinstance(payload, list):
        for item in payload:
            stamp_video_processing(item, processing=mode)
        return payload
    if not isinstance(payload, MutableMapping):
        return payload
    if is_gemini_video_input(payload):
        _apply_processing(payload, mode)
    for value in payload.values():
        if isinstance(value, (list, dict)):
            stamp_video_processing(value, processing=mode)
    return payload
