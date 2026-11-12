#!/usr/bin/env bash
# Push code from the host to the VM (~/work). Host is the source of truth; the VM copy is disposable.
# Never syncs the host .env, VM disks or keys.
set -euo pipefail
INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$INFRA")"
source "$INFRA/vm.conf"
SSH="ssh -i $INFRA/vm/ssh/id_ed25519 -p $SSH_PORT -o IdentitiesOnly=yes -o UserKnownHostsFile=$INFRA/vm/ssh/known_hosts"
# chatwoot's Dockerfile runs `git rev-parse HEAD`, so its .git must reach the build context.
rsync -az --delete -e "$SSH" \
  --include '/vendor/chatwoot/.git/' --exclude '.git/' --exclude 'node_modules/' --exclude '_downloads/' --exclude 'tmp/cache/' \
  --exclude '.env' --exclude '.env.*' --exclude 'stack/.env' \
  "$ROOT/vendor" "$ROOT/stack" "$VM_USER@$SSH_BIND:work/" \
  --filter 'P stack/.env'
echo "synced vendor/ and stack/ -> $VM_USER@vm:~/work/"
