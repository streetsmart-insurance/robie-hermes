"""Immutable promotion contracts: synthetic files and metadata; no network."""
import copy
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from robie_job_engine.release_promotion import (
    REPOSITORY, bind_qa_to_install, unpack_bundle, validate_directory, validate_evidence, validate_provenance,
)

COMMIT = "a" * 40
BODY = b"synthetic unchanged release bytes"
DIGEST = hashlib.sha256(BODY).hexdigest()


def evidence():
    common = dict(commit=COMMIT, release_sha256=DIGEST, environment="Test", host="hermes-test-01")
    deploy = dict(common, production_touched=False, policy_setup_changed=False, previous_release="/test/prior-release",
                  verified_at="2026-10-01T23:00:00Z",
                  test_job_inventory={"blocking": 0, "rows": []},
                  official_install_proof={"done": True, "live": True, "authorizes_complete": False,
                                          "sha": COMMIT[:12], "proof": {"live": True}})
    qa = dict(common, passed=True, reviewer="independent-reviewer", verified_at="2026-10-02T00:00:00Z",
              applicant_ids=["26356199"], filing_enabled=False, client_writes_performed=False,
              driver_lease_clear=True, checks={name: "PASS" for name in (
                  "generation_restart", "stale_receipt", "concurrent_turn", "reply_recovery",
                  "service_account", "secrets", "browser", "job_db")})
    qa["installed_source"] = {"run_id": "10", "artifact_id": "20"}
    qa["check_evidence"] = {name: {"uri": f"https://evidence.example/runs/10/{name}.json",
                                          "sha256": "e" * 64} for name in qa["checks"]}
    return deploy, qa


def package():
    deploy, qa = evidence()
    name = f"robie-hermes-{COMMIT[:12]}.tgz"
    return {name: BODY, name + ".sha256": f"{DIGEST}  {name}\n".encode(),
            "test-deploy-evidence.json": json.dumps(deploy).encode(),
            "test-qa-evidence.json": json.dumps(qa).encode()}


def zipped(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return out.getvalue()


def test_promoted_archive_bytes_are_exactly_the_test_bytes(tmp_path):
    unpack_bundle(zipped(package()), tmp_path, COMMIT)
    archive = validate_directory(tmp_path, COMMIT, DIGEST)
    assert archive.read_bytes() == BODY
    archive.write_bytes(BODY + b"substitution")
    with pytest.raises(ValueError, match="bytes differ"):
        validate_directory(tmp_path, COMMIT, DIGEST)


@pytest.mark.parametrize("field,value", [
    ("commit", "b" * 40), ("release_sha256", "0" * 64), ("host", "hermes-poc-01"),
    ("passed", False), ("reviewer", ""), ("applicant_ids", ["220250093"]),
    ("filing_enabled", True), ("client_writes_performed", True), ("driver_lease_clear", False),
])
def test_mismatched_or_unsafe_qa_is_refused(field, value):
    deploy, qa = evidence()
    qa[field] = value
    with pytest.raises(ValueError):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


@pytest.mark.parametrize("check", list(evidence()[1]["checks"]))
def test_inconclusive_and_missing_checks_never_authorize_promotion(check):
    deploy, qa = evidence()
    qa["checks"][check] = "INCONCLUSIVE"
    with pytest.raises(ValueError, match="QA check not passed"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)
    del qa["checks"][check]
    with pytest.raises(ValueError, match="QA check not passed"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


@pytest.mark.parametrize("change", [
    {"previous_release": ""}, {"production_touched": True}, {"policy_setup_changed": True},
    {"test_job_inventory": {"blocking": 1, "rows": [{"id": "active"}]}},
    {"official_install_proof": {"done": True, "live": False}},
])
def test_missing_rollback_busy_jobs_or_pointer_only_proof_refuses(change):
    deploy, qa = evidence()
    deploy.update(change)
    with pytest.raises(ValueError):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


def provenance():
    run = dict(id=12, head_repository={"full_name": REPOSITORY}, event="workflow_dispatch",
               head_branch="main", path=".github/workflows/deploy-test.yml", status="completed",
               conclusion="success", head_sha=COMMIT)
    artifact = dict(id=34, workflow_run={"id": 12, "head_sha": COMMIT}, expired=False, size_in_bytes=1024,
                    name=f"verified-test-release-{COMMIT}")
    return run, artifact


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("head_branch", "feature"), ("head_sha", "b" * 40),
    ("conclusion", "failure"), ("path", ".github/workflows/ci.yml"),
    ("head_repository", {"full_name": "untrusted/fork"}),
])
def test_untrusted_or_wrong_run_is_refused(field, value):
    run, artifact = provenance()
    validate_provenance(run, artifact, "12", "34", COMMIT)
    run[field] = value
    with pytest.raises(ValueError):
        validate_provenance(run, artifact, "12", "34", COMMIT)


