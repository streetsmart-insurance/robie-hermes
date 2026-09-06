"""Gemini video inputs must include processing=agentic (or the configured mode)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient
from robie_job_engine.gemini_video import (
    DEFAULT_VIDEO_PROCESSING,
    generate_content_video_part,
    interactions_video_input,
    is_gemini_video_input,
    stamp_video_processing,
    video_processing_mode,
)


class GeminiVideoProcessingTests(unittest.TestCase):
    def test_default_mode_is_agentic(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_VIDEO_PROCESSING", None)
            self.assertEqual(video_processing_mode(), "agentic")
            self.assertEqual(DEFAULT_VIDEO_PROCESSING, "agentic")

    def test_env_override_and_rejection(self):
        with patch.dict(os.environ, {"GEMINI_VIDEO_PROCESSING": "static"}):
            self.assertEqual(video_processing_mode(), "static")
        with patch.dict(os.environ, {"GEMINI_VIDEO_PROCESSING": "AGENTIC"}):
            self.assertEqual(video_processing_mode(), "agentic")
        with patch.dict(os.environ, {"GEMINI_VIDEO_PROCESSING": "fps"}):
            with self.assertRaisesRegex(ValueError, "agentic or static"):
                video_processing_mode()

    def test_interactions_youtube_and_upload_include_agentic(self):
        youtube = interactions_video_input(uri="https://youtu.be/7Z5Vy9JBANs")
        self.assertEqual(youtube["type"], "video")
        self.assertEqual(youtube["uri"], "https://youtu.be/7Z5Vy9JBANs")
        self.assertEqual(youtube["processing"], "agentic")

        uploaded = interactions_video_input(
            uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
            mime_type="video/mp4",
        )
        self.assertEqual(uploaded["processing"], "agentic")
        self.assertEqual(uploaded["mime_type"], "video/mp4")

        inline = interactions_video_input(data="ZmFrZS1ieXRlcw==", mime_type="video/webm")
        self.assertEqual(inline["processing"], "agentic")
        self.assertEqual(inline["data"], "ZmFrZS1ieXRlcw==")

    def test_generate_content_parts_use_documented_rest_field(self):
        uri_part = generate_content_video_part(
            file_uri="gs://bucket/lecture.mp4",
            mime_type="video/mp4",
        )
        self.assertEqual(uri_part["mediaProcessing"], "AGENTIC")
        self.assertEqual(uri_part["fileData"]["fileUri"], "gs://bucket/lecture.mp4")
        self.assertEqual(uri_part["fileData"]["mimeType"], "video/mp4")
        self.assertNotIn("processing", uri_part)

        inline = generate_content_video_part(
            inline_data="ZmFrZQ==",
            mime_type="video/webm",
        )
        self.assertEqual(inline["mediaProcessing"], "AGENTIC")
        self.assertEqual(inline["inlineData"]["mimeType"], "video/webm")

    def test_stamp_adds_agentic_to_every_video_shape(self):
        payload = {
            "model": "gemini-3.7-flash",
            "input": [
                {"type": "video", "uri": "https://youtu.be/example"},
                {"type": "text", "text": "Summarize this Loom"},
                {
                    "type": "video",
                    "uri": "https://www.loom.com/share/abc",
                    "processing": "static",
                },
            ],
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "fileData": {
                                "fileUri": "/tmp/job.webm",
                                "mimeType": "video/webm",
                            }
                        },
                        {"text": "What happened?"},
                    ],
                }
            ],
        }
        stamped = stamp_video_processing(payload)
        self.assertIs(stamped, payload)
        self.assertEqual(payload["input"][0]["processing"], "agentic")
        self.assertEqual(payload["input"][1], {"type": "text", "text": "Summarize this Loom"})
        self.assertEqual(payload["input"][2]["processing"], "static")
        self.assertEqual(payload["contents"][0]["parts"][0]["mediaProcessing"], "AGENTIC")
        self.assertNotIn("mediaProcessing", payload["contents"][0]["parts"][1])

    def test_is_gemini_video_input_detects_known_shapes(self):
        self.assertTrue(is_gemini_video_input({"type": "video", "uri": "https://youtu.be/x"}))
        self.assertTrue(
            is_gemini_video_input(
                {"file_data": {"file_uri": "gs://b/clip.mp4", "mime_type": "video/mp4"}}
            )
        )
        self.assertFalse(is_gemini_video_input({"type": "text", "text": "hello"}))
        self.assertFalse(is_gemini_video_input({"fileData": {"fileUri": "gs://b/doc.pdf", "mimeType": "application/pdf"}}))

    def test_explicit_static_builder_override(self):
        clip = interactions_video_input(
            uri="https://youtu.be/short",
            processing="static",
        )
        self.assertEqual(clip["processing"], "static")
        part = generate_content_video_part(
            file_uri="gs://b/short.mp4",
            processing="static",
        )
        self.assertEqual(part["mediaProcessing"], "STATIC")

    def test_builders_reject_incomplete_or_non_video_inputs(self):
        with self.assertRaisesRegex(ValueError, "uri or data"):
            interactions_video_input()
        with self.assertRaisesRegex(ValueError, "not both"):
            interactions_video_input(uri="https://youtu.be/x", data="Zg==")
        with self.assertRaisesRegex(ValueError, "video/\\*"):
            generate_content_video_part(file_uri="gs://b/doc.pdf", mime_type="application/pdf")

    def test_vertex_stuck_field_request_stays_text_only(self):
        captured: dict[str, object] = {}

        def opener(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))

            class _Resp:
                def read(self):
                    return json.dumps(
                        {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
                    ).encode("utf-8")

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return _Resp()

        client = VertexGeminiFieldClient(
            project="streetsmart-test",
            token_provider=lambda: "test-token",
            opener=opener,
        )
        client.generate_unique_field("name one field")
        body = captured["body"]
        self.assertEqual(body["contents"][0]["parts"], [{"text": "name one field"}])
        self.assertNotIn("mediaProcessing", body["contents"][0]["parts"][0])
        self.assertNotIn("processing", body["contents"][0]["parts"][0])


if __name__ == "__main__":
    unittest.main()
