"""Weekly Policy Change Request Tracker automation.

Pipeline: Looker report 4659 (Policy Change Request Summary, 1-year filter,
all rows) -> fold into the "New Change Request Tracker 2" Google Sheet
weekly tab -> group by fixed producer teams -> sort team, then days-open
desc -> Policy Number forced to plain text -> light-red conditional format
for Days Open >30.

Every edge-case assumption is recorded in ASSUMPTIONS.md mapped to the
Sandeep question numbers (P1-P12, X1-X6). Sandeep's answers are pending;
the CONFIG object in config.py is where they plug in.
"""
