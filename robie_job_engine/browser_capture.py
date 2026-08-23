from __future__ import annotations

import argparse
import base64
import subprocess
import time
from pathlib import Path


def capture(cdp_url: str, output: Path, stop_file: Path, fps: int) -> None:
    from playwright.sync_api import sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-framerate", str(fps), "-vcodec", "mjpeg",
            "-i", "pipe:0", "-an", "-c:v", "libvpx-vp9", "-deadline", "realtime",
            "-cpu-used", "8", "-b:v", "900k", str(output),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            deadline = time.monotonic() + 20
            page = None
            while page is None and time.monotonic() < deadline and not stop_file.exists():
                pages = [p for context in browser.contexts for p in context.pages]
                preferred = [p for p in pages if "ezlynx.com" in p.url.casefold()]
                page = (preferred or [p for p in pages if p.url != "about:blank"] or pages or [None])[0]
                if page is None:
                    time.sleep(0.25)
            if page is None:
                raise RuntimeError("no Chrome tab was available for recording")
            session = page.context.new_cdp_session(page)

            def on_frame(params):
                try:
                    if ffmpeg.stdin and not stop_file.exists():
                        ffmpeg.stdin.write(base64.b64decode(params["data"]))
                        ffmpeg.stdin.flush()
                finally:
                    session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

            session.on("Page.screencastFrame", on_frame)
            session.send("Page.startScreencast", {
                "format": "jpeg", "quality": 68, "maxWidth": 1280,
                "maxHeight": 720, "everyNthFrame": 1,
            })
            while not stop_file.exists():
                # Playwright's synchronous API dispatches CDP event callbacks
                # while it is inside a Playwright call. A plain time.sleep here
                # starves Page.screencastFrame and produces an empty video.
                page.wait_for_timeout(250)
            session.send("Page.stopScreencast")
            page.wait_for_timeout(500)
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
    parser.add_argument("--fps", type=int, default=4)
    args = parser.parse_args()
    capture(args.cdp_url, Path(args.output), Path(args.stop_file), args.fps)


if __name__ == "__main__":
    main()
