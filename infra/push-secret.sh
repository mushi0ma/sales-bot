#!/usr/bin/env bash
# Copy ONE variable from ./.env (host) into the VM's ~/work/stack/.env, replacing an existing line.
# The value travels over ssh stdin only: never argv, never the terminal, never shell history.
#   infra/push-secret.sh TELEGRAM_BOT_TOKEN
set -euo pipefail
name="${1:?usage: $0 VAR_NAME}"
[[ "$name" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "bad variable name" >&2; exit 1; }
root="$(cd "$(dirname "$0")/.." && pwd)"
line="$(grep -E "^${name}=" "$root/.env" | tail -1 || true)"
[[ -n "${line#*=}" ]] || { echo "$name is not set in ./.env" >&2; exit 1; }
printf '%s\n' "$line" | "$root/infra/vm.sh" ssh -o BatchMode=yes \
  "f=~/work/stack/.env; umask 077; touch \$f; { grep -v '^${name}=' \$f | sed -e '\$a\\' || true; cat; } > \$f.new; mv \$f.new \$f; chmod 600 \$f; echo '${name} -> VM stack/.env'"
