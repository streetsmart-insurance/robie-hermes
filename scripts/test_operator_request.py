"""Validate an issue command using fresh GitHub authorization; no cloud access."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import urllib.request

REPO = 'streetsmart-insurance/robie-hermes'
REPO_ID = 1343750842
RELEASE = '42e872f4c86fc4b4e37f859fc390f0b7c832f373'
DIGEST = '876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064'
COMMAND = re.compile(r'/robie-test (inspect|hold|install|verify) commit=([0-9a-f]{40}) sha256=([0-9a-f]{64}) nonce=([0-9a-f]{32}) expires=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)')


def require(ok):
    if not ok:
        raise ValueError('OPERATOR_REQUEST_REFUSED')


def validate(event, fresh, permission, config, now):
    require(config['enabled'] in {'INSPECT_ONLY_V1', 'STOPPED_OPERATOR_V1'})
    require(config['ref'] == 'refs/heads/main' and config['attempt'] == '1')
    require(event['action'] == 'created')
    require(event['repository']['id'] == REPO_ID and event['repository']['full_name'] == REPO)
    issue, comment = event['issue'], event['comment']
    require('pull_request' not in issue and issue['number'] == int(config['issue']))
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
    require(operation == 'inspect' if config['enabled'] == 'INSPECT_ONLY_V1'
            else operation in {'hold', 'install', 'verify'})
    require(commit == RELEASE and digest == DIGEST)
    expiry = dt.datetime.strptime(expires, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    created = dt.datetime.fromisoformat(fresh['created_at'].replace('Z', '+00:00'))
    require(created <= now < expiry <= created + dt.timedelta(minutes=30))
    return dict(version=1, operation=operation, commit=commit, sha256=digest,
                nonce=nonce, expires=expires, comment_id=fresh['id'],
                actor_id=fresh['user']['id'], issue=issue['number'])


def get(path):
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    # Only numeric event IDs and validated login names enter API paths.
    comment_id = event['comment']['id']
    require(type(comment_id) is int and comment_id > 0)
    fresh = get(f'/issues/comments/{comment_id}')
    login = fresh['user']['login']
    require(re.fullmatch(r'[A-Za-z0-9-]{1,39}', login) is not None)
    permission = get('/collaborators/' + login + '/permission')
    config = {key: os.environ[name] for key, name in {
        'enabled': 'OPERATOR_ENABLED', 'issue': 'OPERATOR_ISSUE',
        'actor_ids': 'OPERATOR_ACTOR_IDS', 'ref': 'GITHUB_REF',
        'attempt': 'GITHUB_RUN_ATTEMPT'}.items()}
    result = validate(event, fresh, permission, config, dt.datetime.now(dt.timezone.utc))
    Path(os.environ['RUNNER_TEMP'], 'operator-request.json').write_text(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('OPERATOR_REQUEST_REFUSED') from None
