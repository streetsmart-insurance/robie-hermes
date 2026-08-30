#!/usr/bin/env python3
"""Populates the 3 Google Docs with their respective content."""

import subprocess
import time
from pathlib import Path

ROBIE_DIR = Path("/Users/carloferrara/Documents/Hermes Projects/robie-hermes")

docs_map = [
    {
        "url": "https://docs.google.com/document/d/1NBIkFB6aD7h5o2UjSHPFdPJkoeYKuyyKbVnUx3D2Y4Q/edit?tab=t.0",
        "file": ROBIE_DIR / "docs" / "STREETSMART_PRODUCTIVITY_AND_ACCOUNTABILITY_SYSTEM.md"
    },
    {
        "url": "https://docs.google.com/document/d/1ZrzbZqGDflz8ACwc37fVLPiPma-Xfn1kCDStoEydvFY/edit?tab=t.0",
        "file": ROBIE_DIR / "docs" / "STREETSMART_ROLE_BASED_ACCOUNTABILITY_SUPER_PROMPT.md"
    },
    {
        "url": "https://docs.google.com/document/d/1rd6C37Ay78b3z5w2yyRt8iOZ6u5HTCroCuBsbKXdQl8/edit?tab=t.0",
        "file": ROBIE_DIR / "docs" / "STREETSMART_WEEKLY_PERFORMANCE_SCORECARD.md"
    }
]

for doc in docs_map:
    url = doc["url"]
    content = doc["file"].read_text(encoding="utf-8")
    
    # Copy content to clipboard
    p_copy = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p_copy.communicate(content.encode("utf-8"))
    
    print(f"Pasting into: {url}...")
    
    # Open tab and paste
    script = f'''
    tell application "Google Chrome"
      set w to first window whose id is 1304793385
      repeat with i from 1 to count of tabs of w
        set t to item i of tabs of w
        if URL of t contains "{url.split('/edit')[0]}" then
          set active tab index of w to i
          delay 2
          tell application "System Events"
            keystroke "v" using command down
          end tell
          return "Pasted successfully"
        end if
      end repeat
    end tell
    '''
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    print("  ", res.stdout.strip())
    time.sleep(2)

print("=== ALL GOOGLE DOCS POPULATED ===")
