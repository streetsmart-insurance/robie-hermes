from __future__ import annotations

import argparse
import base64
import os
import subprocess
import time
from pathlib import Path

from .recording_tab import (
    TabCandidate,
    read_page_hint,
    select_recording_tab,
    write_attach_log,
)


SENSITIVE_CAPTURE_SELECTOR = ",".join(
    (
        "input[type='password']",
        "input[autocomplete='one-time-code']",
        "input[autocomplete^='cc-']",
        "[data-robie-sensitive]",
        "input[name*='password' i]",
        "input[name*='mfa' i]",
        "input[name*='otp' i]",
        "input[name*='token' i]",
        "input[name*='card' i]",
        "input[name*='cvv' i]",
        "input[name*='cvc' i]",
        "input[name*='ssn' i]",
    )
)


def _mask_sensitive_fields(page) -> None:
    """Visually mask secrets/regulated fields without reading their values."""
    script = """selector => {
        const styleId = 'robie-capture-mask-style';
        if (!document.getElementById(styleId)) {
          const style = document.createElement('style');
          style.id = styleId;
          style.textContent = '[data-robie-capture-masked="true"] {' +
            'color: transparent !important; text-shadow: 0 0 12px #000 !important;' +
            'caret-color: transparent !important; background-image: none !important;}';
          document.documentElement.appendChild(style);
        }
        document.querySelectorAll(selector).forEach(
          element => element.setAttribute('data-robie-capture-masked', 'true')
        );
    }"""
    for frame in page.frames:
        try:
            frame.evaluate(script, SENSITIVE_CAPTURE_SELECTOR)
        except Exception:
            # Cross-origin or transient frames are excluded until they can be
            # inspected safely; never read field values into the recorder.
            continue


def _remove_capture_masks(page) -> None:
    script = """() => {
        document.querySelectorAll('[data-robie-capture-masked]').forEach(
          element => element.removeAttribute('data-robie-capture-masked')
        );
        document.getElementById('robie-capture-mask-style')?.remove();
    }"""
    for frame in page.frames:
        try:
            frame.evaluate(script)
        except Exception:
            continue


def _page_identity(page) -> str:
    guid = getattr(page, "_guid", None) or getattr(page, "guid", None)
    return str(guid or id(page))


def _hint_url(hint_file: Path | None) -> str | None:
    data = read_page_hint(hint_file)
    if not data:
        return None
    url = str(data.get("url") or "").strip()
    return url or None


def _list_tabs(browser, nav_times: dict[str, float]) -> list[tuple[TabCandidate, object]]:
    now = time.monotonic()
    listed: list[tuple[TabCandidate, object]] = []
    for context in browser.contexts:
        for page in context.pages:
            identity = _page_identity(page)
            nav_times.setdefault(identity, now)
            listed.append(
                (
                    TabCandidate(
                        identity=identity,
                        url=page.url,
                        last_navigated_at=nav_times[identity],
                    ),
                    page,
                )
            )
    return listed


def _note_url_changes(
    listed: list[tuple[TabCandidate, object]],
    nav_times: dict[str, float],
    last_urls: dict[str, str],
) -> list[tuple[TabCandidate, object]]:
    now = time.monotonic()
    refreshed: list[tuple[TabCandidate, object]] = []
    for candidate, live in listed:
        previous = last_urls.get(candidate.identity)
        if previous is not None and previous != candidate.url:
            nav_times[candidate.identity] = now
        last_urls[candidate.identity] = candidate.url
        refreshed.append(
            (
                TabCandidate(
                    identity=candidate.identity,
                    url=candidate.url,
                    last_navigated_at=nav_times[candidate.identity],
                    title=candidate.title,
                ),
                live,
            )
        )
    return refreshed


def _watch_navigations(
    listed: list[tuple[TabCandidate, object]],
    nav_times: dict[str, float],
    watched: set[str],
) -> None:
    for candidate, live in listed:
        if candidate.identity in watched:
            continue
        try:
            live.on(
                "framenavigated",
                lambda _frame, key=candidate.identity: nav_times.__setitem__(
                    key, time.monotonic()
                ),
            )
            watched.add(candidate.identity)
        except Exception:
            continue


def _live_for(
    listed: list[tuple[TabCandidate, object]],
    chosen: TabCandidate | None,
    default=None,
):
    if chosen is None:
        return default
    for candidate, live in listed:
        if candidate.identity == chosen.identity:
            return live
    return default


