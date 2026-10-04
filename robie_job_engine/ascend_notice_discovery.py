"""Read-only mail discovery candidate. No routing, accounts or write adapters.

All likely notices enter a PRIVATE local review queue, never auto-authorized.
A sender domain, quoted header or URL is a lead, not authenticated authorship.
The injected Gmail service must have readonly scope. No service is constructed.
"""
from __future__ import annotations
import base64, hashlib, json, re, sqlite3, os
from email.utils import parseaddr
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from datetime import date

class ScanIncomplete(RuntimeError): pass
class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag=='a':
            href=dict(attrs).get('href','');self.parts.append(' '+href+' ')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
        if tag in ('br','p','div','tr'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def decode_text(payload, budget=1_000_000):
    texts=[];used=0
    def walk(p):
        nonlocal used
        # Do not download arbitrary attachments or remote links.
        if p.get('filename'): return
        mime=p.get('mimeType','');data=(p.get('body') or {}).get('data')
        if data and mime in ('text/plain','text/html'):
            if len(data)>budget*2:raise ScanIncomplete('body_exceeds_budget')
            try:text=base64.urlsafe_b64decode(data+'='*(-len(data)%4)).decode('utf-8',errors='replace')
            except Exception as e:raise ScanIncomplete('body_decode_failed') from e
            used+=len(text)
            if used>budget:raise ScanIncomplete('body_exceeds_budget')
            if mime=='text/html':
                parser=Text();parser.feed(text);text=''.join(parser.parts)
            texts.append(unescape(text))
        for child in p.get('parts') or []:walk(child)
    walk(payload);return '\n'.join(texts)

FAMILIES=(('past_due',r'past[ -]?due payment'),('payment_failed',r'payment failed|failed payment'),
          ('cancellation',r'notice of cancel|cancel(?:ed|led|lation).*?(?:non.payment|policy|loan)|loan has been cancel'),
          ('return_premium',r'return premium|refund to your customer'),
          ('new_program',r'new program|program created|finance agreement'))

def discovery(message):
    payload=message.get('payload') or {}
    headers={h.get('name','').lower():h.get('value','') for h in payload.get('headers') or []}
    subject=headers.get('subject','');body=decode_text(payload)
    sender=parseaddr(headers.get('from',''))[1].lower();domain=sender.rsplit('@',1)[-1]
    direct=domain in ('useascend.com','ascend.com') # claim only, never identity verification
    text=subject+'\n'+body
    domains=[]
    for url in re.findall(r'https?://[^\s<>"\']+',text):
        host=(urlparse(url).hostname or '').lower()
        if host=='useascend.com' or host.endswith('.useascend.com'):domains.append(host)
    forwarded=bool(re.search(r'(?im)^\s*(?:from|sender):[^\n]*@(?:[A-Za-z0-9.-]+\.)?useascend\.com\b',body))
    families=[kind for kind,pattern in FAMILIES if re.search(pattern,text,re.I)]
    branded=bool(re.search(r'\bAscend\b',text,re.I))
    signal=direct or bool(domains) or forwarded or branded
    # Business terms alone can come from any insurer, staff email or spam.
    if not signal:return {'classification':'unrelated','reason':'no_ascend_source_signal'}
    kind='ambiguous' if len(families)>1 else families[0] if families else 'unknown'
    return {'classification':'review','notice_family':kind,
            'reason':'multiple_notice_families' if len(families)>1 else 'unrecognized_ascend_notice' if not families else 'source_and_mapping_review_required',
            'subject':subject,'observed_from':headers.get('from',''),
            'sender_claim':'direct_domain' if direct else 'forward_or_link' if domains or forwarded else 'unverified_brand_text',
            'source_verified':False,'may_file':False,'may_send_task':False,
            'internal_date':message.get('internalDate',''),
            'read_state':'unread' if 'UNREAD' in message.get('labelIds',[]) else 'read',
            'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'body_excerpt':body[:6000], 'body_excerpt_truncated':len(body)>6000,
            'attachment_text_unread':any(p.get('filename') for p in payload.get('parts') or [])}

class ReviewQueue:
    """Separate local SQLite queue. No legacy ledger and no external publication."""
    def __init__(self,path):
        if str(path)!=':memory:':
            fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);os.chmod(path,0o600)
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS notice_review(mailbox TEXT,message_id TEXT,payload TEXT,status TEXT DEFAULT "review",PRIMARY KEY(mailbox,message_id))');self.db.commit()
    def put(self,mailbox,message_id,item):
        self.db.execute('INSERT INTO notice_review(mailbox,message_id,payload) VALUES (?,?,?) ON CONFLICT(mailbox,message_id) DO UPDATE SET payload=excluded.payload',(mailbox,message_id,json.dumps(item)));self.db.commit()
    def rows(self):return [dict(mailbox=m,message_id=i,item=json.loads(p),status=s) for m,i,p,s in self.db.execute('SELECT mailbox,message_id,payload,status FROM notice_review ORDER BY mailbox,message_id')]
    def close(self):self.db.close()

