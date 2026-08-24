# Provisioning and rotation

Run `sh scripts/provision-secrets.sh <gcp-project-id>` from an authenticated operator workstation only when initially provisioning or rotating credentials. The helper creates or reuses `ezlynx-username` and `ezlynx-password`, then prompts locally for new secret versions without echoing the password.

Grant the Google Cloud identity used by the Hermes host `roles/secretmanager.secretAccessor` on only those two secrets. Configure `/etc/streetsmart-hermes/robie-ezlynx.env` from `deploy/systemd/robie-ezlynx.env.example` with mode `0600`.

Install `deploy/requirements-ezlynx-session.txt` in the Hermes virtual environment, deploy the two systemd units, then enable the timer:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now robie-ezlynx-browser.service robie-ezlynx-session.timer
```

Test with `sudo systemctl start robie-ezlynx-session.service` and inspect only the sanitized session status. Never print or log secret payloads.
