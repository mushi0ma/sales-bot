#!/usr/bin/env bash
# Shell or one command on the VM over IAP (no public SSH):
#   deploy/ssh.sh                      interactive shell
#   deploy/ssh.sh 'docker ps'          one command
#   deploy/ssh.sh -- -N -L 3100:127.0.0.1:3100   extra ssh flags (here: the agents console on localhost:3100)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/gcp.env"
g=(gcloud compute ssh "$GCP_VM" --project="$GCP_PROJECT" --zone="$GCP_ZONE" --tunnel-through-iap --quiet)
if [[ "${1:-}" == "--" ]]; then exec "${g[@]}" "$@"; fi
exec "${g[@]}" ${1:+--command="$1"}
