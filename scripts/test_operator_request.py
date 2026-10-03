"""Validate a fixed merged-PR conversation command using fresh GitHub authorization; no cloud access."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

REPO = 'streetsmart-insurance/robie-hermes'
REPO_ID = 1343750842
RELEASE = '42e872f4c86fc4b4e37f859fc390f0b7c832f373'
DIGEST = '876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064'
# Fixed execution dependency manifest, read from this approved checkout only.
# Git blob identity, object type and mode must match at event and current main.
SECURITY_PATHS = (
    '.github/workflows/test-operator-event-proof.yml',
    '.github/workflows/test-operator-bootstrap.yml',
    '.github/workflows/test-operator-inspect.yml',
    '.github/workflows/test-operator-stopped.yml',
    'scripts/test_operator_request.py', 'scripts/test_operator_setup_proof.py',
    'scripts/test_operator_bootstrap.py', 'scripts/test_operator_inspect.py',
    'scripts/test_operator_stopped.py', 'scripts/ensure-gcloud-ssh-key.sh',
    'deploy/test-stopped-approval.disabled.json',
)
WORKFLOWS = {
    'EVENT_PROOF_V1': '.github/workflows/test-operator-event-proof.yml',
    'BOOTSTRAP_INSPECT_V1': '.github/workflows/test-operator-bootstrap.yml',
    'INSPECT_ONLY_V1': '.github/workflows/test-operator-inspect.yml',
    'STOPPED_OPERATOR_V1': '.github/workflows/test-operator-stopped.yml',
}
COMMAND = re.compile(r'/robie-test (event-proof|bootstrap-inspect|inspect|prepare-hold|hold|install|verify) commit=([0-9a-f]{40}) sha256=([0-9a-f]{64}) nonce=([0-9a-f]{32}) expires=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)')


def require(ok):
    if not ok:
        raise ValueError('OPERATOR_REQUEST_REFUSED')


def validate(event, fresh, permission, config, now, pull_request):
    modes = {'INSPECT_ONLY_V1': {'inspect'}, 'STOPPED_OPERATOR_V1': {'prepare-hold', 'hold', 'install', 'verify'},
             'EVENT_PROOF_V1': {'event-proof'}, 'BOOTSTRAP_INSPECT_V1': {'bootstrap-inspect'}}
    require(config['enabled'] in modes)
    require(config['ref'] == 'refs/heads/main' and config['attempt'] == '1')
    require(event['action'] == 'created')
    require(event['repository']['id'] == REPO_ID and event['repository']['full_name'] == REPO)
    issue, comment = event['issue'], event['comment']
    require('pull_request' in issue and type(issue['number']) is int and issue['number'] == int(config['issue']))
    require(issue['pull_request']['url'] == f'https://api.github.com/repos/{REPO}/pulls/{issue["number"]}')
    require(type(pull_request['id']) is int and pull_request['id'] == int(config['pr_id']))
    require(type(pull_request['number']) is int and pull_request['number'] == issue['number'])
    require(pull_request['merged'] is True and pull_request['state'] == 'closed')
    require(pull_request['base']['repo']['id'] == REPO_ID and pull_request['base']['repo']['full_name'] == REPO)
    require(pull_request['base']['ref'] == 'main')
    require(re.fullmatch('[0-9a-f]{40}', config['approved_controller_commit']) is not None)
    require(config['controller_commit'] == config['approved_controller_commit'] == pull_request['merge_commit_sha'])
    require(re.fullmatch('[0-9a-f]{40}', config['approved_controller_tree']) is not None)
    require(config['controller_tree'] == config['checkout_tree'] == config['approved_controller_tree'])
    require(config['actor_ids'] == '320188404' and fresh['user']['id'] == 320188404)
    require(fresh['issue_url'] == f'https://api.github.com/repos/{REPO}/issues/{issue["number"]}')
    require(comment['id'] == fresh['id'] and comment['body'] == fresh['body'])
    require(comment['user']['id'] == fresh['user']['id'] == event['sender']['id'])
    require(fresh['user']['type'] == 'User')
    require(str(fresh['user']['id']) in config['actor_ids'].split(','))
    require(permission['user']['id'] == fresh['user']['id'])
    require(permission['permission'] in {'admin', 'maintain', 'write'})
    require(fresh['created_at'] == fresh['updated_at'])
    match = COMMAND.fullmatch(fresh['body'])
    require(match is not None)
    operation, commit, digest, nonce, expires = match.groups()
    require(operation in modes[config['enabled']])
    require(commit == RELEASE and digest == DIGEST)
    expiry = dt.datetime.strptime(expires, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    created = dt.datetime.fromisoformat(fresh['created_at'].replace('Z', '+00:00'))
    require(created <= now < expiry <= created + dt.timedelta(minutes=30))
    return dict(version=1, operation=operation, commit=commit, sha256=digest,
                nonce=nonce, expires=expires, comment_id=fresh['id'],
                actor_id=fresh['user']['id'], issue=issue['number'], pr_id=pull_request['id'])


def get(path):
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def revision_manifest(commit, getter, expected_tree=None):
    require(re.fullmatch('[0-9a-f]{40}', commit or '') is not None)
    revision = getter('/git/commits/' + commit)
    require(revision['sha'] == commit)
    tree_sha = revision['tree']['sha']
    require(re.fullmatch('[0-9a-f]{40}', tree_sha or '') is not None)
    if expected_tree is not None:
        require(tree_sha == expected_tree)
    tree = getter('/git/trees/' + tree_sha + '?recursive=1')
    require(tree['sha'] == tree_sha and tree['truncated'] is False)
    require(len(tree['tree']) <= 100000)
    entries = {}
    seen = set()
    for entry in tree['tree']:
        path = entry['path']
        require(path not in seen)
        seen.add(path)
        if path in SECURITY_PATHS:
            require(entry['type'] == 'blob' and entry['mode'] in {'100644', '100755'})
            require(re.fullmatch('[0-9a-f]{40}', entry['sha']) is not None)
            entries[path] = {key: entry[key] for key in ('sha', 'type', 'mode')}
    require(set(entries) == set(SECURITY_PATHS))
    return entries


def execution_guard(approved, approved_tree, event_sha, getter, workflow_sha=None):
    """Drift check within existing protected-main trust, not a WIF boundary.

    A privileged main author can replace a workflow and omit this check. Do not
    claim this comparison constrains that existing principal's cloud authority.
    """
    require(re.fullmatch('[0-9a-f]{40}', approved_tree or '') is not None)
    workflow_sha = event_sha if workflow_sha is None else workflow_sha
    require(re.fullmatch('[0-9a-f]{40}', workflow_sha or '') is not None)
    reference = getter('/git/ref/heads/main')
    require(reference['ref'] == 'refs/heads/main' and reference['object']['type'] == 'commit')
    main_sha = reference['object']['sha']
    expected = revision_manifest(approved, getter, approved_tree)
    for revision in set((event_sha, workflow_sha, main_sha)):
        if revision != approved:
            require(revision_manifest(revision, getter) == expected)
    return dict(approved_controller_commit=approved, approved_controller_tree=approved_tree,
                event_commit=event_sha, executing_workflow_commit=workflow_sha, observed_main_commit=main_sha,
                execution_manifest_sha256=hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                security_paths_unchanged=True)


def main():
    require(os.environ['GITHUB_WORKFLOW_REF'] ==
            REPO + '/' + WORKFLOWS[os.environ['OPERATOR_ENABLED']] + '@refs/heads/main')
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    # Only numeric event IDs and validated login names enter API paths.
    comment_id = event['comment']['id']
    require(type(comment_id) is int and comment_id > 0)
    fresh = get(f'/issues/comments/{comment_id}')
    login = fresh['user']['login']
    require(re.fullmatch(r'[A-Za-z0-9-]{1,39}', login) is not None)
    permission = get('/collaborators/' + login + '/permission')
    config = {key: os.environ[name] for key, name in {
        'enabled': 'OPERATOR_ENABLED', 'issue': 'OPERATOR_PR_NUMBER', 'pr_id': 'OPERATOR_PR_ID',
        'actor_ids': 'OPERATOR_ACTOR_IDS', 'ref': 'GITHUB_REF',
        'attempt': 'GITHUB_RUN_ATTEMPT'}.items()}
    for field in ('issue', 'pr_id'):
        require(re.fullmatch('[1-9][0-9]{0,19}', config[field]) is not None)
    approved = os.environ['OPERATOR_SETUP_COMMIT']
    require(re.fullmatch('[0-9a-f]{40}', approved) is not None)
    pull_request = get('/pulls/' + config['issue'])
    commit = get('/git/commits/' + approved)
    require(commit['sha'] == approved)
    config.update(controller_commit=approved, approved_controller_commit=approved,
                  approved_controller_tree=os.environ['OPERATOR_CONTROLLER_TREE'],
                  controller_tree=commit['tree']['sha'],
                  checkout_tree=subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'],
                     check=True, capture_output=True, text=True, timeout=10).stdout.strip())
    require(subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True,
                           text=True, timeout=10).stdout.strip() == approved)
    result = validate(event, fresh, permission, config, dt.datetime.now(dt.timezone.utc), pull_request)
    evidence = execution_guard(approved, config['approved_controller_tree'], os.environ['GITHUB_SHA'], get,
                               workflow_sha=os.environ['GITHUB_WORKFLOW_SHA'])
    evidence.update(pr_number=pull_request['number'], pr_id=pull_request['id'],
                    executing_workflow_ref=os.environ['GITHUB_WORKFLOW_REF'])
    Path(os.environ['RUNNER_TEMP'], 'controller-evidence.json').write_text(json.dumps(evidence, sort_keys=True))
    Path(os.environ['RUNNER_TEMP'], 'operator-request.json').write_text(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('OPERATOR_REQUEST_REFUSED') from None
