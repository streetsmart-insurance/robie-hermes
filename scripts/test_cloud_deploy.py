"""Disabled-by-default, fixed Test deployment dispatcher and receiving validator.

No arbitrary workflow/ref/operation inputs. The original human comment is fetched
again by the receiving workflow; dispatcher inputs are identifiers, not authority.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

REPO = 'streetsmart-insurance/robie-hermes'
REPO_ID = 1343750842
ACTOR = 320188404
DISPATCH = '.github/workflows/test-cloud-deploy.yml'
RECEIVE = '.github/workflows/deploy-test.yml'
PATHS = (DISPATCH, RECEIVE, 'scripts/test_cloud_deploy.py',
         'scripts/test_cloud_deploy_claim.py', 'scripts/build-release.sh',
         'scripts/transfer-test-artifact.py', 'scripts/deploy-test-release.sh',
         'scripts/ensure-gcloud-ssh-key.sh')
COMMAND = re.compile(r'/robie-test deploy commit=([0-9a-f]{40}) sha256=([0-9a-f]{64}) prior=([0-9a-f]{40}) prior_sha256=([0-9a-f]{64}) nonce=([0-9a-f]{32}) expires=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)')


def need(ok):
    if not ok:
        raise ValueError('TEST_CLOUD_DEPLOY_REFUSED')


def api(path, data=None):
    req = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        need(len(raw) <= 4 * 1024 * 1024)
        return json.loads(raw) if raw else None


def numeric(value):
    need(isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,19}', value) is not None)
    return int(value)


def validate_comment(comment, pr, permission, config, now):
    need(config['enabled'] == 'TEST_DEPLOY_V1')
    need(comment['user']['id'] == ACTOR and comment['user']['type'] == 'User')
    need(permission['user']['id'] == ACTOR and permission['permission'] in {'admin', 'maintain', 'write'})
    need(comment['issue_url'] == f'https://api.github.com/repos/{REPO}/issues/{config["pr"]}')
    need(type(comment['id']) is int and comment['id'] > 0)
    need(comment['created_at'] == comment['updated_at'])
    need(type(pr['number']) is int and pr['number'] == config['pr'])
    need(type(pr['id']) is int and pr['id'] == config['pr_id'])
    need(pr['merged'] is True and pr['state'] == 'closed')
    need(pr['base']['ref'] == 'main' and pr['base']['repo']['id'] == REPO_ID
         and pr['base']['repo']['full_name'] == REPO)
    need(pr['merge_commit_sha'] == config['controller'])
    match = COMMAND.fullmatch(comment['body'])
    need(match is not None)
    commit, digest, prior, prior_digest, nonce, expires = match.groups()
    created = dt.datetime.fromisoformat(comment['created_at'].replace('Z', '+00:00'))
    expiry = dt.datetime.strptime(expires, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    need(created <= now < expiry <= created + dt.timedelta(minutes=30))
    need(commit != prior)  # This route is a new deployment, never an implicit reinstall.
    return dict(version=1, operation='deploy', commit=commit, sha256=digest,
                prior=prior, prior_sha256=prior_digest, nonce=nonce, expires=expires,
                comment_id=comment['id'], actor_id=ACTOR, actor_login=comment['user']['login'],
                pr=config['pr'], pr_id=config['pr_id'])


def manifest(sha, getter, tree=None):
    need(re.fullmatch('[0-9a-f]{40}', sha or '') is not None)
    commit = getter('/git/commits/' + sha)
    need(commit['sha'] == sha)
    tree_sha = commit['tree']['sha']
    need(re.fullmatch('[0-9a-f]{40}', tree_sha) is not None)
    need(tree is None or tree_sha == tree)
    result = getter('/git/trees/' + tree_sha + '?recursive=1')
    need(result['sha'] == tree_sha and result['truncated'] is False)
    entries = {}; seen = set()
    for item in result['tree']:
        path = item['path']; need(path not in seen); seen.add(path)
        if path in PATHS:
            need(item['type'] == 'blob' and item['mode'] in {'100644', '100755'})
            need(re.fullmatch('[0-9a-f]{40}', item['sha']) is not None)
            entries[path] = {k:item[k] for k in ('sha', 'type', 'mode')}
    need(set(entries) == set(PATHS))
    return entries


def controller_guard(config, event_sha, workflow_sha, getter):
    need(re.fullmatch('[0-9a-f]{40}', config['tree'] or '') is not None)
    ref = getter('/git/ref/heads/main')
    need(ref['ref'] == 'refs/heads/main' and ref['object']['type'] == 'commit')
    current = ref['object']['sha']
    expected = manifest(config['controller'], getter, config['tree'])
    for sha in {current, event_sha, workflow_sha}:
        need(manifest(sha, getter) == expected)
    return dict(controller_commit=config['controller'], controller_tree=config['tree'],
                event_commit=event_sha, workflow_commit=workflow_sha, observed_main=current,
                manifest_sha256=hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest())


def validate_dispatch_event(event, comment, config):
    need(event['action'] == 'created' and event['repository']['id'] == REPO_ID
         and event['repository']['full_name'] == REPO)
    need(event['issue']['number'] == config['pr'] and 'pull_request' in event['issue'])
    need(event['issue']['pull_request']['url'] == f'https://api.github.com/repos/{REPO}/pulls/{config["pr"]}')
    need(event['sender']['id'] == event['comment']['user']['id'] == ACTOR)
    need(event['comment']['id'] == comment['id'] and event['comment']['body'] == comment['body'])


def validate_receiver(inputs, request, event_sha):
    # Manual dispatch cannot turn a comment into certify/stopped/Production work.
    need(inputs['operation'] == 'deploy' and inputs['confirmation'] == 'DEPLOY_TO_HERMES_TEST_01')
    need(str(request['comment_id']) == inputs['cloud_comment_id'])
    need(request['commit'] == event_sha)
    need(all(not inputs.get(k) for k in ('test_run_id', 'test_artifact_id', 'release_sha256', 'qa_evidence')))


def main(mode):
    need(mode in {'dispatch', 'receive'})
    need(os.environ['GITHUB_REF'] == 'refs/heads/main' and os.environ['GITHUB_REF_PROTECTED'] == 'true')
    need(os.environ['GITHUB_RUN_ATTEMPT'] == '1')
    path = DISPATCH if mode == 'dispatch' else RECEIVE
    need(os.environ['GITHUB_WORKFLOW_REF'] == REPO + '/' + path + '@refs/heads/main')
    config = dict(enabled=os.environ['CLOUD_DEPLOY_ENABLED'],
        pr=numeric(os.environ['CLOUD_DEPLOY_PR']), pr_id=numeric(os.environ['CLOUD_DEPLOY_PR_ID']),
        controller=os.environ['CLOUD_DEPLOY_COMMIT'], tree=os.environ['CLOUD_DEPLOY_TREE'])
    checkout = Path(__file__).resolve().parents[1]
    for expression, expected in [('HEAD', config['controller']), ('HEAD^{tree}', config['tree'])]:
        need(subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', expression], text=True).strip() == expected)
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    need(event['repository']['id'] == REPO_ID and event['repository']['full_name'] == REPO)
    if mode == 'dispatch':
        need(os.environ['GITHUB_EVENT_NAME'] == 'issue_comment')
        comment_id = event['comment']['id']; need(type(comment_id) is int and comment_id > 0)
    else:
        need(os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch')
        comment_id = numeric(event['inputs']['cloud_comment_id'])
    comment = api('/issues/comments/' + str(comment_id))
    need(comment['id'] == comment_id)
    login = comment['user']['login']; need(re.fullmatch('[A-Za-z0-9-]{1,39}', login) is not None)
    request = validate_comment(comment, api('/pulls/' + str(config['pr'])),
        api('/collaborators/' + login + '/permission'), config, dt.datetime.now(dt.timezone.utc))
    evidence = controller_guard(config, os.environ['GITHUB_SHA'], os.environ['GITHUB_WORKFLOW_SHA'], api)
    if mode == 'dispatch':
        validate_dispatch_event(event, comment, config)
        need(request['commit'] == evidence['observed_main'])
    else:
        validate_receiver(event['inputs'], request, os.environ['GITHUB_SHA'])
    request['controller_commit'] = config['controller']
    request['receiver_run_id'] = numeric(os.environ['GITHUB_RUN_ID'])
    output = Path(os.environ['RUNNER_TEMP'])
    (output / 'cloud-deploy-request.json').write_text(json.dumps(request, sort_keys=True))
    (output / 'cloud-deploy-controller.json').write_text(json.dumps(evidence, sort_keys=True))
    if mode == 'dispatch':
        # No retry: an ambiguous POST result must be reconciled by read-only run lookup.
        api('/actions/workflows/deploy-test.yml/dispatches', {'ref':'main', 'inputs':{
            'operation':'deploy', 'confirmation':'DEPLOY_TO_HERMES_TEST_01',
            'cloud_comment_id':str(comment_id)}})


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception:
        raise SystemExit('TEST_CLOUD_DEPLOY_REFUSED: no automatic retry') from None
