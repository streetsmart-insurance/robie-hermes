"""
Post-Call Webhook Receiver for Autonomous Carrier Voice Engine.
Receives completed call transcripts and recordings from Bland AI / Retell AI,
auto-threads notes into EZLynx discussion cards, and notifies assigned CSRs.
Built using standard library http.server for zero external dependencies.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict

from src.voice.call_completion import extract_transfer_outcome, handle_completed_call

logger = logging.getLogger("voice_webhook_server")

__all__ = [
    "VoiceWebhookRequestHandler",
    "extract_transfer_outcome",
    "handle_completed_call",
    "run_webhook_server",
]


class VoiceWebhookRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/webhook/voice/call-completed"):
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON", "details": str(e)}).encode("utf-8"))
                return

            call_data = payload.get("call", payload)
            # Run processing in background thread
            threading.Thread(target=handle_completed_call, args=(call_data,), daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "received", "call_id": call_data.get("call_id")}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "robie_voice_webhook"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_webhook_server(host: str = "0.0.0.0", port: int = 8088):
    server = HTTPServer((host, port), VoiceWebhookRequestHandler)
    logger.info(f"Starting Robie Voice Webhook server on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_webhook_server()
