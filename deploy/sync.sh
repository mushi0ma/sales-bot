#!/usr/bin/env bash
# Push stack/ and deploy/ from this checkout to the VM ($VM_ROOT). The checkout is the source of truth.
# Never sends .env files or keys. vendor/ is NOT sent: the VM clones it itself (deploy/vm/fetch-vendor.sh).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
source "$here/gcp.env"
rsync -rlptz --delete -e "$here/rsh" \
  --exclude '.env' --exclude '.env.*' --exclude '__pycache__/' --exclude '*.pyc' \
  --filter 'P stack/.env' \
  "$root/stack" "$root/deploy" "vm:$VM_ROOT/"
echo "synced stack/ and deploy/ -> $GCP_VM:$VM_ROOT"
