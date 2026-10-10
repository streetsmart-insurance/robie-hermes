"""Operation scope: document_upload and note_append to any client, switched only by a root-owned policy file.

Closed by default. No environment variable opens it. Only the two named
entrypoints can hold it, the sealed agent interpreter never can, and every
other write kind keeps the applicant allowlist.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import robie_job_engine.ezlynx_write_scope as scope

ROOT = Path(__file__).resolve().parents[1]
OTHER = "999999999"
UPLOAD = scope.OPERATION_DOCUMENT_UPLOAD
NOTE = scope.OPERATION_NOTE_APPEND


def policy_body(**overrides):
    body = {
        "version": 1,
        "environment": "PRODUCTION",
        "approved_by": "Carlo Ferrara",
        "approved_at": "2026-10-10T17:54:00-04:00",
        "entrypoints": {"robie_filer": {"operations": [UPLOAD, NOTE]}},
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Production-looking process, no scope registered, files owned by the test user."""
    monkeypatch.setattr(scope, "_OPERATION_SCOPE", None)
    monkeypatch.setattr(scope, "_TRUSTED_OWNER_UID", os.getuid())
    monkeypatch.setattr(scope, "_hostname", lambda: "hermes-poc-01")
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    for name in ("ROBIE_EZLYNX_WRITE_SCOPE", "ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "EZLYNX_WRITE_APPLICANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    def write(body=None, *, mode=0o644, raw=None):
        path = tmp_path / "ezlynx-write-scope.json"
        path.write_text(raw if raw is not None else json.dumps(body or policy_body()))
        path.chmod(mode)
        monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", path)
        return path

    tmp_path.chmod(0o755)
    return write


# ------------------------------------------------------------------ closed by default
def test_no_policy_file_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", tmp_path / "absent.json")
    assert scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER) is None
    assert scope.operation_scope_registered() is False
    assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False
    with pytest.raises(scope.EzlynxWriteScopeError, match="EZLYNX_WRITE_SCOPE_REFUSED"):
        scope.require_allowed_ezlynx_write_applicant(OTHER, operation=UPLOAD)


def test_filer_registration_refuses_without_a_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", tmp_path / "absent.json")
    with pytest.raises(scope.EzlynxWriteScopeError, match="root-owned write-scope policy"):
        scope.register_filer_operation_scope()


def test_the_test_account_still_passes_without_any_policy():
    assert scope.require_allowed_ezlynx_write_applicant("220250093", operation=UPLOAD) == "220250093"


# ------------------------------------------------------------------ a valid policy
def test_valid_policy_allows_exactly_the_two_operations_for_any_client(policy_file):
    policy_file()
    info = scope.register_filer_operation_scope()
    assert info["operations"] == frozenset({UPLOAD, NOTE})
    for operation in (UPLOAD, NOTE):
        assert scope.operation_is_write_allowed(OTHER, operation) is True
        assert scope.require_allowed_ezlynx_write_applicant(OTHER, operation=operation) == OTHER
    # nothing else opens
    for operation in (None, "", "policy_create", "discussion_create", "task_create", "reassign", "delete"):
        assert scope.operation_is_write_allowed(OTHER, operation) is False
        with pytest.raises(scope.EzlynxWriteScopeError):
            scope.require_allowed_ezlynx_write_applicant(OTHER, operation=operation)
    # and the plain applicant allowlist did not move
    assert scope.applicant_is_write_allowed(OTHER) is False
    with pytest.raises(scope.EzlynxWriteScopeError):
        scope.require_allowed_ezlynx_write_applicant(OTHER)
    assert scope.applicant_is_write_allowed_for(OTHER, UPLOAD) is True
    assert scope.applicant_is_write_allowed_for(OTHER, None) is False


def test_an_implausible_applicant_is_never_allowed(policy_file):
    policy_file()
    scope.register_filer_operation_scope()
    for bad in ("", "0", "abc", "12 34", "-5", "1.5", None):
        assert scope.operation_is_write_allowed(bad, UPLOAD) is False


def test_only_the_listed_operations_are_allowed(policy_file):
    policy_file(policy_body(entrypoints={"robie_filer": {"operations": [NOTE]}}))
    scope.register_filer_operation_scope()
    assert scope.operation_is_write_allowed(OTHER, NOTE) is True
    assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False


