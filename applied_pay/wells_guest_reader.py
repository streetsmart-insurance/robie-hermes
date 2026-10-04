"""Test-only captured-page Wells reader. No login, network, payments or writes.

A trusted operator supplies captured pages. Posted is never converted to cleared.
Row numbers/hashes are evidence locations, never invented bank transaction IDs.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib, json, re
from urllib.parse import urlsplit

ALLOWED_ACCOUNTS = {'3021': 'trust', '3018': 'operating'}
HOSTS = {'www.wellsfargo.com', 'connect.secure.wellsfargo.com'}
class CaptureError(ValueError): pass

def amount(text):
    value = Decimal(str(text).replace('$', '').replace(',', '').strip())
    if not value.is_finite() or value < 0 or value != value.quantize(Decimal('.01')):
        raise CaptureError('invalid unsigned amount')
    return str(value.quantize(Decimal('.01')))

def read_capture(capture, *, artifact_bytes, now, environment='TEST'):
    if environment != 'TEST': raise CaptureError('Test only')
    if now.tzinfo is None: raise CaptureError('timezone required')
    url = urlsplit(capture['source_url'])
    if url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password:
        raise CaptureError('unsupported bank source')
    account = capture.get('account_last4')
    if account not in ALLOWED_ACCOUNTS: raise CaptureError('account out of scope')
    if capture.get('access_mode') != 'view_only' or not capture.get('identity_review_reference'):
        raise CaptureError('verified guest identity required')
    try: captured = datetime.fromisoformat(capture['captured_at'])
    except (ValueError, KeyError): raise CaptureError('capture timestamp required')
    if captured.tzinfo is None or not timedelta(0) <= now-captured <= timedelta(hours=24):
        raise CaptureError('stale or invalid capture')
    if hashlib.sha256(artifact_bytes).hexdigest() != capture.get('artifact_sha256'):
        raise CaptureError('capture integrity mismatch')
    # Compare every supplied page against the original captured artifact, not metadata alone.
    original = json.loads(artifact_bytes)
    if original.get('pages') != capture.get('pages') or original.get('account_last4') != account:
        raise CaptureError('page/source binding mismatch')
    pages = capture.get('pages') or []
    if not pages: raise CaptureError('no pages')
    rows, issues, seen = [], [], set()
    for index, page in enumerate(pages):
        if page.get('page_number') != index+1: raise CaptureError('page order/gap')
        if page.get('account_last4') != account: raise CaptureError('mixed accounts')
        if page.get('error'): issues.append({'page':index+1,'reason':'page error'})
        if index<len(pages)-1 and page.get('has_next') is not True:
            raise CaptureError('premature end marker')
        section = None
        for position, cells in enumerate(page.get('rows', [])):
            if not isinstance(cells, list): raise CaptureError('invalid row')
            joined = ' '.join(str(x) for x in cells)
            if len(cells)==1:
                if 'Pending Transactions' in joined: section='pending'
                elif 'Authorized Transactions' in joined: section='authorized'
                elif 'Posted Transactions' in joined: section='posted'
                continue
            if len(cells)!=5 or not re.fullmatch(r'\d{2}/\d{2}/\d{2}', str(cells[1])):
                if any('$' in str(x) for x in cells): issues.append({'page':index+1,'row':position,'reason':'unrecognized transaction row'})
                continue
            if section is None: raise CaptureError('transaction section missing')
            credit,debit = str(cells[3]).strip(),str(cells[4]).strip()
            if bool(credit)==bool(debit): raise CaptureError('ambiguous debit/credit')
            try: bank_date=datetime.strptime(cells[1],'%m/%d/%y').date().isoformat()
            except ValueError: raise CaptureError('invalid bank date')
            descriptor=str(cells[2]); refs=re.findall(r'TRN\*1\*([A-Z0-9]+)',descriptor)
            row={'account_last4':account,'account_role':ALLOWED_ACCOUNTS[account],
                 'bank_date':bank_date,'amount':amount(credit or debit),
                 'direction':'credit' if credit else 'debit','bank_status':section,
                 'descriptor':descriptor,'bank_reference_candidates':sorted(set(refs)),
                 'bank_transaction_id':None,'clearing_verified':False,
                 'source_location':{'page':index+1,'row':position},
                 'return_or_reversal_candidate':bool(re.search(r'\b(return|reversal|returned)\b',descriptor,re.I)),
                 'review_reasons':['stable bank transaction ID not captured','bank clearing semantics unverified']}
            # Exact overlapping rows may be distinct transactions. Retain, flag, never silently dedupe.
            key=(bank_date,descriptor,row['amount'],row['direction'],section)
            if key in seen: row['review_reasons'].append('duplicate-looking row; identity unresolved')
            seen.add(key);rows.append(row)
    complete=pages[-1].get('has_next') is False and not issues
    if not complete: issues.append({'reason':'capture incomplete; no completeness claim'})
    return {'mode':'captured_page_shadow_only','account_last4':account,'rows':rows,'issues':issues,
            'complete':complete,'cleared_bank_deposits':[], 'operating_observations':rows if account=='3018' else [],
            'bank_actions':0,'qbo_posts':0,'ezlynx_writes':0}
