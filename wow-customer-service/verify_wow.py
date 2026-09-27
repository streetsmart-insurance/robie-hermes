#!/usr/bin/env python3
"""
WOW Customer Service - September Verification Script

Note-based SCREENING only. This script flags rows for evidence review against
the authoritative destination system (carrier portal, EZLynx reports, Magellan).
It does NOT issue final rejections — per agency rules, labels and notes are
claims, not proof; evidence must come from the authoritative system.

Verdicts:
  FLAG   - note contradicts the label; flag for review, not a final rejection
  VERIFY - note claims the label; needs authoritative proof
  WEAK   - note doesn't mention the label's key terms; needs evidence or clarification

Usage: python3 verify_wow.py <september_raw.csv>
"""
import csv
import re
import sys
from collections import defaultdict

# Patterns that contradict the label in the note -> FLAG for review
AUTOPAY_FLAG_PATTERNS = [
    r'paid in full',
    r'mortgage billed',
]

NCSR_FLAG_PATTERNS = [
    r'sent.*app.*for signature',
    r'sending.*app',
    r'policy shell',
    r'waiting on.*download',
    r'will fu on.*download',
    r'putting the app together',
    r'wait for esign',
]

CROSSSELL_FLAG_PATTERNS = [
    r'\bBOR\b.*renewal',
    r'sent.*app.*for signature',
    r'policy shell',
    r'sent bind request',
]

def check_autopay(note):
    """Returns: 'FLAG', 'VERIFY', or 'WEAK' (screening verdicts, not final)"""
    note_lower = note.lower()
    for pattern in AUTOPAY_FLAG_PATTERNS:
        if re.search(pattern, note_lower):
            return 'FLAG'
    # Check if note mentions autopay/EFT
    if re.search(r'autopay|auto.pay|eft|electronic funds', note_lower):
        return 'VERIFY'  # Claims autopay, needs portal check
    return 'WEAK'  # Labeled autopay but note doesn't mention it

def check_ncsr(note):
    """Returns: 'FLAG', 'VERIFY', or 'WEAK' (screening verdicts, not final)"""
    note_lower = note.lower()
    for pattern in NCSR_FLAG_PATTERNS:
        if re.search(pattern, note_lower):
            return 'FLAG'
    # Check for policy number (stronger)
    if re.search(r'\b\d{6,}\b|[A-Z]{2,}\d+', note):
        return 'VERIFY'
    if re.search(r'\bsold\b|\bissued\b|\bbound\b', note_lower):
        return 'VERIFY'
    return 'WEAK'

def check_crosssell(note):
    """Returns: 'FLAG', 'VERIFY', or 'WEAK' (screening verdicts, not final)"""
    note_lower = note.lower()
    for pattern in CROSSSELL_FLAG_PATTERNS:
        if re.search(pattern, note_lower):
            return 'FLAG'
    if re.search(r'\bissued\b', note_lower):
        return 'VERIFY'
    if re.search(r'\bsold\b|\bbound\b', note_lower):
        return 'VERIFY'
    return 'WEAK'

def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'september_raw.csv'
    results = defaultdict(lambda: defaultdict(int))
    details = []

    with open(input_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            labels = row.get('Activity Labels', '')
            note = row.get('Note', '') or ''
            emp = row.get('Note Created by', '')
            account = row.get('Account Name', '')

            if 'AutoPay Setup' in labels:
                verdict = check_autopay(note)
                results['AutoPay Setup'][verdict] += 1
                if verdict in ('FLAG', 'WEAK'):
                    details.append(('AutoPay Setup', verdict, emp, account, note[:60]))

            if 'New Customer CSR' in labels:
                verdict = check_ncsr(note)
                results['New Customer CSR'][verdict] += 1
                if verdict == 'FLAG':
                    details.append(('New Customer CSR', verdict, emp, account, note[:60]))

            if 'Cross Sell' in labels:
                verdict = check_crosssell(note)
                results['Cross Sell'][verdict] += 1
                if verdict in ('FLAG', 'WEAK'):
                    details.append(('Cross Sell', verdict, emp, account, note[:60]))

    print("=== WOW Verification Screening Results ===\n")
    print("(Screening verdicts only — final status requires authoritative proof.)\n")
    for label in results:
        print(f"{label}:")
        for verdict in ['FLAG', 'WEAK', 'VERIFY']:
            count = results[label][verdict]
            if count:
                print(f"  {verdict}: {count}")
        print()

    print("\n=== Items Flagged for Evidence Review ===\n")
    for label, verdict, emp, account, note in details:
        print(f"[{verdict}] {label} | {emp} | {account}")
        print(f"  Note: {note}...")
        print()

if __name__ == '__main__':
    main()