def test_other_run_or_expired_artifact_is_refused():
    run, artifact = provenance()
    for change in ({"expired": True}, {"workflow_run": {"id": 13, "head_sha": COMMIT}}):
        bad = copy.deepcopy(artifact)
        bad.update(change)
        with pytest.raises(ValueError):
            validate_provenance(run, bad, "12", "34", COMMIT)


def test_zip_traversal_or_extra_member_refuses_before_write(tmp_path):
    files = package()
    files["../escape"] = b"bad"
    with pytest.raises(ValueError, match="unexpected bundle members"):
        unpack_bundle(zipped(files), tmp_path, COMMIT)
    assert list(tmp_path.iterdir()) == []


def test_production_downloads_test_package_before_authentication_and_never_builds():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy-production.yml").read_text()
    primary = workflow.split("  lost-customer-retention:")[0]
    # The normal path never builds: build-release.sh may appear ONLY inside the
    # explicit override-gated step (Carlo override 2026-10-06).
    lines = primary.splitlines()
    for i, line in enumerate(lines):
        if "build-release.sh" in line:
            # Walk back to the enclosing step's `if:` guard.
            context = "\n".join(lines[max(0, i - 40):i + 1])
            assert "TEST GATE OVERRIDE" in context and "inputs.override_test_gate" in context, (
                "build-release.sh outside the override-gated step"
            )
    assert "release_promotion download" in primary
    assert primary.index("release_promotion download") < primary.index("google-github-actions/auth")
    assert "inputs.test_run_id" in primary and "inputs.test_artifact_id" in primary
    # The override requires its own distinct confirmation string.
    assert "DEPLOY_TO_HERMES_POC_01_OVERRIDE_TEST_GATE" in primary


def test_install_only_bundle_cannot_be_promoted_until_independent_qa(tmp_path):
    files = package()
    qa = files.pop("test-qa-evidence.json")
    unpack_bundle(zipped(files), tmp_path, COMMIT, require_qa=False)
    archive = validate_directory(tmp_path, COMMIT, DIGEST, require_qa=False)
    with pytest.raises(FileNotFoundError):
        validate_directory(tmp_path, COMMIT, DIGEST)
    (tmp_path / "test-qa-evidence.json").write_bytes(qa)
    assert validate_directory(tmp_path, COMMIT, DIGEST).read_bytes() == archive.read_bytes() == BODY


def test_installed_artifact_provenance_is_not_certified_artifact_provenance():
    run, artifact = provenance()
    artifact["name"] = f"installed-test-release-{COMMIT}"
    validate_provenance(run, artifact, "12", "34", COMMIT, require_qa=False)
    with pytest.raises(ValueError, match="wrong artifact name"):
        validate_provenance(run, artifact, "12", "34", COMMIT)


def test_certification_has_no_gcp_identity_and_does_not_redeploy():
    import yaml
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/deploy-test.yml").read_text())
    certify = workflow["jobs"]["certify-test"]
    assert certify["permissions"] == {"actions": "read", "contents": "read"}
    assert "refs/heads/main" in certify["if"]
    body = json.dumps(certify)
    assert "google-github-actions/auth" not in body
    assert "gcloud" not in body and "build-release.sh" not in body
    assert "authenticated workflow actor" in body
    assert "download-installed" in body and "release_promotion verify" in body


