"""Synthetic API/systemd-free tests; no cloud, clients, or host mutation."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ctl = SimpleNamespace(**runpy.run_path(str(ROOT / 'scripts/test_cloud_deploy.py')))
claim = runpy.run_path(str(ROOT / 'scripts/test_cloud_deploy_claim.py'))
remote = {}; exec(claim['REMOTE'], remote)
NOW = dt.datetime(2026, 10, 3, 16, tzinfo=dt.timezone.utc)
COMMIT, PRIOR, CONTROLLER, TREE = 'a'*40, 'b'*40, 'c'*40, 'd'*40
BODY = f'/robie-test deploy commit={COMMIT} sha256={"e"*64} prior={PRIOR} prior_sha256={"f"*64} nonce={"0"*32} expires=2026-10-03T16:20:00Z'


def fixture():
    config = dict(enabled='TEST_DEPLOY_V1', pr=800, pr_id=12345, controller=CONTROLLER, tree=TREE)
    comment = dict(id=123, user=dict(id=ctl.ACTOR, type='User', login='carlo504'),
                   issue_url=f'https://api.github.com/repos/{ctl.REPO}/issues/800',
                   created_at='2026-10-03T15:59:59Z', updated_at='2026-10-03T15:59:59Z', body=BODY)
    pr = dict(number=800, id=12345, merged=True, state='closed', merge_commit_sha=CONTROLLER,
              base=dict(ref='main', repo=dict(id=ctl.REPO_ID, full_name=ctl.REPO)))
    permission = dict(user=dict(id=ctl.ACTOR), permission='write')
    return config, comment, pr, permission


def request():
    c, f, p, a = fixture()
    return ctl.validate_comment(f, p, a, c, NOW)


def test_actual_human_identity_and_exact_release_are_preserved():
    r = request()
    assert r['actor_id'] == ctl.ACTOR and r['actor_login'] == 'carlo504'
    assert r['commit'] == COMMIT and r['prior'] == PRIOR and r['comment_id'] == 123
    assert r['commit'] != '42e872f4c86fc4b4e37f859fc390f0b7c832f373'


@pytest.mark.parametrize('change', [
    lambda c,f,p,a: c.update(enabled=''),
    lambda c,f,p,a: f['user'].update(id=42),
    lambda c,f,p,a: f['user'].update(type='Bot'),
    lambda c,f,p,a: f.update(updated_at='2026-10-03T16:00:00Z'),
    lambda c,f,p,a: f.update(issue_url=f'https://api.github.com/repos/{ctl.REPO}/issues/801'),
    lambda c,f,p,a: p.update(merged=False),
    lambda c,f,p,a: p.update(state='open'),
    lambda c,f,p,a: p.update(id=888),
    lambda c,f,p,a: p.update(number=801),
    lambda c,f,p,a: p.update(merge_commit_sha='1'*40),
    lambda c,f,p,a: p['base'].update(ref='feature'),
    lambda c,f,p,a: p['base']['repo'].update(id=42),
    lambda c,f,p,a: a.update(permission='read'),
    lambda c,f,p,a: a['user'].update(id=42),
])
def test_untrusted_edited_unapproved_context_refuses(change):
    c,f,p,a=fixture();change(c,f,p,a)
    with pytest.raises(ValueError): ctl.validate_comment(f,p,a,c,NOW)


@pytest.mark.parametrize('body', [
    BODY+'\n', BODY+' extra=x', BODY.replace(' deploy ', ' install '),
    BODY.replace('/robie-test', '/robie-production'), BODY.replace(' commit=', '  commit='),
    BODY.replace(COMMIT, PRIOR), BODY.replace('16:20:00Z','15:59:00Z'),
    BODY.replace('16:20:00Z','16:30:00Z'), BODY.replace('0'*32,'../escape'),
    BODY.replace('e'*64, '$(id)'), BODY.replace(COMMIT, 'main'),
])
def test_strict_commands_no_shell_or_scope_expansion(body):
    c,f,p,a=fixture();f['body']=body
    with pytest.raises(ValueError): ctl.validate_comment(f,p,a,c,NOW)


def inputs():
    return dict(cloud_comment_id='123',operation='deploy',confirmation='DEPLOY_TO_HERMES_TEST_01')


@pytest.mark.parametrize('change', [dict(operation='certify'),dict(operation='install-stopped'),
    dict(confirmation='DEPLOY_TO_HERMES_POC_01'),dict(cloud_comment_id='124'),dict(qa_evidence='{}'),
    dict(test_run_id='123'),dict(test_artifact_id='42'),dict(release_sha256='a'*64)])
def test_receiving_workflow_does_not_trust_dispatch_inputs(change):
    i=inputs();i.update(change)
    with pytest.raises(ValueError): ctl.validate_receiver(i,request(),COMMIT)


def test_moving_main_dispatch_race_refuses():
    ctl.validate_receiver(inputs(),request(),COMMIT)
    with pytest.raises(ValueError): ctl.validate_receiver(inputs(),request(),'9'*40)


def test_dispatch_event_is_bound_to_original_comment():
    c,f,p,a=fixture()
    event=dict(action='created',repository=dict(id=ctl.REPO_ID,full_name=ctl.REPO),
        issue=dict(number=800,pull_request=dict(url=f'https://api.github.com/repos/{ctl.REPO}/pulls/800')),
        sender=dict(id=ctl.ACTOR),comment=copy.deepcopy(f))
    ctl.validate_dispatch_event(event,f,c)
    for part,key,value in [('comment','body',BODY+' '),('sender','id',42),('issue','number',801)]:
        bad=copy.deepcopy(event);bad[part][key]=value
        with pytest.raises(ValueError): ctl.validate_dispatch_event(bad,f,c)


def fake_api():
    entries=[dict(path=p,mode='100644',type='blob',sha=hashlib.sha1(p.encode()).hexdigest()) for p in ctl.PATHS]
    trees={CONTROLLER:dict(sha=TREE,truncated=False,tree=copy.deepcopy(entries)),
           COMMIT:dict(sha='1'*40,truncated=False,tree=copy.deepcopy(entries))}
    def get(path):
        if path=='/git/ref/heads/main':return dict(ref='refs/heads/main',object=dict(type='commit',sha=COMMIT))
        if path.startswith('/git/commits/'):
            sha=path.rsplit('/',1)[-1];return dict(sha=sha,tree=dict(sha=trees[sha]['sha']))
        tree=path.rsplit('/',1)[-1].split('?')[0]
        return next(v for v in trees.values() if v['sha']==tree)
    return trees,get


def test_unrelated_main_advance_allowed_but_security_file_drift_refuses():
    trees,get=fake_api();c=fixture()[0]
    assert ctl.controller_guard(c,COMMIT,CONTROLLER,get)['observed_main']==COMMIT
    trees[COMMIT]['tree'][0]['sha']='2'*40
    with pytest.raises(ValueError):ctl.controller_guard(c,COMMIT,CONTROLLER,get)


@pytest.mark.parametrize('mutation', ['missing','duplicate','mode','truncated','tree'])
def test_manifest_fail_closed(mutation):
    trees,get=fake_api();tree=trees[COMMIT]
    if mutation=='missing':tree['tree'].pop()
    elif mutation=='duplicate':tree['tree'].append(tree['tree'][0])
    elif mutation=='mode':tree['tree'][0]['mode']='120000'
    elif mutation=='truncated':tree['truncated']=True
    else:trees[CONTROLLER]['sha']='9'*40
    with pytest.raises(ValueError):ctl.controller_guard(fixture()[0],COMMIT,CONTROLLER,get)


def test_replay_claim_persists_after_failure_and_rejects_reuse(tmp_path):
    r=request();r.update(receiver_run_id=77,controller_commit=CONTROLLER)
    remote['consume'](r,tmp_path)
    for candidate in (r,dict(r,receiver_run_id=78),dict(r,nonce='1'*32),dict(r,comment_id=124)):
        with pytest.raises(FileExistsError):remote['consume'](candidate,tmp_path)
    assert json.loads((tmp_path/'comment_id-123').read_text())==r
    assert (tmp_path/('nonce-'+'0'*32)).stat().st_mode & 0o777 == 0o600


def test_partial_nonce_collision_never_erases_first_claim(tmp_path):
    r=request();remote['consume'](r,tmp_path)
    with pytest.raises(FileExistsError):remote['consume'](dict(r,comment_id=124),tmp_path)
    assert (tmp_path/'comment_id-124').exists()
    with pytest.raises(FileExistsError):remote['consume'](dict(r,comment_id=124,nonce='1'*32),tmp_path)


def test_claim_paths_cannot_follow_symlinks(tmp_path):
    outside=tmp_path/'outside';outside.write_text('unchanged')
    (tmp_path/'comment_id-123').symlink_to(outside)
    with pytest.raises(FileExistsError):remote['consume'](request(),tmp_path)
    assert outside.read_text()=='unchanged'


def test_renderer_serializes_data_never_interpolates_python():
    r=request();r['actor_login']="');raise Exception('injected')#"
    program=claim['render'](r,'claim');compile(program,'rendered','exec')
    assert r['actor_login'] not in program
    with pytest.raises(ValueError):claim['render'](r,'install')


def test_workflows_disabled_and_cloud_auth_after_independent_validation():
    dispatcher=yaml.safe_load((ROOT/ctl.DISPATCH).read_text())
    job=dispatcher['jobs']['dispatch']
    assert "vars.ROBIE_TEST_CLOUD_DEPLOY_ENABLED == 'TEST_DEPLOY_V1'" in job['if']
    assert job['permissions']=={'contents':'read','issues':'read','pull-requests':'read','actions':'write'}
    assert 'id-token' not in json.dumps(dispatcher)
    workflow=yaml.safe_load((ROOT/ctl.RECEIVE).read_text())
    steps=workflow['jobs']['deploy-test']['steps']
    names=[s.get('name',s.get('uses')) for s in steps]
    auth=next(i for i,s in enumerate(steps) if 'google-github-actions/auth' in s.get('uses',''))
    assert names.index('Independently authorize original cloud comment') < auth
    assert names.index('Revalidate cloud request and exact built bytes before cloud authentication') < auth
    assert names.index('Consume durable Test request before staging') < names.index('Create bounded Test staging directory')
    assert names.index('Recheck request, replay ownership and rollback target before installation') < names.index('Install and independently verify Test')
    assert '!inputs.cloud_comment_id' in workflow['jobs']['certify-test']['if']
    assert workflow['concurrency']=={'group':'robie-hermes-test-deploy','cancel-in-progress':False}
    assert 'workflow_dispatch' in str(workflow.get(True,workflow.get('on')))


def test_no_live_inspection_or_production_binding_migration():
    text=(ROOT/ctl.DISPATCH).read_text()+(ROOT/'scripts/test_cloud_deploy.py').read_text()
    assert 'ROBIE_TEST_OPERATOR_SETUP_COMMIT' not in text
    assert 'ROBIE_TEST_STOPPED_OPERATOR_ENABLED' not in text
    assert "api('/actions/workflows/deploy-test.yml/dispatches'" in text
    assert 'deploy-production.yml/dispatches' not in text


def test_rollback_pointer_and_digest_are_actual_preconditions(tmp_path):
    r=request();target=tmp_path/'releases'/PRIOR[:12]/('robie-hermes-'+PRIOR[:12]);target.mkdir(parents=True)
    (target/'.release-sha256').write_text(r['prior_sha256']+'\n')
    (tmp_path/'current').symlink_to(target);(tmp_path/'releases/current').symlink_to(target)
    remote['verify_prior'](r,tmp_path)
    (target/'.release-sha256').write_text('9'*64)
    with pytest.raises(ValueError):remote['verify_prior'](r,tmp_path)
    (target/'.release-sha256').write_text(r['prior_sha256'])
    (tmp_path/'current').unlink();(tmp_path/'current').symlink_to(tmp_path)
    with pytest.raises(ValueError):remote['verify_prior'](r,tmp_path)


def test_receipt_ownership_cannot_transfer_to_another_receiver(tmp_path, monkeypatch):
    r=request();r.update(receiver_run_id=77,controller_commit=CONTROLLER)
    remote['consume'](r,tmp_path)
    # Permission checks separately covered; test immutable content equality as non-root.
    monkeypatch.setitem(remote,'safe',lambda *args,**kwargs: None)
    remote['check'](r,tmp_path)
    with pytest.raises(ValueError):remote['check'](dict(r,receiver_run_id=78),tmp_path)
    with pytest.raises(ValueError):remote['check'](dict(r,sha256='9'*64),tmp_path)


def test_private_path_checks_reject_unsafe_mode_owner_and_symlink():
    import stat
    class Fake:
        def __init__(self,uid,mode):self.uid=uid;self.mode=mode
        def lstat(self):return SimpleNamespace(st_uid=self.uid,st_mode=self.mode)
    remote['safe'](Fake(0,stat.S_IFREG|0o600),private=True)
    for uid,mode in ((1,stat.S_IFREG|0o600),(0,stat.S_IFREG|0o644),(0,stat.S_IFLNK|0o600)):
        with pytest.raises(ValueError):remote['safe'](Fake(uid,mode),private=True)


def test_receiver_main_fetches_human_provenance_even_for_bot_dispatch(tmp_path, monkeypatch):
    c,f,p,a=fixture();trees,get=fake_api()
    env={'CLOUD_DEPLOY_ENABLED':c['enabled'],'CLOUD_DEPLOY_PR':'800','CLOUD_DEPLOY_PR_ID':'12345',
         'CLOUD_DEPLOY_COMMIT':CONTROLLER,'CLOUD_DEPLOY_TREE':TREE,
         'GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true','GITHUB_RUN_ATTEMPT':'1',
         'GITHUB_WORKFLOW_REF':ctl.REPO+'/'+ctl.RECEIVE+'@refs/heads/main',
         'GITHUB_SHA':COMMIT,'GITHUB_WORKFLOW_SHA':COMMIT,'GITHUB_EVENT_NAME':'workflow_dispatch',
         'GITHUB_RUN_ID':'987','RUNNER_TEMP':str(tmp_path),'GITHUB_EVENT_PATH':str(tmp_path/'event.json')}
    # GitHub Actions bot did the dispatch; authority must come from freshly fetched human comment.
    (tmp_path/'event.json').write_text(json.dumps(dict(repository=dict(id=ctl.REPO_ID,full_name=ctl.REPO),
        sender=dict(id=41898282,type='Bot'),inputs=inputs())))
    for key,value in env.items():monkeypatch.setenv(key,value)
    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(ctl.dt,'datetime',Clock)
    monkeypatch.setattr(ctl.subprocess,'check_output',lambda args,**kw: TREE if args[-1]=='HEAD^{tree}' else CONTROLLER)
    seen=[]
    def api(path,data=None):
        assert data is None
        seen.append(path)
        if path=='/issues/comments/123':return f
        if path=='/pulls/800':return p
        if path=='/collaborators/carlo504/permission':return a
        return get(path)
    monkeypatch.setitem(ctl.main.__globals__,'api',api)
    ctl.main('receive')
    out=json.loads((tmp_path/'cloud-deploy-request.json').read_text())
    assert out['actor_id']==ctl.ACTOR and out['receiver_run_id']==987
    assert '/issues/comments/123' in seen and '/collaborators/carlo504/permission' in seen
    # A later edited comment fails on the second independently fetched validation.
    f['updated_at']='2026-10-03T16:00:00Z'
    with pytest.raises(ValueError):ctl.main('receive')
    f['updated_at']=f['created_at'];monkeypatch.setenv('GITHUB_RUN_ATTEMPT','2')
    with pytest.raises(ValueError):ctl.main('receive')


def test_claim_refuses_production_before_any_path_access(monkeypatch):
    monkeypatch.setattr(remote['os'],'geteuid',lambda:0)
    monkeypatch.setattr(remote['socket'],'gethostname',lambda:'hermes-poc-01')
    with pytest.raises(ValueError):remote['run'](request(),'claim')


def test_transfer_uses_only_bounded_registered_cloud_key(monkeypatch):
    transfer=runpy.run_path(str(ROOT/'scripts/transfer-test-artifact.py'))
    env=dict(GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY=ctl.REPO,TEST_VM='hermes-test-01',
        PROJECT_ID='streetsmart-hermes-poc',ZONE='us-east1-b',SSH_KEY=str(Path.home()/'.ssh/google_compute_engine'),
        CLOUD_COMMENT_ID='123',CLOUD_DEPLOY_ENABLED='TEST_DEPLOY_V1',GITHUB_SHA=COMMIT,
        ARCHIVE_SHA256='e'*64,INSTALLER_SHA256='f'*64,ARTIFACT_SHA256='a'*64)
    fn=transfer['transfer'];monkeypatch.setitem(fn.__globals__,'artifact_url',lambda env:'https://example.blob.core.windows.net/synthetic')
    calls=[]
    def runner(command,**kwargs):
        calls.append(command);return SimpleNamespace(returncode=0,stdout='TEST_ARTIFACT_TRANSFER_OK\n')
    fn(env,runner)
    assert '--ssh-key-expire-after=1h' in calls[0]
    assert '--ssh-key-file='+env['SSH_KEY'] in calls[0]
    for change in (dict(SSH_KEY='/tmp/substitute'),dict(CLOUD_DEPLOY_ENABLED=''),dict(CLOUD_COMMENT_ID='../escape')):
        with pytest.raises(ValueError):fn(dict(env,**change),runner)
    manual=dict(env,SSH_KEY='/tmp/hermes-test-deploy');manual.pop('CLOUD_COMMENT_ID')
    fn(manual,runner)
    assert '--ssh-key-file=/tmp/hermes-test-deploy' in calls[-1]


def test_every_workflow_ssh_key_use_has_bounded_registration():
    text=(ROOT/ctl.RECEIVE).read_text()
    for line in text.splitlines():
        if '--ssh-key-file=' in line:
            assert '--ssh-key-expire-after=1h' in line