def search_query(start_date,end_date):
    # Explicit bounded backfill window includes read + unread. No implicit 2d cap.
    start=date.fromisoformat(start_date);end=date.fromisoformat(end_date)
    if end<=start:raise ValueError('end_must_follow_start')
    # Query is a discovery lead, never a verified sender filter.
    return f'after:{start.isoformat()} before:{end.isoformat()} (from:useascend.com OR "useascend.com" OR "Ascend")'

def scan(service,mailbox,queue,*,start_date,end_date,page_size=100,max_pages=100):
    if not mailbox or '@' not in mailbox:raise ValueError('explicit_mailbox_required')
    query=search_query(start_date,end_date);token=None;seen_tokens=set();seen_ids=set()
    counts={'mode':'discovery_only','mailbox':mailbox,'query':query,'pages':0,'fetched':0,'review':0,'unrelated':0,'errors':[], 'complete':False,'destination_writes':0,'gmail_label_changes':0}
    for _ in range(max_pages):
        args=dict(userId='me',q=query,maxResults=page_size)
        if token:args['pageToken']=token
        try:page=service.users().messages().list(**args).execute()
        except Exception as e:counts['errors'].append('list:'+type(e).__name__);return counts
        counts['pages']+=1
        for row in page.get('messages') or []:
            mid=row.get('id')
            if not mid or mid in seen_ids:continue
            seen_ids.add(mid)
            try:
                message=service.users().messages().get(userId='me',id=mid,format='full').execute()
                if message.get('id')!=mid:raise ScanIncomplete('message_id_mismatch')
                item=discovery(message);counts['fetched']+=1
                if item['classification']=='review':queue.put(mailbox,mid,item);counts['review']+=1
                else:counts['unrelated']+=1
            except Exception as e:
                counts['errors'].append(mid+':'+type(e).__name__)
                queue.put(mailbox,mid,{'classification':'review','reason':'fetch_or_decode_failed','error_type':type(e).__name__,'source_verified':False,'may_file':False,'may_send_task':False})
        token=page.get('nextPageToken')
        if not token:
            counts['complete']=not counts['errors'];return counts
        if token in seen_tokens:counts['errors'].append('repeated_page_token');return counts
        seen_tokens.add(token)
    counts['errors'].append('page_limit_exceeded');return counts

def main(argv=None,service_factory=None):
    """Explicit Test-only readonly preview; never hooks the live notice driver."""
    import argparse,os
    parser=argparse.ArgumentParser(description='Test-only Ascend mail discovery, no destination actions')
    parser.add_argument('--mailbox',required=True)
    parser.add_argument('--start-date',required=True)
    parser.add_argument('--end-date',required=True)
    parser.add_argument('--queue-db',required=True)
    parser.add_argument('--delegation-service-account',required=True)
    args=parser.parse_args(argv)
    if os.environ.get('ROBIE_ENV','').upper()!='TEST':
        parser.exit(2,'TEST_ONLY: refusing outside ROBIE_ENV=TEST\n')
    if service_factory is None:
        from .gmail_accountability import build_notice_gmail_service
        service_factory=build_notice_gmail_service
    # This existing factory selects readonly scopes when modify=False.
    service=service_factory(args.delegation_service_account,args.mailbox,modify=False)
    queue=ReviewQueue(args.queue_db)
    try:result=scan(service,args.mailbox,queue,start_date=args.start_date,end_date=args.end_date)
    finally:queue.close()
    print(json.dumps(result,indent=2))
    return 0 if result['complete'] else 1

if __name__=='__main__':raise SystemExit(main())
