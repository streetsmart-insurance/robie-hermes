#!/usr/bin/env python3
import json
import subprocess
import time

html_path = "/Users/carloferrara/Documents/Hermes Projects/robie-hermes/docs/jake_holistic_accountability_directive.html"
with open(html_path, "r", encoding="utf-8") as f:
    html_content = f.read()

# Open Gmail compose tab in Chrome
script_open = '''
tell application "Google Chrome"
  set w to first window whose id is 1304793385
  make new tab at end of tabs of w with properties {URL:"https://mail.google.com/mail/u/0/?fs=1&to=jake@streetsmart.insurance&cc=carlo@streetsmart.insurance&su=StreetSmart+Holistic+Accountability+Architecture+%26+Role-Based+Super-Prompts&tf=cm"}
end tell
'''
p = subprocess.run(["osascript", "-e", script_open], capture_output=True, text=True)
print("Opened compose tab:", p.stdout)

time.sleep(3)

# Populate rich HTML body
escaped_html = json.dumps(html_content)
script_pop = f'''
tell application "Google Chrome"
  set w to first window whose id is 1304793385
  tell active tab of w
    return execute javascript "(() => {{
      const body = document.querySelector(\\"div[role=\\'textbox\\'][aria-label*=\\'Message Body\\']\\");
      if (body) {{
        body.innerHTML = {escaped_html};
        return \\"Populated rich HTML body in Gmail\\";
      }}
      return \\"Body element not found\\";
    }})()"
  end tell
end tell
'''
p_pop = subprocess.run(["osascript", "-e", script_pop], capture_output=True, text=True)
print(p_pop.stdout)

time.sleep(1)

# Click Send button
script_send = '''
tell application "Google Chrome"
  set w to first window whose id is 1304793385
  tell active tab of w
    return execute javascript "(() => {
      const sendBtn = document.querySelector(\\"div.T-I.J-J5-Ji.aoO.v7.T-I-atl.L3, [data-tooltip*=\\'Send\\'], [aria-label*=\\'Send\\']\\");
      if (sendBtn) {
        sendBtn.click();
        return \\"Clicked Send button\\";
      }
      return \\"Send button not found\\";
    })()"
  end tell
end tell
'''
p_send = subprocess.run(["osascript", "-e", script_send], capture_output=True, text=True)
print(p_send.stdout)
