#!/usr/bin/env python3
"""Read-only probe of the EZLynx Documents API (self-contained).

Runs on hermes-poc-01 (which has real internet access and the VM service
account). It:

  1. Loads the EZLynx API config for the requested environment from
     Secret Manager (UAT or Production credentials) via the gcloud CLI.
  2. Acquires an OAuth2 token (vendor_data_access grant).
  3. Issues read-only GETs against candidate DocumentApi paths and
     reports HTTP status codes.

Self-contained: imports nothing from robie_job_engine, so the workflow
only has to copy this one script to the VM (no recursive folder copy,
which proved unreliable).

No documents are downloaded, nothing is written, and no secret values
are printed. Output is a JSON summary for the workflow evidence artifact.
Every network call carries an explicit timeout so the probe can never
hang indefinitely.

Usage:
  sudo python3 probe_ezlynx_document_api.py <uat|prod> <secret-resource-name>
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import time
import urllib.parse

# Mirrors robie_job_engine.ezlynx_api.REQUIRED_CONFIG_FIELDS exactly, so the
# probe validates the same secret shape the engine will consume.
REQUIRED_FIELDS = (
    "client_id",
    "client_secret",
    "username",
    "integration_group_id",
    "token_endpoint",
    "document_base_url",
    "scope",
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

CALL_TIMEOUT = 30  # seconds for every network operation


def load_secret(secret_resource: str) -> dict:
    """Fetch the secret JSON via the gcloud CLI on the VM.

    `gcloud secrets versions access` prints the raw payload (already
    decoded), so no base64 step is needed.
    """
    parts = secret_resource.split("/")
    project, name = parts[1], parts[3]
    proc = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            name,
            "--project",
            project,
        ],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gcloud secret access failed: {proc.stderr[:160]}")
    return json.loads(proc.stdout.strip())


GRANT_TYPE = "vendor_data_access"


def get_token(cfg: dict) -> str:
    """OAuth2 token request, mirroring EzlynxApiClient.get_token exactly."""
    parsed = urllib.parse.urlparse(cfg["token_endpoint"])
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
    try:
        body = urllib.parse.urlencode(
            {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "grant_type": GRANT_TYPE,
                "scope": cfg["scope"],
                "username": cfg["username"],
                "integration_group_id": cfg["integration_group_id"],
            }
        )
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"token request failed: HTTP {resp.status}")
    data = json.loads(raw.decode())
    token = data.get("access_token")
    if not token:
        raise RuntimeError("token response had no access_token")
    return token


def probe_path(cfg: dict, token: str, path: str) -> dict:
    entry: dict = {"path": path}
    try:
        parsed = urllib.parse.urlparse(cfg["document_base_url"])
        base = (parsed.path or "").rstrip("/")
        full_path = f"{base}/{path.lstrip('/')}"
        if parsed.query:
            full_path += "?" + parsed.query
        conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
        try:
            conn.request("GET", full_path, headers={"Authorization": f"Bearer {token}"})
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        entry["status"] = resp.status
        if resp.status == 200:
            try:
                result = json.loads(raw.decode())
                entry["shape"] = type(result).__name__
                if isinstance(result, dict):
                    entry["keys"] = list(result.keys())[:10]
            except Exception:  # noqa: BLE001
                entry["shape"] = f"non-json ({len(raw)} bytes)"
        else:
            entry["body_hint"] = raw[:120].decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        entry["status"] = None
        entry["error"] = f"{type(exc).__name__}: {exc}"[:160]
    return entry


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"uat", "prod"}:
        print(
            "usage: probe_ezlynx_document_api.py <uat|prod> <secret-resource-name>",
            file=sys.stderr,
        )
        return 2

    summary: dict = {
        "environment": argv[1],
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "self_contained": True,
        "auth": {"ok": False},
        "paths": [],
    }

    try:
        cfg = load_secret(argv[2])
    except Exception as exc:  # noqa: BLE001
        summary["auth"] = {"ok": False, "error": f"config load failed: {exc}"[:200]}
        print(json.dumps(summary, indent=2))
        return 1

    if not isinstance(cfg, dict):
        summary["auth"] = {"ok": False, "error": "config payload is not a JSON object"}
        print(json.dumps(summary, indent=2))
        return 1
    missing = [f for f in REQUIRED_FIELDS if not str(cfg.get(f) or "").strip()]
    summary["config"] = {
        "fields_present": sorted(k for k in cfg.keys()),
        "fields_missing": missing,
    }
    if missing:
        summary["auth"] = {
            "ok": False,
            "error": f"secret missing fields: {', '.join(missing)}",
        }
        print(json.dumps(summary, indent=2))
        return 1

    try:
        token = get_token(cfg)
    except Exception as exc:  # noqa: BLE001
        summary["auth"] = {"ok": False, "error": str(exc)[:200]}
        print(json.dumps(summary, indent=2))
        return 1

    summary["auth"] = {"ok": True, "token_acquired": True}

    for path in CANDIDATE_PATHS:
        summary["paths"].append(probe_path(cfg, token, path))

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
