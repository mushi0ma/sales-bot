#!/usr/bin/env bash
# Dump the Postgres databases (Chatwoot, agents, Twenty: the cards operators also edit by hand) and upload them to gs://$GCS_BUCKET/backups/ (the bucket deletes them after 30 days).
# Runs on the VM (systemd timer salesbot-backup). Streams pg_dump straight to the bucket, nothing on local disk.
set -euo pipefail
root=/srv/salesbot
source "$root/deploy/gcp.env"
cd "$root/stack"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
for spec in "cw-postgres chatwoot_production" "agents-postgres fazerai_agents_db" "platform-postgres twenty"; do
  set -- $spec
  docker compose exec -T "$1" pg_dump -U postgres -Fc "$2" \
    | gcloud storage cp - "gs://$GCS_BUCKET/backups/$ts/$2.dump" --project="$GCP_PROJECT" --quiet
done
echo "backup $ts -> gs://$GCS_BUCKET/backups/$ts/"
