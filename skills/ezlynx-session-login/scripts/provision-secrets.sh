#!/bin/sh
set -eu
set +x
umask 077

project_id=${1:-}
if [ -z "$project_id" ]; then
  echo "Usage: $0 <gcp-project-id>" >&2
  exit 2
fi

command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is required" >&2
  exit 2
}

username_secret=ezlynx-username
password_secret=ezlynx-password

for secret_name in "$username_secret" "$password_secret"; do
  if ! gcloud secrets describe "$secret_name" --project "$project_id" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" --project "$project_id" --replication-policy=automatic >/dev/null
  fi
done

printf "EZLynx username: " >&2
IFS= read -r ezlynx_username
printf "EZLynx password: " >&2
trap 'stty echo 2>/dev/null || true; unset ezlynx_username ezlynx_password' EXIT INT TERM
stty -echo
IFS= read -r ezlynx_password
stty echo
printf "\n" >&2

[ -n "$ezlynx_username" ] || { echo "Username cannot be empty" >&2; exit 2; }
[ -n "$ezlynx_password" ] || { echo "Password cannot be empty" >&2; exit 2; }

printf %s "$ezlynx_username" | gcloud secrets versions add "$username_secret" --project "$project_id" --data-file=- >/dev/null
printf %s "$ezlynx_password" | gcloud secrets versions add "$password_secret" --project "$project_id" --data-file=- >/dev/null

unset ezlynx_username ezlynx_password
trap - EXIT INT TERM
echo "Stored new EZLynx credential versions in Google Secret Manager." >&2
