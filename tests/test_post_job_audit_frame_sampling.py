"""Regression tests: post_job_audit samples frames across the full timeline.

Guards against a recurrence of the 2026-09-18 Test failure mode, where
``extract_video_frames`` ran ffmpeg with plain ``-frames:v N`` and no fps /
select filter. That only returns the *first* N decoded frames (~3s at 4fps),
so long Looker jobs whose motion happens after page load false-failed the
recording-motion check and their post-job audits could never PASS.

The fix probes the video duration and, for videos longer than 0.5s, adds an
``fps=<max_frames/duration>`` filter so the N extracted frames are spread
evenly across the whole recording.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import robie_job_engine.post_job_audit as pja


def _tiny_ppm() -> bytes:
    # 2x2 binary PPM, maxval 255.
    return b"P6\n2 2\n255\n" + b"\x00" * 12


def _run_extract(duration_stderr: bytes) -> list[str]:
    """Run extract_video_frames with ffmpeg mocked; return the -vf argument."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-f" in cmd and "null" in cmd:
            # Duration probe.
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"", stderr=duration_stderr
            )
        if "image2pipe" in cmd and "-vf" not in cmd:
            # Decode probe.
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_tiny_ppm(), stderr=b""
            )
        # Frame extraction.
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_tiny_ppm() + _tiny_ppm(), stderr=b""
        )

    video = Path("/tmp/fake-recording.webm")
    with (
        mock.patch.object(pja.subprocess, "run", side_effect=fake_run),
        mock.patch.object(pja.shutil, "which", return_value="/usr/bin/ffmpeg"),
        mock.patch.object(Path, "is_file", return_value=True),
        mock.patch.object(Path, "stat") as mock_stat,
    ):
        mock_stat.return_value.st_size = 1024
        pja.extract_video_frames(video, max_frames=12)

    vf_args = [c[c.index("-vf") + 1] for c in calls if "-vf" in c]
    assert vf_args, "expected an ffmpeg extraction call with -vf"
    return vf_args


def test_long_video_samples_across_full_timeline():
    vf = _run_extract(b"Duration: 00:02:00.00, start: 0.000000")[0]
    # 12 frames / 120 s = 0.1 fps.
    assert vf == "fps=0.100000,scale=160:90", vf


def test_short_video_falls_back_to_plain_scale():
    vf = _run_extract(b"Duration: 00:00:00.20, start: 0.000000")[0]
    assert vf == "scale=160:90", vf


def test_unparseable_duration_falls_back_to_plain_scale():
    vf = _run_extract(b"no duration here")[0]
    assert vf == "scale=160:90", vf


def test_extraction_still_uses_frame_cap():
    """The fps filter must not drop the -frames:v cap (bounded work)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-f" in cmd and "null" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"", stderr=b"Duration: 00:01:00.00"
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_tiny_ppm(), stderr=b""
        )

    video = Path("/tmp/fake-recording.webm")
    with (
        mock.patch.object(pja.subprocess, "run", side_effect=fake_run),
        mock.patch.object(pja.shutil, "which", return_value="/usr/bin/ffmpeg"),
        mock.patch.object(Path, "is_file", return_value=True),
        mock.patch.object(Path, "stat") as mock_stat,
    ):
        mock_stat.return_value.st_size = 1024
        pja.extract_video_frames(video, max_frames=12)

    extraction = [c for c in calls if "-frames:v" in c]
    assert extraction, "expected an extraction call"
    assert extraction[0][extraction[0].index("-frames:v") + 1] == "12"


def test_extract_video_frames_docstring_promises_even_spacing():
    assert "evenly" in (pja.extract_video_frames.__doc__ or "").lower()
    src = inspect.getsource(pja.extract_video_frames)
    assert "fps=" in src, "frame sampling must use an fps filter"
