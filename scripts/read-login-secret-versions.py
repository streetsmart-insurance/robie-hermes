#!/usr/bin/env python3
"""Resolve pinned EZLynx Secret Manager version resource names on the VM.

Runs ON hermes-poc-01 with the engine venv. The VM service account (not the
GitHub deployer identity) holds secretmanager.versions.list, so version
STATES are resolved where the grants live. Lists versions only -- never
reads or prints payloads -- and prints:

    ROBIE_EZLYNX_USERNAME_SECRET=projects/<project>/secrets/ezlynx-username/versions/<n>
    ROBIE_EZLYNX_PASSWORD_SECRET=projects/<project>/secrets/ezlynx-password/versions/<n>

for the newest ENABLED version of each secret (never versions/latest, which
may be a DESTROYED leftover). Exit 2 (fail closed) when Secret Manager is
unavailable, a secret has no ENABLED version, or anything is unexpected.
"""

from __future__ import annotations

import argparse
import re
import sys

DEFAULT_PROJECT = "streetsmart-hermes-poc"

# Secret Manager secret id -> environment variable the engine reads.
ENV_MAP = {
    "ezlynx-username": "ROBIE_EZLYNX_USERNAME_SECRET",
    "ezlynx-password": "ROBIE_EZLYNX_PASSWORD_SECRET",
}

# The engine's ezlynx_login_bootstrap.secret() only accepts a pinned
# reference of the form .../versions/[1-9][0-9]*.
_VERSION_RE = re.compile(r"[1-9]\d*")


def fail(message: str) -> int:
    print(f"read-login-secret-versions: refusing: {message}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print pinned newest-ENABLED Secret Manager version "
        "resource names (states only, never payloads)."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()

    try:
        from robie_job_engine.login_secret_health import inspect_login_secrets
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"cannot import engine version inspector: "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        report = inspect_login_secrets(project=args.project)
    except Exception as exc:  # noqa: BLE001
        return fail(f"version listing failed: {type(exc).__name__}: {exc}")

    if not isinstance(report, dict):
        return fail("version inspector returned an unexpected report")
    result = report.get("result")
    if result == "UNKNOWN":
        return fail(f"secret manager unavailable: {report.get('reason')}")
    if result != "OK":
        return fail(f"version health is {result}: {report.get('reason')}")

    secrets = report.get("secrets")
    if not isinstance(secrets, list):
        return fail("version report has no secret list")
    by_id = {}
    for item in secrets:
        if isinstance(item, dict) and item.get("secret_id"):
            by_id[item["secret_id"]] = item

    lines = []
    for secret_id, env_var in ENV_MAP.items():
        item = by_id.get(secret_id)
        version = (item or {}).get("newest_enabled_version")
        number = None
        # The engine reports versions as "versions/<n>".
        if isinstance(version, str) and version.startswith("versions/"):
            number = version.split("/", 1)[1]
        if not number or not _VERSION_RE.fullmatch(number):
            return fail(f"no usable ENABLED version for {secret_id}")
        resource = (
            f"projects/{args.project}/secrets/{secret_id}/versions/{number}"
        )
        lines.append(f"{env_var}={resource}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
