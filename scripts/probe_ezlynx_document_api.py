#!/usr/bin/env python3
"""Read-only probe of the EZLynx Documents API.

Runs on hermes-poc-01 (which has real internet access and the VM service
account). It:

  1. Loads the EZLynx API config for the requested environment from
     Secret Manager (UAT or Production credentials).
  2. Acquires an OAuth2 token (vendor_data_access grant).
  3. Issues read-only GETs against candidate DocumentApi paths and
     reports HTTP status codes.

No documents are downloaded, nothing is written, and no secret values
are printed. Output is a JSON summary for the workflow evidence artifact.

Usage:
  sudo python3 probe_ezlynx_document_api.py <uat|prod> <secret-resource-name> [engine-dir]

The engine dir defaults to /opt/streetsmart-hermes/robie-job-engine; the
probe workflow passes a staging dir holding the PR's engine code so the
probe validates the candidate code before it is deployed.
"""

from __future__ import annotations

import json
import os
import sys
import time

ENGINE_DIR = (
    sys.argv[3]
    if len(sys.argv) > 3
    else "/opt/streetsmart-hermes/robie-job-engine"
)
sys.path.insert(0, ENGINE_DIR)

from robie_job_engine.ezlynx_api import (  # noqa: E402
    ENV_PROD_SECRET,
    ENV_UAT_SECRET,
    EzlynxApiClient,
    EzlynxApiError,
    load_ezlynx_api_config,
)

CANDIDATE_PATHS = [
    "documents",
    "document",
    "v1/documents",
    "api/documents",
    "Documents",
    "documents/list",
    "documents/search",
]


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"uat", "prod"}:
        print(
            "usage: probe_ezlynx_document_api.py <uat|prod> <secret-resource-name> [engine-dir]",
            file=sys.stderr,
        )
        return 2
    if argv[1] == "uat":
        os.environ["ROBIE_ENV"] = "TEST"
        os.environ[ENV_UAT_SECRET] = argv[2]
    else:
        os.environ["ROBIE_ENV"] = "PRODUCTION"
        os.environ[ENV_PROD_SECRET] = argv[2]

    summary: dict = {
        "environment": os.environ["ROBIE_ENV"],
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine_dir": ENGINE_DIR,
        "auth": {"ok": False},
        "paths": [],
    }

    try:
        config = load_ezlynx_api_config()
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        summary["auth"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: config load failed",
        }
        print(json.dumps(summary, indent=2))
        return 1

    client = EzlynxApiClient(config)
    try:
        token = client.get_token()
    except EzlynxApiError as exc:
        summary["auth"] = {
            "ok": False,
            "status": exc.status,
            "retryable": exc.retryable,
            "error": str(exc)[:200],
        }
        print(json.dumps(summary, indent=2))
        return 1

    summary["auth"] = {"ok": True, "token_acquired": bool(token)}

    for path in CANDIDATE_PATHS:
        entry: dict = {"path": path}
        try:
            result = client.api_get(path, timeout=30)
            entry["status"] = 200
            entry["shape"] = type(result).__name__
            if isinstance(result, dict):
                entry["keys"] = list(result.keys())[:10]
        except EzlynxApiError as exc:
            entry["status"] = exc.status
            entry["retryable"] = exc.retryable
            entry["error"] = str(exc)[:160]
        summary["paths"].append(entry)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