@pytest.mark.parametrize("bad_digest", [False, True])
def test_download_checks_api_provenance_and_zip_digest_before_unpack(tmp_path, monkeypatch, bad_digest):
    from robie_job_engine.release_promotion import download_verified_bundle
    run, artifact = provenance()
    blob = zipped(package())
    artifact["digest"] = "sha256:" + ("0" * 64 if bad_digest else hashlib.sha256(blob).hexdigest())
    endpoints = {
        f"repos/{REPOSITORY}/actions/runs/12": json.dumps(run).encode(),
        f"repos/{REPOSITORY}/actions/artifacts/34": json.dumps(artifact).encode(),
        f"repos/{REPOSITORY}/actions/artifacts/34/zip": blob,
    }
    calls = []
    def api(args):
        assert args[:2] == ["gh", "api"]
        calls.append(args[2])
        return endpoints[args[2]]
    monkeypatch.setattr("robie_job_engine.release_promotion.subprocess.check_output", api)
    if bad_digest:
        with pytest.raises(ValueError, match="ZIP digest mismatch"):
            download_verified_bundle(tmp_path, COMMIT, DIGEST, "12", "34")
        assert list(tmp_path.iterdir()) == []
    else:
        assert download_verified_bundle(tmp_path, COMMIT, DIGEST, "12", "34").read_bytes() == BODY
    assert calls == list(endpoints)


@pytest.mark.parametrize("value", [None, "garbage", "2026-10-02T00:00:00",
                                  "2026-10-01T23:00:00Z", "2026-10-01T22:59:59Z"])
def test_qa_requires_timezone_timestamp_after_installation(value):
    deploy, qa = evidence()
    qa["verified_at"] = value
    with pytest.raises(ValueError):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


def test_timezone_offsets_compare_as_instants():
    deploy, qa = evidence()
    qa["verified_at"] = "2026-10-02T02:00:00+03:00"
    with pytest.raises(ValueError, match="follow installation"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)
    qa["verified_at"] = "2026-10-02T02:00:01+03:00"
    validate_evidence(deploy, qa, COMMIT, DIGEST)


@pytest.mark.parametrize("value", [None, {}, {"uri": "file:///tmp/proof", "sha256": "a" * 64},
    {"uri": "https://evidence.example/proof", "sha256": "not-a-digest"},
    {"uri": "https://user:password@evidence.example/proof", "sha256": "a" * 64}])
def test_pass_without_durable_evidence_reference_and_digest_refuses(value):
    deploy, qa = evidence()
    qa["check_evidence"]["generation_restart"] = value
    with pytest.raises(ValueError):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


def test_certification_binds_actual_installed_download_and_actor():
    _, qa = evidence()
    bound = bind_qa_to_install(qa, "10", "20", qa["reviewer"])
    assert bound["installed_source"] == {"run_id": "10", "artifact_id": "20"}
    assert bound is not qa
    for run_id, artifact_id, actor in (("11", "20", qa["reviewer"]),
                                      ("10", "21", qa["reviewer"]),
                                      ("10", "20", "different-reviewer")):
        with pytest.raises(ValueError):
            bind_qa_to_install(qa, run_id, artifact_id, actor)


def test_qa_without_original_installed_source_refuses():
    deploy, qa = evidence()
    del qa["installed_source"]
    with pytest.raises(ValueError, match="installed source"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


def test_certification_binds_only_after_download():
    root = Path(__file__).resolve().parents[1]
    certify = (root / ".github/workflows/deploy-test.yml").read_text().split("  certify-test:", 1)[1]
    assert certify.index("release_promotion download-installed") < certify.index("qa = bind_qa_to_install")
    assert 'os.environ["TEST_RUN_ID"]' in certify and 'os.environ["TEST_ARTIFACT_ID"]' in certify


@pytest.mark.parametrize("check", list(evidence()[1]["checks"]))
def test_every_passing_check_requires_its_own_evidence(check):
    deploy, qa = evidence()
    del qa["check_evidence"][check]
    with pytest.raises(ValueError, match="QA evidence"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)


@pytest.mark.parametrize("value", [None, "garbage", "2026-10-01T23:00:00"])
def test_installation_timestamp_must_be_authoritative_and_zoned(value):
    deploy, qa = evidence()
    deploy["verified_at"] = value
    with pytest.raises(ValueError, match="installation timestamp"):
        validate_evidence(deploy, qa, COMMIT, DIGEST)