def test_an_entrypoint_not_in_the_policy_gets_nothing(policy_file):
    policy_file(policy_body(entrypoints={"ezlynx_api_cli": {"operations": [NOTE]}}))
    assert scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER) is None
    assert scope.operation_scope_registered() is False
    info = scope.register_operation_scope(scope.ENTRYPOINT_EZLYNX_API_CLI)
    assert info["operations"] == frozenset({NOTE})


def test_a_process_cannot_hold_two_entrypoint_scopes(policy_file):
    policy_file(policy_body(entrypoints={
        "robie_filer": {"operations": [NOTE]}, "ezlynx_api_cli": {"operations": [NOTE]},
    }))
    scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER)
    with pytest.raises(scope.EzlynxWriteScopeError, match="already holds"):
        scope.register_operation_scope(scope.ENTRYPOINT_EZLYNX_API_CLI)


def test_unknown_entrypoint_cannot_register(policy_file):
    policy_file()
    with pytest.raises(scope.EzlynxWriteScopeError, match="not an entrypoint"):
        scope.register_operation_scope("some_agent_script")


def test_policy_hash_and_contents_are_logged_and_returned(policy_file, caplog):
    path = policy_file()
    import hashlib

    with caplog.at_level(logging.WARNING, logger=scope.logger.name):
        info = scope.register_filer_operation_scope()
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads(info["contents"]) == policy_body()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert info["sha256"] in text and "robie_filer" in text and "note_append" in text


# ------------------------------------------------------------------ a damaged policy never widens
@pytest.mark.parametrize(
    "body, why",
    [
        (policy_body(version=2), "version"),
        (policy_body(version=True), "version"),
        (policy_body(environment="TEST"), "is for TEST"),
        (policy_body(environment="STAGING"), "environment"),
        (policy_body(approved_by=""), "approved_by"),
        (policy_body(approved_by=7), "approved_by"),
        (policy_body(approved_at="yesterday"), "approved_at"),
        (policy_body(extra=1), "unknown keys"),
        (policy_body(entrypoints={}), "non-empty"),
        (policy_body(entrypoints={"some_script": {"operations": [NOTE]}}), "unknown entrypoint"),
        (policy_body(entrypoints={"robie_filer": {"operations": []}}), "non-empty operations"),
        (policy_body(entrypoints={"robie_filer": {"operations": [NOTE, NOTE]}}), "twice"),
        (policy_body(entrypoints={"robie_filer": {"operations": ["policy_create"]}}), "cannot be allowed"),
        (policy_body(entrypoints={"robie_filer": {"operations": [NOTE], "all": True}}), "unexpected keys"),
        (policy_body(entrypoints={"robie_filer": {"operations": ["*"]}}), "cannot be allowed"),
        (["not", "an", "object"], "JSON object"),
    ],
)
def test_bad_policy_contents_are_refused(policy_file, body, why):
    policy_file(body)
    with pytest.raises(scope.EzlynxWriteScopeError, match=why):
        scope.register_filer_operation_scope()
    assert scope.operation_scope_registered() is False


def test_not_json_is_refused(policy_file):
    policy_file(raw="{not json")
    with pytest.raises(scope.EzlynxWriteScopeError, match="not valid JSON"):
        scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER)


def test_oversize_file_is_refused(policy_file):
    policy_file(raw=json.dumps(policy_body(approved_by="x" * (scope.MAX_POLICY_BYTES + 5))))
    with pytest.raises(scope.EzlynxWriteScopeError, match="larger than expected"):
        scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER)


def test_wrong_host_is_refused(policy_file, monkeypatch):
    policy_file()
    monkeypatch.setattr(scope, "_hostname", lambda: "hermes-test-01")
    with pytest.raises(scope.EzlynxWriteScopeError, match="host hermes-poc-01"):
        scope.register_filer_operation_scope()


def test_policy_for_another_environment_is_refused(policy_file, monkeypatch):
    policy_file()
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    with pytest.raises(scope.EzlynxWriteScopeError, match="is for PRODUCTION"):
        scope.register_filer_operation_scope()


def test_group_or_world_writable_file_is_refused(policy_file):
    for mode in (0o664, 0o646, 0o666, 0o620):
        policy_file(mode=mode)
        with pytest.raises(scope.EzlynxWriteScopeError, match="writable"):
            scope.register_filer_operation_scope()


def test_writable_directory_is_refused(policy_file, tmp_path):
    policy_file()
    tmp_path.chmod(0o775)
    try:
        with pytest.raises(scope.EzlynxWriteScopeError, match="directory is group or world writable"):
            scope.register_filer_operation_scope()
    finally:
        tmp_path.chmod(0o755)


