"""Require an actually executed credential-free proof before cloud authentication."""
import json
import os
from pathlib import Path
import re
import runpy


def validate(run, jobs):
    def need(ok):
        if not ok:
            raise ValueError('EVENT_PROOF_REQUIRED')
    need(run['repository']['id'] == 1343750842)
    need(re.fullmatch('[0-9a-f]{40}', run['head_sha']) is not None)
    need(run['path'] == '.github/workflows/test-operator-event-proof.yml')
    need(run['event'] == 'issue_comment' and run['head_branch'] == 'main')
    need(run['conclusion'] == 'success' and run['status'] == 'completed' and run['run_attempt'] == 1)
    need(run['actor']['id'] == run['triggering_actor']['id'] == 320188404)
    need(jobs['total_count'] == len(jobs['jobs']) == 1)
    job = jobs['jobs'][0]
    need(job['name'] == 'Prove connector event' and job['conclusion'] == 'success')
    matches = [s for s in job['steps'] if s['name'] == 'Validate connector event without cloud credentials']
    need(len(matches) == 1 and matches[0]['conclusion'] == 'success')


def main():
    run_id = os.environ['OPERATOR_EVENT_PROOF_RUN_ID']
    if not re.fullmatch('[1-9][0-9]{0,19}', run_id):
        raise ValueError('EVENT_PROOF_REQUIRED')
    controller = runpy.run_path(str(Path(__file__).with_name('test_operator_request.py')))
    get = controller['get']
    run = get('/actions/runs/' + run_id)
    jobs = get('/actions/runs/' + run_id + '/jobs?filter=latest&per_page=100')
    validate(run, jobs)
    evidence = controller['execution_guard'](os.environ['OPERATOR_SETUP_COMMIT'],
        os.environ['OPERATOR_CONTROLLER_TREE'], run['head_sha'], get)
    Path(os.environ['RUNNER_TEMP'], 'bootstrap-proof.json').write_text(json.dumps(
        dict(proof_run_id=int(run_id), proof_event_commit=run['head_sha'],
             controller_commit=os.environ['OPERATOR_SETUP_COMMIT'], execution=evidence, verified=True)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('EVENT_PROOF_REQUIRED') from None
