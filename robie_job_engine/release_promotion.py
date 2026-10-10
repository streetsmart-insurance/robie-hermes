"""Validate a retained Test package before Production can use its exact bytes.

No cloud credentials, deployment actions, or archive extraction live here.
The protected controller owns download provenance; independent Test QA owns
the attestation. A successful installer alone cannot manufacture QA approval.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import zipfile
from urllib.parse import urlparse

REPOSITORY = "streetsmart-insurance/robie-hermes"
MAX_BYTES = 128 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def evidence_timestamp(value: object, label: str) -> datetime:
    require(isinstance(value, str), f"{label} timestamp missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp invalid") from exc
    require(result.tzinfo is not None and result.utcoffset() is not None,
            f"{label} timestamp requires timezone")
    return result


def bind_qa_to_install(qa: dict, run_id: str, artifact_id: str, reviewer: str) -> dict:
    """Called only after download-installed verifies these immutable IDs.

    The workflow persists the binding in QA; this does not authenticate the
    evidence URLs or establish that the actor is an independent reviewer.
    """
    require(run_id.isdecimal() and artifact_id.isdecimal(), "numeric installed IDs required")
    require(bool(reviewer) and qa.get("reviewer") == reviewer,
            "QA reviewer must be the authenticated workflow actor")
    expected = {"run_id": run_id, "artifact_id": artifact_id}
    require(qa.get("installed_source") == expected, "QA installed source differs from downloaded package")
    return {**qa, "installed_source": expected}


def validate_install(deploy: dict, commit: str, digest: str) -> None:
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "invalid source commit")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "invalid release digest")
    for evidence in (deploy,):
        require(evidence.get("commit") == commit, "evidence source mismatch")
        require(evidence.get("release_sha256") == digest, "evidence digest mismatch")
        require(evidence.get("environment") == "Test", "Test evidence required")
        require(evidence.get("host") == "hermes-test-01", "wrong Test host")
    require(deploy.get("production_touched") is False, "Test touched Production")
    require(deploy.get("policy_setup_changed") is False, "Chat release changed policy setup")
    require(bool(deploy.get("previous_release")), "rollback evidence missing")
    evidence_timestamp(deploy.get("verified_at"), "installation")
    inventory = deploy.get("test_job_inventory") or {}
    require(inventory.get("blocking") == 0 and inventory.get("rows") == [], "Test jobs not clear")
    require(inventory.get("database") != "missing", "Test job database missing")
    proof = deploy.get("official_install_proof") or {}
    require(proof.get("done") is True and proof.get("live") is True, "Test install unverified")
    require(proof.get("authorizes_complete") is False, "install proof cannot authorize jobs")
    require(proof.get("sha") == commit[:12] and proof.get("proof", {}).get("live") is True,
            "install proof source or runtime mismatch")


def validate_evidence(deploy: dict, qa: dict, commit: str, digest: str) -> None:
    validate_install(deploy, commit, digest)
    require(qa.get("commit") == commit and qa.get("release_sha256") == digest, "QA package mismatch")
    require(qa.get("environment") == "Test" and qa.get("host") == "hermes-test-01", "wrong QA target")
    require(qa.get("passed") is True, "independent Test QA not passed")
    require(bool(qa.get("reviewer")), "QA provenance missing")
    qa_time = evidence_timestamp(qa.get("verified_at"), "QA")
    install_time = evidence_timestamp(deploy.get("verified_at"), "installation")
    require(qa_time > install_time, "QA must follow installation")
    source = qa.get("installed_source") or {}
    require(isinstance(source, dict) and set(source) == {"run_id", "artifact_id"}
            and all(isinstance(value, str) and value.isdecimal() for value in source.values()),
            "QA installed source missing or invalid")
    require(qa.get("applicant_ids") == ["26356199"], "Test applicant scope differs")
    require(qa.get("filing_enabled") is False, "filing must stay disabled")
    require(qa.get("client_writes_performed") is False, "this release requires no-client-write QA")
    require(qa.get("driver_lease_clear") is True, "driver coordination unverified")
    checks = qa.get("checks") or {}
    for name in ("generation_restart", "stale_receipt", "concurrent_turn", "reply_recovery",
                 "service_account", "secrets", "browser", "job_db"):
        require(checks.get(name) == "PASS", f"QA check not passed: {name}")
        evidence = (qa.get("check_evidence") or {}).get(name) or {}
        require(isinstance(evidence, dict), f"QA evidence invalid: {name}")
        uri = evidence.get("uri")
        require(isinstance(uri, str) and not any(char.isspace() for char in uri),
                f"QA evidence URI missing: {name}")
        parsed = urlparse(uri)
        require(parsed.scheme in ("https", "gs") and bool(parsed.netloc)
                and bool(parsed.path.strip("/")) and parsed.username is None
                and parsed.password is None, f"QA evidence URI must be durable: {name}")
        require(isinstance(evidence.get("sha256"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])),
                f"QA evidence digest missing: {name}")
        # Each check is run for THIS release: evidence from an earlier release
        # (same host, same infra) is not proof for these bytes.
        require(evidence.get("commit") == commit,
                f"QA evidence is not for this release commit: {name}")
        captured = evidence_timestamp(evidence.get("captured_at"), f"QA evidence {name}")
        require(install_time < captured <= qa_time,
                f"QA evidence must be captured after this Test install and before QA sign-off: {name}")


def validate_directory(directory: Path, commit: str, digest: str, *, require_qa: bool = True) -> Path:
    archive = directory / f"robie-hermes-{commit[:12]}.tgz"
    require(archive.is_file() and not archive.is_symlink(), "release archive missing")
    require(archive.stat().st_size <= MAX_BYTES, "release archive too large")
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == digest, "release bytes differ")
    checksum = archive.with_name(archive.name + ".sha256")
    require(checksum.read_text().split() == [digest, archive.name], "checksum file differs")
    deploy = json.loads((directory / "test-deploy-evidence.json").read_text())
    if require_qa:
        qa = json.loads((directory / "test-qa-evidence.json").read_text())
        validate_evidence(deploy, qa, commit, digest)
    else:
        validate_install(deploy, commit, digest)
    return archive


def validate_provenance(run: dict, artifact: dict, run_id: str, artifact_id: str, commit: str,
                        *, require_qa: bool = True) -> None:
    require(str(run.get("id")) == run_id, "run identity mismatch")
    require(str(artifact.get("id")) == artifact_id, "artifact identity mismatch")
    require(run.get("head_repository", {}).get("full_name") == REPOSITORY, "wrong source repository")
    require(run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main",
            "protected main Test dispatch required")
    require(run.get("path", "").split("@")[0] == ".github/workflows/deploy-test.yml", "wrong Test workflow")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "Test run not successful")
    require(run.get("head_sha") == commit, "Test run source mismatch")
    require(artifact.get("workflow_run", {}).get("id") == run.get("id"), "artifact belongs to another run")
    require(artifact.get("workflow_run", {}).get("head_sha") == commit, "artifact source mismatch")
    require(artifact.get("expired") is False, "Test artifact expired")
    prefix = "verified-test-release" if require_qa else "installed-test-release"
    require(artifact.get("name") == f"{prefix}-{commit}", "wrong artifact name")
    require(isinstance(artifact.get("size_in_bytes"), int) and 0 < artifact["size_in_bytes"] <= MAX_BYTES,
            "invalid artifact size")


def unpack_bundle(blob: bytes, directory: Path, commit: str, *, require_qa: bool = True) -> None:
    import io

    archive = f"robie-hermes-{commit[:12]}.tgz"
    expected = {archive, archive + ".sha256", "test-deploy-evidence.json"}
    if require_qa:
        expected.add("test-qa-evidence.json")
    require(len(blob) <= MAX_BYTES, "bundle too large")
    with zipfile.ZipFile(io.BytesIO(blob)) as bundle:
        members = bundle.infolist()
        require(len(members) == len(expected) and {m.filename for m in members} == expected, "unexpected bundle members")
        require(sum(m.file_size for m in members) <= MAX_BYTES, "expanded bundle too large")
        for member in members:
            require(not member.is_dir() and stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG),
                    "bundle member is not a regular file")
        directory.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = directory / member.filename
            require(not target.exists() and not target.is_symlink(), "bundle destination exists")
            with target.open("xb") as stream:
                stream.write(bundle.read(member))


def download_verified_bundle(directory: Path, commit: str, digest: str, run_id: str, artifact_id: str,
                             *, require_qa: bool = True) -> Path:
    require(run_id.isdecimal() and artifact_id.isdecimal(), "numeric immutable IDs required")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "invalid source commit")

    def api(path: str) -> bytes:
        return subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/{path}"])

    run = json.loads(api(f"actions/runs/{run_id}"))
    artifact = json.loads(api(f"actions/artifacts/{artifact_id}"))
    validate_provenance(run, artifact, run_id, artifact_id, commit, require_qa=require_qa)
    blob = api(f"actions/artifacts/{artifact_id}/zip")
    expected = artifact.get("digest", "")
    require(expected == "sha256:" + hashlib.sha256(blob).hexdigest(), "artifact ZIP digest mismatch")
    unpack_bundle(blob, directory, commit, require_qa=require_qa)
    return validate_directory(directory, commit, digest, require_qa=require_qa)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify", "download", "verify-installed", "download-installed"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-id")
    args = parser.parse_args()
    require_qa = not args.mode.endswith("-installed")
    if args.mode.startswith("download"):
        require(bool(args.run_id and args.artifact_id), "Test run/artifact IDs required")
        archive = download_verified_bundle(args.directory, args.commit, args.sha256, args.run_id, args.artifact_id,
                                           require_qa=require_qa)
    else:
        archive = validate_directory(args.directory, args.commit, args.sha256, require_qa=require_qa)
    print(json.dumps({"commit": args.commit, "release_sha256": args.sha256, "archive": str(archive)}))


if __name__ == "__main__":
    main()
