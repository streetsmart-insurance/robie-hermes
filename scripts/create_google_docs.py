#!/usr/bin/env python3
"""Creates Google Docs for Iteration 1 Blueprint, Role Super-Prompts, and Weekly Scorecard."""

import json
import subprocess
import time
from pathlib import Path

ROBIE_DIR = Path("/Users/carloferrara/Documents/Hermes Projects/robie-hermes")

docs_to_create = [
    {
        "title": "StreetSmart Productivity & Accountability System — Iteration 1 Blueprint",
        "file": ROBIE_DIR / "docs" / "STREETSMART_PRODUCTIVITY_AND_ACCOUNTABILITY_SYSTEM.md"
    },
    {
        "title": "StreetSmart Role-Based Accountability Super-Prompt Architecture",
        "file": ROBIE_DIR / "docs" / "STREETSMART_ROLE_BASED_ACCOUNTABILITY_SUPER_PROMPT.md"
    },
    {
        "title": "StreetSmart Weekly Executive Performance & Forensic Scorecard (Iteration 1 Data)",
        "file": ROBIE_DIR / "docs" / "STREETSMART_WEEKLY_PERFORMANCE_SCORECARD.md"
    }
]

created_links = []

for item in docs_to_create:
    title = item["title"]
    filepath = item["file"]
    content = filepath.read_text(encoding="utf-8")
    
    print(f"Creating Google Doc: {title}...")
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to open location "https://docs.new"'])
    time.sleep(5)
    
    # Get the URL of the created doc
    script_get_url = '''
    tell application "Google Chrome"
      set w to first window whose id is 1304793385
      tell active tab of w
        return URL of active tab of w
      end tell
    end tell
    '''
    res = subprocess.run(["osascript", "-e", script_get_url], capture_output=True, text=True)
    doc_url = res.stdout.strip()
    print(f"  URL: {doc_url}")
    created_links.append({"title": title, "url": doc_url})

print("=== ALL GOOGLE DOCS CREATED ===")
print(json.dumps(created_links, indent=2))
