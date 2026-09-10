#!/usr/bin/env python3
import sys
import time
import json
import logging
from src.voice.voice_client import CarrierVoiceClient
from src.voice.call_completion import handle_completed_call, fetch_bland_call_details

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("monitor_call")

def monitor(call_id: str, max_wait_sec: int = 600):
    client = CarrierVoiceClient()
    logger.info(f"Starting monitoring for Bland AI call {call_id} (timeout={max_wait_sec}s)...")
    start_time = time.time()
    last_trans_count = 0

    while time.time() - start_time < max_wait_sec:
        details = fetch_bland_call_details(call_id, client)
        status = (details.get("status") or details.get("queue_status") or "unknown").lower()
        transcripts = details.get("transcripts") or details.get("concatenated_transcript") or []
        
        if isinstance(transcripts, list) and len(transcripts) > last_trans_count:
            for t in transcripts[last_trans_count:]:
                user = t.get("user", "speaker")
                text = t.get("text", "")
                logger.info(f"[{user}]: {text}")
            last_trans_count = len(transcripts)

        logger.info(f"Call {call_id} status: {status} (elapsed: {int(time.time() - start_time)}s)")
        
        if status in ("completed", "ended", "failed", "error", "no-answer", "busy"):
            logger.info(f"Call {call_id} has concluded with status: {status}")
            break
            
        time.sleep(10)

    # Fetch final details
    final_details = fetch_bland_call_details(call_id, client)
    logger.info("Final call details fetched. Executing handle_completed_call...")
    result = handle_completed_call(final_details)
    logger.info("Completed call handled: %s", json.dumps(result))
    return result

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: monitor_bland_call_and_complete.py <call_id>")
        sys.exit(1)
    monitor(sys.argv[1])
