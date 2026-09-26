"""Synthetic live collector contracts. No network, credentials or real client data."""
from robie_job_engine.daily_accounting_live import collect_ascend, collect_applied_portal, collect_ezlynx_tasks, briefing_payload


class FakeAscend:
    origin = 'https://sandbox.api.useascend.com'
    def __init__(self, responses): self.responses = responses; self.calls=[]
    def get(self, path, query=None):
        self.calls.append((path, query))
        return self.responses[(path, query['page'])]


def pages(count=2):
    paths=('cancelation_returns','invoices','programs','loans','payouts')
    out={}
    for p in paths:
        for n in range(1,count+1):
            out[(f'/v1/{p}',n)]={'data':[{'id':f'SYN-{p}-{n}','updated_at':'2026-09-26T10:00:00Z','status':'open'}],
                                   'meta':{'next':f'/v1/{p}?page={n+1}' if n<count else None,'count':1}}
    return out


def test_ascend_follows_every_page_and_exhausts_all_collections():
    client=FakeAscend(pages())
    result=collect_ascend(client)
    assert result.complete and len(result.items)==10
    assert len(client.calls)==10
    assert result.provenance.startswith('Ascend')


def test_ascend_rejects_loop_foreign_next_missing_meta_and_duplicate_ids():
    cases=[]
    base=pages()
    for changed in ({'next':'/v1/invoices?page=1','count':1},
                    {'next':'https://evil.invalid/steal','count':1},
                    {'count':1},
                    {'next':None,'count':999}):
        p=dict(base); p[('/v1/invoices',1)]={'data':p[('/v1/invoices',1)]['data'],'meta':changed};cases.append(p)
    for p in cases: assert not collect_ascend(FakeAscend(p)).complete
    p=pages();p[('/v1/invoices',2)]['data']=p[('/v1/invoices',1)]['data']
    assert not collect_ascend(FakeAscend(p)).complete


def test_applied_portal_requires_exhausted_pages_and_row_count():
    rows=[{'ref':'SYN-B1','lines':[{'type':'ach_chargeback','psp_ref':'SYN-R1','txn_date':'2026-09-26','amount':'-20.00'}]}]
    assert collect_applied_portal([{'batches':rows,'row_count':1,'next':None,'as_of':'2026-09-26T10:00:00Z'}],
                                  scope='all batches 2026-09-26').complete
    assert not collect_applied_portal([{'batches':rows,'row_count':2,'next':None,'as_of':'2026-09-26T10:00:00Z'}],
                                      scope='all batches 2026-09-26').complete
    assert not collect_applied_portal([{'batches':rows,'row_count':1,'next':'page2','as_of':'2026-09-26T10:00:00Z'}],
                                      scope='all batches 2026-09-26').complete


def test_ezlynx_unverified_ui_no_ids_is_incomplete():
    result=collect_ezlynx_tasks([{'title':'SYN-Task','due_date':'2026-09-27','assigned_team':'Accounting Team'}],
      as_of='2026-09-26T10:00:00Z', scope='all Accounting Team open tasks', exhausted=True)
    assert not result.complete


def test_briefing_payload_carries_incomplete_coverage_and_no_false_clearing():
    report=briefing_payload({}, as_of='2026-09-26T10:00:00Z')
    assert not report['briefing_ready'] and len(report['coverage'])==3
    assert 'bank clearing not proven' in report['settlement_caveat'].lower()


def test_ascend_auth_failure_is_incomplete_without_exception_detail():
    class Unauthorized(FakeAscend):
        def get(self, path, query=None):
            raise RuntimeError('secret from upstream error')
    result=collect_ascend(Unauthorized({}))
    assert not result.complete and result.items == []
    assert 'secret' not in result.error


def test_ascend_page_limit_and_count_false_are_incomplete():
    p=pages();p[('/v1/invoices',1)]['meta']['count']=False
    assert not collect_ascend(FakeAscend(p)).complete
    assert not collect_ascend(FakeAscend(pages()), max_pages=1).complete


def test_briefing_stays_candidate_even_with_all_sources_complete():
    from robie_job_engine.daily_accounting_checks import SourceSnapshot
    snapshots={s:SourceSnapshot(s,[],True) for s in ('Ascend','Applied Pay','EZLynx Accounting Team')}
    report=briefing_payload(snapshots,as_of='2026-09-26T10:00:00Z')
    assert 'candidate' in report['status']
