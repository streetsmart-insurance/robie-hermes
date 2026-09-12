# Provisioning and rotation

Run `sh scripts/provision-secrets.sh <gcp-project-id>` from an authenticated operator workstation only when initially provisioning or rotating credentials. The helper creates or reuses `ezlynx-username` and `ezlynx-password`, then prompts locally for new secret versions without echoing the password.

Grant the Google Cloud identity used by the Hermes host `roles/secretmanager.secretAccessor` on only those two secrets. Configure `/etc/streetsmart-hermes/robie-ezlynx.env` from `deploy/systemd/robie-ezlynx.env.example` with mode `0600`.

Install `deploy/requirements-ezlynx-session.txt` in the Hermes virtual environment and keep the persistent browser unit. Do **not** enable `robie-ezlynx-session.timer` or `robie-chrome-refresh.timer` — those owners are retired. Hourly heal is `.github/workflows/monitor-ezlynx-session.yml`. Dusty will mask the live 03:30 chrome-refresh unit separately.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now robie-ezlynx-browser.service
```

Inspect only the sanitized session status from
`scripts/check_ezlynx_session.py --json`. Never print or log secret payloads.
