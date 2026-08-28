"""PR + post-deploy regression battery. Logic tests + known Test replay.

Detection is automatic. Merge, Production flip, and hermes-gateway restart
stay human-gated (Jake Approve / Carlo Confirm). GitHub-hosted runners never
drive live EZLynx. Replay refuses Production env and live Hermes job-db paths.

A NEW fail (not a known-accepted Test replay outcome) posts to the Robie
Chat space as the Chat APP and may open a draft PR. It does not @robie,
bind, email the insured, merge, or deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from .quote_replay import (
    is_live_hermes_path,
    refuse_production_targets,
    run_quote_replay,
)
from .runtime_env import PRODUCTION_ENV_NAMES, ProductionGuardError, current_robie_env


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHAT_SPACE = "spaces/AAQAZbLJO78"
HUMAN_GATE = (
    "Human gate: Jake Approve (StreetSmartJake) and Carlo Confirm. "
    "Do not merge from this hook. Do not flip Production. "
    "Do not restart hermes-gateway."
)
ISOLATED_UNSET = (
    "ROBIE_ENV",
    "ROBIE_JOB_DB",
    "ROBIE_ARTIFACT_ROOT",
    "ROBIE_BROWSER_CDP_URL",
    "ROBIE_QUOTE_PDF",
)
LIVE_PRODUCTION_JOB_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
LIVE_PRODUCTION_ARTIFACTS = "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
TEST_JOB_DB = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db"
TEST_ARTIFACTS = "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts"

LOGIC_JOB_TYPE_GATE = (
    "scripts/check-job-type-gate.py",
    "check",
)
LOGIC_UNITTEST = ("-m", "unittest", "discover", "-s", "tests", "-v")
LOGIC_PYTEST_MODULES = (
    "tests/test_carrier_directory.py",
    "tests/test_ezlynx_poller.py",
    "tests/test_video_to_skill.py",
)
PYTEST_ONLY_MODULES = frozenset(
    Path(item).stem for item in LOGIC_PYTEST_MODULES
)

# Replay outcomes that are accepted on Test / isolated runners. A different
# outcome for the same id is a NEW fail and must Chat + draft.
KNOWN_ACCEPTED_REPLAY = {
    "quote-replay:missing-pdf": frozenset({"BLOCKED"}),
    "quote-replay:production-env": frozenset({"REFUSED"}),
    "quote-replay:live-hermes-job-db": frozenset({"REFUSED"}),
    "quote-replay:test-paths-require-test-env": frozenset({"REFUSED"}),
}


def _chat_space() -> str:
    for key in (
        "ROBIE_REGRESSION_CHAT_SPACE",
        "ROBIE_PREFLIGHT_CHAT_SPACE",
        "ROBIE_OPS_CHAT_SPACE",
        "GOOGLE_CHAT_HOME_CHANNEL",
    ):
        value = os.environ.get(key, "").strip()
        if value.startswith("spaces/"):
            return value
    return DEFAULT_CHAT_SPACE


def default_work_dir() -> Path:
    raw = os.environ.get("ROBIE_REGRESSION_WORK_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    candidate = REPO_ROOT / ".robie-durable-test" / "regression-battery"
    if is_live_hermes_path(candidate):
        return Path("/var/lib/robie-regression-battery")
    return candidate


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Drop Production job-db / env so the suite cannot inherit live paths."""
    env = dict(base if base is not None else os.environ)
    for key in ISOLATED_UNSET:
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        (":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else ""
    )
    return env


def logic_job_type_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, str(REPO_ROOT / LOGIC_JOB_TYPE_GATE[0]), LOGIC_JOB_TYPE_GATE[1]]


def logic_unittest_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, *LOGIC_UNITTEST]


def logic_pytest_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, "-m", "pytest", *LOGIC_PYTEST_MODULES, "-q"]


def pytest_available() -> bool:
    return shutil.which("pytest") is not None or _can_import_pytest()


def _can_import_pytest() -> bool:
    try:
        import pytest  # noqa: F401

        return True
    except ImportError:
        return False


