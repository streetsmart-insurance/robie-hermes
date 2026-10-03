import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trigger = load('test_operator_request')
host = load('test_operator_inspect')


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 2, 23, 0, tzinfo=dt.timezone.utc)
        self.fresh = dict(id=100, body=f'/robie-test inspect commit={trigger.RELEASE} sha256={trigger.DIGEST} nonce={"a"*32} expires=2026-10-02T23:20:00Z',
                          user=dict(id=320188404, type='User'), created_at='2026-10-02T23:00:00Z',
                          updated_at='2026-10-02T23:00:00Z',
                          issue_url=f'https://api.github.com/repos/{trigger.REPO}/issues/900')
        self.event = dict(action='created', repository=dict(id=trigger.REPO_ID, full_name=trigger.REPO),
                          issue=dict(number=900, pull_request={'url': f'https://api.github.com/repos/{trigger.REPO}/pulls/900'}), comment=copy.deepcopy(self.fresh), sender=dict(id=320188404))
        self.permission = dict(user=dict(id=320188404), permission='write')
        self.config = dict(enabled='INSPECT_ONLY_V1', issue='900', actor_ids='320188404',
                           ref='refs/heads/main', attempt='1', pr_id='12345',
                           controller_commit='a'*40, approved_controller_commit='a'*40,
                           controller_tree='b'*40, checkout_tree='b'*40, approved_controller_tree='b'*40)
        self.pr = dict(id=12345, number=900, merged=True, state='closed', merge_commit_sha='a'*40,
                       base={'ref':'main', 'repo':{'id':trigger.REPO_ID, 'full_name':trigger.REPO}})

    def request(self):
        return trigger.validate(self.event, self.fresh, self.permission, self.config, self.now, self.pr)

    def test_valid(self):
        self.assertEqual(self.request()['operation'], 'inspect')

    def test_stopped_operations_require_separate_enablement_and_exact_grammar(self):
        original = self.fresh['body']
        for operation in ('prepare-hold', 'hold', 'install', 'verify'):
            self.fresh['body'] = self.event['comment']['body'] = original.replace('inspect', operation)
            self.config['enabled'] = 'INSPECT_ONLY_V1'
            with self.assertRaises(ValueError):
                self.request()
            self.config['enabled'] = 'STOPPED_OPERATOR_V1'
            self.assertEqual(self.request()['operation'], operation)
        self.fresh['body'] = self.event['comment']['body'] = original.replace('inspect', 'hold') + ' approved=true'
        with self.assertRaises(ValueError):
            self.request()

    def test_unauthorized_envelopes(self):
        changes = [lambda: self.config.update(enabled=''),
                   lambda: self.config.update(ref='refs/heads/feature'),
                   lambda: self.config.update(attempt='2'),
                   lambda: self.config.update(actor_ids='43'),
                   lambda: self.permission.update(permission='read'),
                   lambda: self.permission['user'].update(id=43),
                   lambda: self.event['issue'].pop('pull_request'),
                   lambda: self.event['issue'].update(number=901),
                   lambda: self.event['repository'].update(id=1),
                   lambda: self.event['sender'].update(id=43),
                   lambda: self.fresh.update(updated_at='2026-10-02T23:01:00Z'),
                   lambda: self.fresh.update(issue_url='https://example.com'),
                   lambda: self.fresh['user'].update(type='Bot')]
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                change()
                with self.assertRaises(ValueError):
                    self.request()

    def test_command_injection_wrong_release_mutations_and_expiry(self):
        original = self.fresh['body']
        bodies = [original + '\n', original + '; id', original.replace('inspect', 'install'),
                  original.replace('inspect', 'resume'), original.replace(trigger.DIGEST, '0'*64),
                  original.replace(trigger.RELEASE, '0'*40), original.replace('23:20', '23:00'),
                  original.replace('23:20', '23:31'), original.replace('a'*32, '../bad')]
        for body in bodies:
            with self.subTest(body=body):
                self.fresh['body'] = self.event['comment']['body'] = body
                with self.assertRaises(ValueError):
                    self.request()

    def test_host_checks(self):
        request = self.request()
        config = dict(enabled='INSPECT_ONLY_V1', commit=host.RELEASE, sha256=host.DIGEST,
                      actor_ids=[320188404], issue=900, pr_id=12345)
        host.validate(request, config, self.now)
        for key, value in [('operation', 'hold'), ('nonce', '../bad'), ('actor_id', 43),
                           ('issue', 901), ('comment_id', True), ('sha256', '0'*64),
                           ('expires', '2026-10-02T23:00:00Z')]:
            changed = dict(request, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                host.validate(changed, config, self.now)

    def test_replay_claim_survives_restart_and_conflicts(self):
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            host.consume(request, state)
            for candidate in (request, dict(request, nonce='b'*32), dict(request, comment_id=101)):
                with self.assertRaises(FileExistsError):
                    host.consume(candidate, state)
            self.assertEqual(json.loads((state / 'comment_id-100').read_text()), request)
            self.assertEqual((state / 'nonce-' ).exists(), False)

    def test_snapshot_only_fixed_reads_and_refuses_pointer_escape(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if command[2] == 'crond.service':
                return SimpleNamespace(stdout='LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n')
            if command[2].endswith('.timer'):
                return SimpleNamespace(stdout='LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n')
            return SimpleNamespace(stdout='LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nUnitFileState=disabled\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'releases' / 'old'
            release.mkdir(parents=True)
            (root / 'current').symlink_to(release)
            (root / 'releases/current').symlink_to(release)
            result = host.snapshot(runner, root)
            self.assertFalse(result['hold_verified'])
            self.assertEqual(result['units']['crond.service']['LoadState'], 'not-found')
            self.assertIsNone(result['units']['crond.service']['MainPID'])
            self.assertIsNone(result['units']['robie-scheduler.timer']['MainPID'])
            self.assertEqual(result['units']['robie-scheduler.timer']['SubState'], 'waiting')
            self.assertEqual(len(calls), len(host.UNITS + host.AUXILIARY_UNITS))
            self.assertTrue(all(command[:2] == ['/usr/bin/systemctl', 'show'] for command in calls))
            (root / 'current').unlink()
            (root / 'current').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                host.snapshot(runner, root)

    def test_missing_unit_does_not_invent_states_or_hide_workers(self):
        absent = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
        for suffix in ('', 'UnitFileState=\n', 'UnitFileState=\nMainPID=0\nControlPID=0\n'):
            result = host.unit_properties('hermes-gateway.service', absent + suffix)
            self.assertEqual(result['LoadState'], 'not-found')
        invalid = [absent.replace('inactive', 'active'), absent.replace('dead', 'running'),
                   absent + 'MainPID=123\n', absent + 'ControlPID=123\n',
                   absent + 'UnitFileState=enabled\n', absent + 'LoadState=loaded\n',
                   absent.replace('LoadState=not-found', 'LoadState=loaded'),
                   absent + 'Environment=secret\n']
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ValueError):
                host.unit_properties('crond.service', output)
        with self.assertRaises(ValueError):
            host.unit_properties('robie-gateway.service', absent)

    def test_loaded_timer_properties_do_not_require_service_interface(self):
        timer = 'LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n'
        result = host.unit_properties('robie-scheduler.timer', timer)
        self.assertIsNone(result['MainPID'])
        self.assertIsNone(result['ControlPID'])
        for suffix in ('MainPID=123\n', 'ControlPID=123\n'):
            with self.assertRaises(ValueError):
                host.unit_properties('robie-scheduler.timer', timer + suffix)
        with self.assertRaises(ValueError):
            host.unit_properties('robie-scheduler.service', timer)

    def test_only_successfully_authorized_job_enters_deployment_queue(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/test-operator-inspect.yml').read_text())
        self.assertNotIn('concurrency', workflow)
        jobs = workflow['jobs']
        self.assertNotIn('concurrency', jobs['authorize'])
        self.assertEqual(jobs['inspect']['needs'], 'authorize')
        self.assertIn("needs.authorize.result == 'success'", jobs['inspect']['if'])
        self.assertEqual(jobs['inspect']['concurrency'],
                         {'group': 'robie-hermes-test-deploy', 'cancel-in-progress': False})

    def test_workflow_no_dynamic_shell_or_runtime_install(self):
        text = (ROOT / '.github/workflows/test-operator-inspect.yml').read_text()
        self.assertNotIn('${{ github.event.comment.body }}', text)
        self.assertNotIn('deploy-test-release.sh', text)
        self.assertNotIn('pull_request_target:', text)
        self.assertIn('environment: Test-Operator-Inspect', text)
        self.assertIn('group: robie-hermes-test-deploy', text)
        self.assertEqual(text.count('python3 -I -B scripts/test_operator_request.py'), 2)


if __name__ == '__main__':
    unittest.main()


class FixedMergedPRTests(unittest.TestCase):
    setUp = OperatorTests.setUp
    request = OperatorTests.request

    def test_wrong_pr_identity_or_merge_state_refused(self):
        changes = [lambda p: p.update(id=999), lambda p: p.update(number=901),
                   lambda p: p.update(id='12345'), lambda p: p.update(merged=False),
                   lambda p: p.update(state='open'), lambda p: p.update(merge_commit_sha='c'*40),
                   lambda p: p['base'].update(ref='other'),
                   lambda p: p['base']['repo'].update(id=99),
                   lambda p: p['base']['repo'].update(full_name='other/repository')]
        for change in changes:
            candidate = copy.deepcopy(self.pr)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                trigger.validate(self.event, self.fresh, self.permission, self.config, self.now, candidate)

    def test_all_modes_require_reviewed_tree_and_exact_controller_commit(self):
        for mode, operation in [('EVENT_PROOF_V1','event-proof'), ('BOOTSTRAP_INSPECT_V1','bootstrap-inspect'),
                                ('INSPECT_ONLY_V1','inspect'), ('STOPPED_OPERATOR_V1','prepare-hold')]:
            self.setUp()
            self.config['enabled']=mode
            self.fresh['body']=self.event['comment']['body']=self.fresh['body'].replace('inspect',operation)
            self.request()
            for field in ('controller_commit','controller_tree','checkout_tree','approved_controller_tree'):
                changed=dict(self.config, **{field:'c'*40})
                with self.subTest(mode=mode,field=field), self.assertRaises(ValueError):
                    trigger.validate(self.event,self.fresh,self.permission,changed,self.now,self.pr)

    def test_pr_head_is_never_the_execution_identity(self):
        self.pr['head']={'sha':'e'*40,'ref':'untrusted','repo':{'full_name':'fork/repo'}}
        result=self.request()
        self.assertEqual(result['pr_id'],12345)
        self.assertNotIn('head',result)
        self.event['issue']['pull_request']['url']='https://api.github.com/repos/other/repo/pulls/900'
        with self.assertRaises(ValueError):
            self.request()

    def test_all_workflows_pin_controller_and_require_main_pr_conversation(self):
        for name in ('inspect','stopped','event-proof','bootstrap'):
            text=(ROOT/f'.github/workflows/test-operator-{name}.yml').read_text()
            data=yaml.safe_load(text)
            self.assertNotIn("github.sha == vars.ROBIE_TEST_OPERATOR_SETUP_COMMIT",text)
            self.assertIn("github.ref == 'refs/heads/main'",text)
            self.assertIn('github.event.issue.pull_request &&',text)
            self.assertNotIn('!github.event.issue.pull_request',text)
            self.assertNotIn('pull_request_target',text)
            self.assertNotIn('ROBIE_TEST_OPERATOR_ISSUE',text)
            self.assertIn('OPERATOR_PR_ID:',text)
            self.assertIn('OPERATOR_CONTROLLER_TREE:',text)
            for job in data['jobs'].values():
                self.assertEqual(job['permissions']['pull-requests'],'read')
                for step in job['steps']:
                    if step.get('uses','').startswith('actions/checkout@'):
                        self.assertEqual(step['with']['ref'],'${{ vars.ROBIE_TEST_OPERATOR_SETUP_COMMIT }}')