def test_a_file_not_owned_by_root_is_refused(policy_file, monkeypatch):
    policy_file()
    monkeypatch.setattr(scope, "_TRUSTED_OWNER_UID", os.getuid() + 1)
    with pytest.raises(scope.EzlynxWriteScopeError, match="not owned by root"):
        scope.register_filer_operation_scope()


def test_a_symlink_is_refused(policy_file, tmp_path, monkeypatch):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(policy_body()))
    real.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", link)
    with pytest.raises(scope.EzlynxWriteScopeError, match="symlinks are refused"):
        scope.register_filer_operation_scope()


def test_a_directory_in_place_of_the_file_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "ezlynx-write-scope.json"
    target.mkdir()
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", target)
    with pytest.raises(scope.EzlynxWriteScopeError, match="not a regular file"):
        scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER)


# ------------------------------------------------------------------ no environment variable opens it
@pytest.mark.parametrize(
    "name, value",
    [
        ("ROBIE_EZLYNX_WRITE_SCOPE", "all"),
        ("ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "*"),
        ("ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "999999999"),
        ("ROBIE_EZLYNX_OPERATION_SCOPE", "document_upload,note_append"),
        ("ROBIE_EZLYNX_WRITE_SCOPE_POLICY", "/tmp/anything.json"),
        ("ROBIE_PLAYGROUND", "1"),
    ],
)
def test_environment_variables_never_open_the_operation_scope(monkeypatch, tmp_path, name, value):
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", tmp_path / "absent.json")
    monkeypatch.setenv(name, value)
    assert scope.register_operation_scope(scope.ENTRYPOINT_ROBIE_FILER) is None
    assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False
    assert scope.operation_is_write_allowed(OTHER, NOTE) is False


# ------------------------------------------------------------------ agent interpreter and Chat jobs
def test_agent_interpreter_cannot_register(policy_file):
    policy_file()
    with mock.patch("robie_job_engine.safety_seal.agent_interpreter", return_value=True):
        with pytest.raises(scope.EzlynxWriteScopeError, match="agent interpreter"):
            scope.register_filer_operation_scope()
    assert scope.operation_scope_registered() is False


def test_agent_interpreter_never_gets_the_scope_even_if_a_value_is_planted(policy_file):
    policy_file()
    scope.register_filer_operation_scope()
    with mock.patch("robie_job_engine.safety_seal.agent_interpreter", return_value=True):
        assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False


def test_a_bound_production_chat_job_is_not_widened(policy_file):
    policy_file()
    scope.register_filer_operation_scope()
    with mock.patch.object(scope, "production_job_applicant", return_value="123456"):
        assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False
        assert scope.operation_is_write_allowed("123456", UPLOAD) is False  # falls to the normal bound-applicant rule
        assert scope.applicant_is_write_allowed("123456") is True
        with pytest.raises(scope.EzlynxWriteScopeError):
            scope.require_allowed_ezlynx_write_applicant(OTHER, operation=UPLOAD)


def test_seal_detects_a_scope_planted_after_it_was_installed():
    """Runs in a child: install_agent_seal adds a process-wide audit hook."""
    code = (
        "import os\n"
        "os.environ['ROBIE_ENV']='PRODUCTION'\n"
        "from robie_job_engine import safety_seal, ezlynx_write_scope as s\n"
        "safety_seal.install_agent_seal()\n"
        "try:\n"
        "    s.register_operation_scope('robie_filer')\n"
        "    print('REGISTERED')\n"
        "except s.EzlynxWriteScopeError as e:\n"
        "    print('REGISTER_REFUSED')\n"
        "s._OPERATION_SCOPE = ('robie_filer', frozenset({'document_upload','note_append'}))\n"
        "try:\n"
        "    s.require_allowed_ezlynx_write_applicant('999999999', operation='note_append')\n"
        "    print('WRITE_ALLOWED')\n"
        "except safety_seal.SafetySealError as e:\n"
        "    print('SEAL_TAMPERED' if 'SAFETY_SEAL_TAMPERED' in str(e) else 'OTHER')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert out.stdout.split() == ["REGISTER_REFUSED", "SEAL_TAMPERED"], out.stdout + out.stderr


def test_seal_detects_a_replaced_operation_check():
    code = (
        "from robie_job_engine import safety_seal, ezlynx_write_scope as s\n"
        "safety_seal.install_agent_seal()\n"
        "s.operation_is_write_allowed = lambda *a, **k: True\n"
        "try:\n"
        "    s.require_allowed_ezlynx_write_applicant('999999999', operation='note_append')\n"
        "    print('WRITE_ALLOWED')\n"
        "except safety_seal.SafetySealError as e:\n"
        "    print('SEAL_TAMPERED' if 'operation_is_write_allowed' in str(e) else 'OTHER')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert out.stdout.split() == ["SEAL_TAMPERED"], out.stdout + out.stderr


# ------------------------------------------------------------------ which call sites pass an operation
class _Stop(Exception):
    pass


def _recorder(calls):
    def record(applicant, *, operation=None):
        calls.append(operation)
        raise _Stop

    return record


def test_append_note_passes_the_note_operation(monkeypatch):
    from robie_job_engine import ezlynx_discussions as discussions

    calls = []
    monkeypatch.setattr(discussions, "require_allowed_ezlynx_write_applicant", _recorder(calls))
    client = discussions.DiscussionApiClient(mock.Mock(), urlopen=mock.Mock())
    with pytest.raises(_Stop):
        client.append_note("1", "hello", applicant_id=OTHER)
    assert calls == [NOTE]


def test_file_note_to_existing_discussion_passes_the_note_operation(monkeypatch):
    from robie_job_engine import ezlynx_discussions as discussions

    calls = []
    monkeypatch.setattr(discussions, "require_allowed_ezlynx_write_applicant", _recorder(calls))
    with pytest.raises(_Stop):
        discussions.file_note_to_existing_discussion(mock.Mock(), OTHER, "hello", dry_run=True)
    assert calls == [NOTE]


def test_creating_a_discussion_never_gets_an_operation(monkeypatch):
    from robie_job_engine import ezlynx_discussions as discussions

    calls = []
    monkeypatch.setattr(discussions, "require_allowed_ezlynx_write_applicant", _recorder(calls))
    creators = [
        fn for name, fn in vars(discussions).items()
        if name.startswith("create_discussion") and callable(fn)
    ]
    assert creators, "expected a create_discussion function"
    for fn in creators:
        import inspect

        params = inspect.signature(fn).parameters
        kwargs = {}
        args = []
        for pname, param in params.items():
            if param.default is not inspect.Parameter.empty or param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
                continue
            args.append(OTHER if "applicant" in pname else "A title" if "title" in pname else "body")
        with pytest.raises(_Stop):
            fn(*args, **kwargs)
    assert calls and all(op is None for op in calls)


def test_document_upload_passes_the_upload_operation(monkeypatch):
    from robie_job_engine import ezlynx_api as api

    calls = []
    monkeypatch.setattr(api, "require_allowed_ezlynx_write_applicant", _recorder(calls))
    client = api.EzlynxApiClient.__new__(api.EzlynxApiClient)
    with pytest.raises(_Stop):
        client.upload_applicant_document(OTHER, "doc.pdf", b"%PDF-1.4 x")
    assert calls == [UPLOAD]


def test_creating_a_policy_never_gets_an_operation(monkeypatch):
    from robie_job_engine import ezlynx_api as api

    calls = []
    monkeypatch.setattr(api, "require_allowed_ezlynx_write_applicant", _recorder(calls))
    client = api.EzlynxApiClient.__new__(api.EzlynxApiClient)
    import inspect

    fn = next(
        getattr(client, name) for name in dir(client)
        if name.startswith("create_policy") and callable(getattr(client, name))
    )
    kwargs = {
        name: (OTHER if "applicant" in name else "x")
        for name, p in inspect.signature(fn).parameters.items()
        if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    with pytest.raises(_Stop):
        fn(**kwargs)
    assert calls == [None]


def test_shared_write_workers_use_the_operation_scope(policy_file):
    from robie_job_engine import ezlynx_shared_writes as shared

    assert shared._scope_refusal(OTHER) is not None
    assert shared._scope_refusal(OTHER, UPLOAD) is not None
    policy_file()
    scope.register_filer_operation_scope()
    assert shared._scope_refusal(OTHER, UPLOAD) is None
    assert shared._scope_refusal(OTHER, NOTE) is None
    assert shared._scope_refusal(OTHER, None) is not None
    assert shared._scope_refusal(OTHER, "policy_create") is not None


# ------------------------------------------------------------------ the CLI
def _cli_args(**kw):
    import argparse

    return argparse.Namespace(**kw)


def test_cli_stays_on_the_allowlist_without_a_policy(tmp_path, monkeypatch):
    from robie_job_engine import ezlynx_api_cli as cli

    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", tmp_path / "absent.json")
    assert cli._guard_write_context() is None
    assert cli._policy_audit(None) == {}


def test_cli_registers_only_when_the_policy_lists_it(policy_file):
    from robie_job_engine import ezlynx_api_cli as cli

    policy_file(policy_body(entrypoints={"robie_filer": {"operations": [UPLOAD, NOTE]}}))
    assert cli._guard_write_context() is None
    assert scope.operation_is_write_allowed(OTHER, UPLOAD) is False
    policy_file(policy_body(entrypoints={"ezlynx_api_cli": {"operations": [UPLOAD, NOTE]}}))
    policy = cli._guard_write_context()
    assert policy["entrypoint"] == "ezlynx_api_cli"
    audit = cli._policy_audit(policy)
    assert len(audit["write_scope_policy_sha256"]) == 64
    assert audit["write_scope_operations"] == "document_upload,note_append"
    assert scope.operation_is_write_allowed(OTHER, NOTE) is True


def test_cli_refuses_writes_when_the_policy_file_is_damaged(policy_file):
    from robie_job_engine import ezlynx_api_cli as cli

    policy_file(mode=0o666)
    with pytest.raises(cli.CliRefused) as err:
        cli._guard_write_context()
    assert err.value.code == "write_scope_policy_refused"


def test_cli_still_rejects_the_all_clients_env_switch(monkeypatch, tmp_path):
    from robie_job_engine import ezlynx_api_cli as cli

    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    with pytest.raises(cli.CliRefused) as err:
        cli._guard_write_context()
    assert err.value.code == "write_scope_all_refused"


def test_cli_docs_upload_dry_run_follows_the_operation_scope(policy_file, tmp_path):
    from robie_job_engine import ezlynx_api_cli as cli

    f = tmp_path / "a.txt"
    f.write_text("hello")
    svc = mock.Mock()
    args = _cli_args(applicant=OTHER, name="a.txt", file=str(f), policy_master_id=None,
                     content_type=None, allow_duplicate=False, dry_run=True)
    with mock.patch.object(cli, "_read_regular_file", return_value=(f, b"hello")):
        with pytest.raises(cli.CliRefused) as err:
            cli.cmd_docs_upload(svc, args)
        assert err.value.code == "EZLYNX_WRITE_SCOPE_REFUSED"
        policy_file(policy_body(entrypoints={"ezlynx_api_cli": {"operations": [UPLOAD, NOTE]}}))
        outcome = cli.cmd_docs_upload(svc, args)
    assert outcome.status == "dry_run" and outcome.result["write_allowed"] is True
    assert outcome.audit["write_scope_policy_sha256"]


# ------------------------------------------------------------------ the filer
def test_filer_any_applicant_registers_from_the_policy(policy_file):
    from robie_job_engine import robie_filer as rf

    policy_file()
    rf._register_any_applicant_scope()
    assert scope.operation_is_write_allowed(OTHER, UPLOAD) is True
    assert scope.operation_is_write_allowed(OTHER, "policy_create") is False


def test_filer_any_applicant_refuses_a_damaged_policy(policy_file):
    from robie_job_engine import robie_filer as rf

    policy_file(mode=0o666)
    with pytest.raises(SystemExit, match="writable"):
        rf._register_any_applicant_scope()
    assert scope.operation_scope_registered() is False


# ------------------------------------------------------------------ install paths never install the policy
def test_no_installer_or_deploy_unit_installs_the_policy_file():
    needles = ("ezlynx-write-scope.json",)
    offenders = []
    for folder in ("scripts", "systemd", "deploy"):
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".json"} and "example" in path.name:
                continue
            if path.suffix not in {".sh", ".service", ".timer", ".conf", ".py", ".yml", ".yaml"}:
                continue
            text = path.read_text(errors="ignore")
            if any(n in text for n in needles):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


def test_the_example_policy_validates_and_is_not_a_live_file(tmp_path, monkeypatch):
    example = ROOT / "deploy" / "ezlynx-write-scope.example.json"
    target = tmp_path / "ezlynx-write-scope.json"
    target.write_bytes(example.read_bytes())
    target.chmod(0o644)
    tmp_path.chmod(0o755)
    monkeypatch.setattr(scope, "WRITE_SCOPE_POLICY_PATH", target)
    info = scope.register_filer_operation_scope()
    assert info["operations"] == frozenset({UPLOAD, NOTE})
    assert json.loads(example.read_text())["entrypoints"].keys() == {"robie_filer"}