def _run_step(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
    name: str,
) -> dict[str, Any]:
    proc = runner(
        argv,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + ("\n" if proc.stderr else "") + (proc.stderr or "")
    ok = int(proc.returncode) == 0
    return {
        "id": name,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "returncode": int(proc.returncode),
        "evidence": output[-4000:],
    }


def discover_unittest_suite(
    tests_dir: Path,
    *,
    include_pytest_modules: bool,
    top_level_dir: Path | None = None,
) -> Any:
    """Load test_*.py. Skip pytest-only modules when pytest is not installed."""
    import unittest

    tests_dir = Path(tests_dir)
    for entry in (str(tests_dir), str(tests_dir.parent)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    extra: dict[str, str] = {}
    if top_level_dir is not None:
        extra["top_level_dir"] = str(top_level_dir)
    for path in sorted(tests_dir.glob("test_*.py")):
        if not include_pytest_modules and path.stem in PYTEST_ONLY_MODULES:
            continue
        suite.addTests(
            loader.discover(str(tests_dir), pattern=path.name, **extra)
        )
    return suite


def run_unittest_skipping_pytest(*, repo_root: Path | None = None) -> int:
    import unittest

    root = Path(repo_root or REPO_ROOT)
    tests_dir = root / "tests"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    suite = discover_unittest_suite(tests_dir, include_pytest_modules=False)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_logic_suite(
    *,
    repo_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    python: str | None = None,
    include_pytest: bool | None = None,
) -> list[dict[str, Any]]:
    """Job-type gate + unittest discover + Phase 3 pytest (if present)."""
    root = Path(repo_root or REPO_ROOT)
    run = runner or subprocess.run
    env = isolated_env()
    env["PYTHONPATH"] = str(root) + (
        (":" + os.environ["PYTHONPATH"]) if os.environ.get("PYTHONPATH") else ""
    )
    want_pytest = pytest_available() if include_pytest is None else include_pytest
    completed = [
        _run_step(
            logic_job_type_argv(python),
            cwd=root,
            env=env,
            runner=run,
            name="job-type-gate",
        )
    ]
    if want_pytest:
        completed.append(
            _run_step(
                logic_unittest_argv(python),
                cwd=root,
                env=env,
                runner=run,
                name="unittest-discover",
            )
        )
        completed.append(
            _run_step(
                logic_pytest_argv(python),
                cwd=root,
                env=env,
                runner=run,
                name="pytest-phase3",
            )
        )
        return completed
    # Production / verify hosts may lack pytest. Do not import those modules.
    exe = python or sys.executable
    skip_argv = [
        exe,
        "-c",
        "from robie_job_engine.regression_battery import run_unittest_skipping_pytest; "
        "raise SystemExit(run_unittest_skipping_pytest())",
    ]
    completed.append(
        _run_step(
            skip_argv,
            cwd=root,
            env=env,
            runner=run,
            name="unittest-discover",
        )
    )
    return completed


def _refuse_outcome(exc: BaseException) -> str:
    if isinstance(exc, ProductionGuardError):
        return "REFUSED"
    return f"ERROR:{type(exc).__name__}"


def run_replay_scenarios(
    *,
    work_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """In-process Test replay / known-failure scenarios. No live EZLynx."""
    root = Path(work_dir or default_work_dir())
    if is_live_hermes_path(root):
        raise ProductionGuardError(
            f"refusing regression replay work dir on live Hermes path: {root}"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(root / "jobs.db")
    artifacts = str(root / "artifacts")
    results: list[dict[str, Any]] = []

    with patch.dict(os.environ, {}, clear=False):
        for key in ISOLATED_UNSET:
            os.environ.pop(key, None)
        missing = run_quote_replay(
            quote_pdf=None, db_path=db, artifact_root=artifacts
        )
    results.append(
        {
            "id": "quote-replay:missing-pdf",
            "kind": "replay",
            "ok": missing.get("outcome") == "BLOCKED",
            "outcome": str(missing.get("outcome") or "UNKNOWN"),
            "evidence": str(missing.get("reason") or ""),
        }
    )

    def _guard(scenario_id: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            outcome = "UNEXPECTED"
            evidence = "guard did not refuse"
        except ProductionGuardError as exc:
            outcome = "REFUSED"
            evidence = str(exc)
        except Exception as exc:  # noqa: BLE001 — classify, do not swallow into PASS
            outcome = _refuse_outcome(exc)
            evidence = str(exc)
        results.append(
            {
                "id": scenario_id,
                "kind": "replay",
                "ok": outcome in KNOWN_ACCEPTED_REPLAY.get(scenario_id, ()),
                "outcome": outcome,
                "evidence": evidence,
            }
        )

    def production_env() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            refuse_production_targets(db, artifacts)

    def live_hermes_job_db() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            refuse_production_targets(LIVE_PRODUCTION_JOB_DB, artifacts)

    def test_paths_require_test_env() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            refuse_production_targets(TEST_JOB_DB, TEST_ARTIFACTS)

    _guard("quote-replay:production-env", production_env)
    _guard("quote-replay:live-hermes-job-db", live_hermes_job_db)
    _guard("quote-replay:test-paths-require-test-env", test_paths_require_test_env)
    return results


def classify_results(
    results: list[dict[str, Any]],
    *,
    known_accepted: dict[str, frozenset[str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    accepted = known_accepted if known_accepted is not None else KNOWN_ACCEPTED_REPLAY
    new_failures: list[dict[str, Any]] = []
    known: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for item in results:
        outcome = str(item.get("outcome") or "")
        allowed = accepted.get(str(item.get("id") or ""), frozenset())
        if item.get("ok") and (not allowed or outcome in allowed):
            passed.append(item)
            continue
        if outcome in allowed:
            known.append(item)
            continue
        new_failures.append(item)
    return {
        "new_failures": new_failures,
        "known_accepted": known,
        "passed": passed,
    }


def format_new_failure_chat(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
) -> str:
    """Short Chat APP post. Never @robie."""
    lines = [
        "ROBIE regression battery — NEW fail",
        f"trigger: {trigger}",
    ]
    for item in new_failures:
        evidence = str(item.get("evidence") or "no evidence").strip().splitlines()
        snippet = evidence[0] if evidence else "no evidence"
        lines.append(
            f"- {item.get('id')}: {item.get('outcome')} ({snippet[:240]})"
        )
    lines.append(HUMAN_GATE)
    text = "\n".join(lines)
    if "@robie" in text.casefold():
        raise ValueError("regression Chat must not @robie")
    return text


def _post_new_failure(
    text: str,
    *,
    poster: Callable[..., Any] | None,
) -> bool:
    if "@robie" in text.casefold():
        raise ValueError("regression Chat must not @robie")
    space = _chat_space()
    if not space.startswith("spaces/"):
        return False
    send = poster
    if send is None:
        from .chat_app_post import post_as_chat_app

        send = post_as_chat_app
    send(space, text)
    return True


def build_draft_pr_argv(*, title: str, body: str) -> list[str]:
    """gh pr create --draft only. Never merge. Never deploy."""
    return [
        "gh",
        "pr",
        "create",
        "--draft",
        "--title",
        title,
        "--body",
        body,
    ]


def assert_draft_argv_safe(argv: list[str]) -> list[str]:
    """Inspect command tokens only. Title/body may mention merge as a human gate."""
    if list(argv[:3]) != ["gh", "pr", "create"]:
        raise ValueError("draft PR hook must use gh pr create")
    if "--draft" not in argv:
        raise ValueError("draft PR hook must pass --draft")
    skip_value = False
    command_tokens: list[str] = []
    for index, part in enumerate(argv):
        if skip_value:
            skip_value = False
            continue
        if part in {"--title", "--body", "--head", "--base", "--label"}:
            skip_value = True
            command_tokens.append(part)
            continue
        if index >= 3 and not str(part).startswith("-"):
            continue
        command_tokens.append(part)
    folded = " ".join(command_tokens).casefold()
    if "--merge" in command_tokens or " merge " in f" {folded} ":
        raise ValueError("draft PR hook cannot merge")
    for flag in command_tokens:
        if not str(flag).startswith("-"):
            continue
        text = flag.casefold()
        if any(token in text for token in ("merge", "deploy", "restart")):
            raise ValueError(f"draft PR hook forbids flag {flag}")
    return argv


def draft_pr_payload(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
) -> dict[str, str]:
    title = "ROBIE regression: NEW fail (do not merge)"
    body = (
        format_new_failure_chat(new_failures, trigger=trigger)
        + "\n\nThis draft is detection only. "
        + HUMAN_GATE
    )
    if "@robie" in body.casefold():
        raise ValueError("draft PR body must not @robie")
    return {"title": title, "body": body, "draft": "true"}


def open_draft_fix_pr(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
    opener: Callable[[dict[str, str]], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    payload = draft_pr_payload(new_failures, trigger=trigger)
    if opener is not None:
        result = opener(payload)
        return {"opened": True, "draft": True, "payload": payload, "result": result}
    argv = assert_draft_argv_safe(
        build_draft_pr_argv(title=payload["title"], body=payload["body"])
    )
    if shutil.which("gh") is None:
        return {
            "opened": False,
            "draft": True,
            "skipped": "gh not available",
            "argv": argv,
            "payload": payload,
        }
    run = runner or subprocess.run
    proc = run(argv, check=False, capture_output=True, text=True)
    return {
        "opened": int(proc.returncode) == 0,
        "draft": True,
        "argv": argv,
        "payload": payload,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def run_regression_battery(
    *,
    trigger: str = "ci",
    notify: bool = False,
    draft_pr: bool = False,
    logic_runner: Callable[[], list[dict[str, Any]]] | None = None,
    replay_runner: Callable[[], list[dict[str, Any]]] | None = None,
    poster: Callable[..., Any] | None = None,
    pr_opener: Callable[[dict[str, str]], Any] | None = None,
    known_accepted: dict[str, frozenset[str]] | None = None,
    include_pytest: bool | None = None,
) -> dict[str, Any]:
    """Run logic suite + replay catalog. Chat/draft only on NEW fails."""
    if current_robie_env() in PRODUCTION_ENV_NAMES and trigger == "replay-live":
        raise ProductionGuardError("refusing live replay: ROBIE_ENV is Production")
    logic = list(logic_runner() if logic_runner is not None else run_logic_suite(
        include_pytest=include_pytest
    ))
    replay = list(
        replay_runner() if replay_runner is not None else run_replay_scenarios()
    )
    classified = classify_results(
        logic + replay, known_accepted=known_accepted
    )
    new_failures = classified["new_failures"]
    message = (
        format_new_failure_chat(new_failures, trigger=trigger)
        if new_failures
        else None
    )
    posted = False
    post_error = None
    draft: dict[str, Any] | None = None
    if new_failures and notify:
        try:
            posted = _post_new_failure(message or "", poster=poster)
        except Exception as exc:
            post_error = f"{type(exc).__name__}: {exc}"
        if draft_pr:
            try:
                draft = open_draft_fix_pr(
                    new_failures, trigger=trigger, opener=pr_opener
                )
            except Exception as exc:
                draft = {
                    "opened": False,
                    "draft": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return {
        "ok": not new_failures,
        "trigger": trigger,
        "results": logic + replay,
        "new_failures": new_failures,
        "known_accepted": classified["known_accepted"],
        "passed": classified["passed"],
        "chat_posted": posted,
        "message": message,
        "chat_post_error": post_error,
        "draft_pr": draft,
        "human_gate": HUMAN_GATE,
    }


def maybe_detach(argv: list[str]) -> None:
    """Double-fork so ExecStartPost does not block hermes-gateway."""
    if "--detach" not in argv and os.environ.get("ROBIE_REGRESSION_DETACH") != "1":
        return
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROBIE regression battery. Logic tests + Test replay catalog. "
            "NEW fails post to Chat without @robie and may open a draft PR. "
            "Does not merge or deploy."
        )
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run the PR battery and exit non-zero on NEW fails. No Chat.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Chat + draft-PR hook on NEW fails (post-deploy).",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Double-fork before running so systemd ExecStartPost returns.",
    )
    parser.add_argument("--trigger", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw = list(argv if argv is not None else sys.argv[1:])
    maybe_detach(raw)
    trigger = args.trigger or ("post-deploy" if args.notify else "ci")
    report = run_regression_battery(
        trigger=trigger,
        notify=bool(args.notify),
        draft_pr=bool(args.notify),
    )
    print(json.dumps(
        {
            "ok": report["ok"],
            "trigger": report["trigger"],
            "new_failures": [
                {"id": item.get("id"), "outcome": item.get("outcome")}
                for item in report["new_failures"]
            ],
            "known_accepted": [
                {"id": item.get("id"), "outcome": item.get("outcome")}
                for item in report["known_accepted"]
            ],
            "chat_posted": report["chat_posted"],
            "draft_pr_opened": bool((report.get("draft_pr") or {}).get("opened")),
        },
        sort_keys=True,
    ))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