def _bind_screencast(page, ffmpeg, stop_file, ready_file, first_frame_written):
    session = page.context.new_cdp_session(page)
    state = {"first_frame_written": first_frame_written, "session": session}

    def on_frame(params):
        try:
            if ffmpeg.stdin and not stop_file.exists():
                ffmpeg.stdin.write(base64.b64decode(params["data"]))
                ffmpeg.stdin.flush()
                if not state["first_frame_written"]:
                    ready_file.touch(mode=0o600, exist_ok=True)
                    state["first_frame_written"] = True
        finally:
            session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    session.on("Page.screencastFrame", on_frame)
    session.send(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "quality": 68,
            "maxWidth": 1280,
            "maxHeight": 720,
            "everyNthFrame": 1,
        },
    )
    return state


def capture(
    cdp_url: str,
    output: Path,
    stop_file: Path,
    ready_file: Path,
    fps: int,
    *,
    hint_file: Path | None = None,
) -> None:
    from playwright.sync_api import sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hint_file = hint_file or Path(
        os.environ.get("ROBIE_RECORDING_HINT_FILE")
        or str(output.with_suffix(".hint.json"))
    )
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-framerate", str(fps), "-vcodec", "mjpeg",
            "-i", "pipe:0", "-an", "-c:v", "libvpx-vp9", "-deadline", "realtime",
            "-cpu-used", "8", "-b:v", "900k", str(output),
        ],
        stdin=subprocess.PIPE,
    )
    attach = {
        "initial_url": "",
        "final_url": "",
        "attached_urls": [],
        "rebinds": 0,
        "selection_mode": "recent_navigation",
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            deadline = time.monotonic() + 20
            nav_times: dict[str, float] = {}
            last_urls: dict[str, str] = {}
            watched: set[str] = set()
            page = None
            while page is None and time.monotonic() < deadline and not stop_file.exists():
                listed = _note_url_changes(
                    _list_tabs(browser, nav_times), nav_times, last_urls
                )
                _watch_navigations(listed, nav_times, watched)
                chosen = select_recording_tab(
                    [item[0] for item in listed],
                    hint_url=_hint_url(hint_file),
                )
                page = _live_for(listed, chosen)
                if page is None:
                    time.sleep(0.25)
            if page is None:
                raise RuntimeError("no Chrome tab was available for recording")
            attach["initial_url"] = page.url
            attach["attached_urls"].append(page.url)
            if _hint_url(hint_file):
                attach["selection_mode"] = "hint"
            _mask_sensitive_fields(page)
            bound = _bind_screencast(page, ffmpeg, stop_file, ready_file, False)
            session = bound["session"]
            try:
                while not stop_file.exists():
                    # Playwright's synchronous API dispatches CDP event callbacks
                    # while it is inside a Playwright call. A plain time.sleep here
                    # starves Page.screencastFrame and produces an empty video.
                    listed = _note_url_changes(
                        _list_tabs(browser, nav_times), nav_times, last_urls
                    )
                    _watch_navigations(listed, nav_times, watched)
                    chosen = select_recording_tab(
                        [item[0] for item in listed],
                        previous_identity=_page_identity(page),
                        hint_url=_hint_url(hint_file),
                    )
                    next_page = _live_for(listed, chosen, page)
                    if next_page is not page:
                        try:
                            session.send("Page.stopScreencast")
                        except Exception:
                            pass
                        _remove_capture_masks(page)
                        page = next_page
                        attach["rebinds"] += 1
                        attach["attached_urls"].append(page.url)
                        attach["selection_mode"] = (
                            "hint" if _hint_url(hint_file) else "recent_navigation"
                        )
                        _mask_sensitive_fields(page)
                        bound = _bind_screencast(
                            page,
                            ffmpeg,
                            stop_file,
                            ready_file,
                            bound["first_frame_written"],
                        )
                        session = bound["session"]
                    else:
                        _mask_sensitive_fields(page)
                    page.wait_for_timeout(250)
                try:
                    session.send("Page.stopScreencast")
                except Exception:
                    pass
                page.wait_for_timeout(500)
                attach["final_url"] = page.url
                if page.url not in attach["attached_urls"]:
                    attach["attached_urls"].append(page.url)
            finally:
                _remove_capture_masks(page)
                try:
                    write_attach_log(output, attach)
                except Exception:
                    pass
    finally:
        if ffmpeg.stdin:
            ffmpeg.stdin.close()
        ffmpeg.wait(timeout=15)
        if ffmpeg.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {ffmpeg.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record ROBIE's browser tab through CDP")
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--fps", type=int, default=4)
    parser.add_argument("--hint-file", default="")
    args = parser.parse_args()
    capture(
        args.cdp_url,
        Path(args.output),
        Path(args.stop_file),
        Path(args.ready_file),
        args.fps,
        hint_file=Path(args.hint_file) if args.hint_file else None,
    )


if __name__ == "__main__":
    main()
